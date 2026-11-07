#!/usr/bin/env python3
"""Wire an app repository up for nas-deploy-agent auto-deploys.

Run this from the root of the app repository:

    cd /path/to/app-repo
    /path/to/nas-deploy-agent/setup_auto_deploy.py

With no arguments (or -i) the script asks for the values it needs. Flags can be
mixed with -i; whatever is passed becomes the interactive default.

It adds two jobs to a GitHub Actions workflow:

  publish  builds an image and pushes :latest plus :sha-<short> to GitHub
           Container Registry using the built-in GITHUB_TOKEN.
  deploy   calls the NAS webhook after publish succeeds, with an HMAC signature.

If the repository has no workflow, a complete one is created at
.github/workflows/deploy.yml. If it has exactly one workflow, the jobs are
appended to it; with more than one, pass --workflow to pick one explicitly.

Optional first-run helpers:
  --update-projects / --projects-file   add the project to projects.json
  --set-secrets                         set DEPLOY_WEBHOOK_URL/SECRET with gh
  --agent-env                           write the secret to the agent's .env
  --nas-host / --create-compose         verify or scaffold the NAS compose file
  --check                               verify the agent's /healthz endpoint
  --make-package-public                 make the GHCR package public after the
                                        first publish (gh api)

Only the Python standard library is used.
"""

import argparse
import getpass
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

WORKFLOW_DIR = Path(".github/workflows")
DEFAULT_WORKFLOW = WORKFLOW_DIR / "deploy.yml"
TEST_JOB_NAMES = {"test", "tests", "unit", "unit-tests", "ci", "lint"}

PUBLISH_JOB = """\
  publish:
    name: Publish image
    runs-on: ubuntu-latest
__NEEDS__    if: __IF__
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4

      - uses: docker/setup-buildx-action@v3

      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - id: meta
        uses: docker/metadata-action@v5
        with:
          images: __IMAGE__
          tags: |
            type=raw,value=latest
            type=sha,format=short,prefix=sha-

      - uses: docker/build-push-action@v6
        with:
          context: __CONTEXT__
          file: __DOCKERFILE__
          platforms: __PLATFORM__
          push: true
__BUILD_ARGS__          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
__BUILD_SECRETS__          cache-from: type=gha
          cache-to: type=gha,mode=max"""

CONCURRENCY = """\
    concurrency:
      group: nas-deploy-__PROJECT__
      cancel-in-progress: false
"""

DEPLOY_JOB = """\
  deploy:
    name: Deploy
    runs-on: ubuntu-latest
__CONCURRENCY__    needs: publish
    if: __IF__
    steps:
      - name: Ask the NAS to deploy
        env:
          DEPLOY_WEBHOOK_URL: ${{ secrets.DEPLOY_WEBHOOK_URL }}
          DEPLOY_WEBHOOK_SECRET: ${{ secrets.DEPLOY_WEBHOOK_SECRET }}
          PROJECT: __PROJECT__
        run: |
          if [ -z "$DEPLOY_WEBHOOK_URL" ] || [ -z "$DEPLOY_WEBHOOK_SECRET" ]; then
            echo "DEPLOY_WEBHOOK_URL or DEPLOY_WEBHOOK_SECRET is not set; skipping deploy"
            exit 0
          fi
          BODY=$(printf '{"project":"%s","sha":"%s"}' "$PROJECT" "$GITHUB_SHA")
          TS=$(date +%s)
          SIG=$(python3 -c 'import hashlib,hmac,sys; print(hmac.new(sys.argv[1].encode(), sys.argv[2].encode()+b"."+sys.argv[3].encode(), hashlib.sha256).hexdigest())' "$DEPLOY_WEBHOOK_SECRET" "$TS" "$BODY")
          attempt=1
          while true; do
            code=$(curl -sS -o /tmp/nas-deploy.out -w '%{http_code}' -X POST "$DEPLOY_WEBHOOK_URL" \\
              -H 'Content-Type: application/json' \\
              -H "X-Deploy-Timestamp: $TS" \\
              -H "X-Deploy-Signature: sha256=$SIG" \\
              --data "$BODY") || code=000
            if [ "$code" != "409" ] || [ "$attempt" -ge __RETRIES__ ]; then
              break
            fi
            echo "another deploy is running; retrying in 10s ($attempt/__RETRIES__)"
            sleep 10
            attempt=$((attempt + 1))
          done
          cat /tmp/nas-deploy.out 2>/dev/null || true
          case "$code" in
            2??) ;;
            *) echo "deploy failed with HTTP $code"; exit 1 ;;
          esac
"""

NEW_WORKFLOW = """\
name: Auto deploy

on:
  push:
    branches: [__BRANCH__]
  workflow_dispatch:

permissions:
  contents: read
  packages: write

jobs:
__JOBS__
"""

COMPOSE_TEMPLATE = """\
# Created by setup_auto_deploy.py for __PROJECT__.
# Add ports, environment, volumes, and any other services this app needs.
services:
  __SERVICE__:
    image: __IMAGE__:latest
    restart: unless-stopped
"""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def run_git(root, *args):
    try:
        return subprocess.check_output(
            ["git", *args], cwd=str(root), text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def parse_github_slug(url):
    """Return owner/repo from a GitHub remote URL, lowercased for GHCR."""
    if not url:
        return None
    url = url.strip()
    patterns = (
        r"git@github\.com:(?P<slug>.+?)(?:\.git)?$",
        r"ssh://git@github\.com/(?P<slug>.+?)(?:\.git)?/?$",
        r"https?://(?:[^@/]+@)?github\.com/(?P<slug>.+?)(?:\.git)?/?$",
    )
    for pattern in patterns:
        match = re.match(pattern, url)
        if match:
            return match.group("slug").lower()
    return None


def github_slug(root):
    env = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if env:
        return env.lower()
    return parse_github_slug(run_git(root, "remote", "get-url", "origin"))


def detect_default_branch(root):
    ref = run_git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if ref and "/" in ref:
        return ref.split("/", 1)[1]
    if shutil.which("gh"):
        try:
            branch = subprocess.check_output(
                ["gh", "repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"],
                cwd=str(root),
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            if branch:
                return branch
        except (OSError, subprocess.CalledProcessError):
            pass
    return "main"


def sanitize_project(name):
    """Docker Compose project names are lowercase [a-z0-9][a-z0-9_-]*."""
    name = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")
    return name or "app"


def sanitize_service(name):
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-")
    return name or "app"


def yaml_string(value):
    return json.dumps(str(value))


def find_top_level_key(lines, key):
    pattern = re.compile(r"^" + re.escape(key) + r"\s*:")
    for index, line in enumerate(lines):
        if pattern.match(line):
            return index
    return None


def block_end(lines, start):
    """Index of the first line after a top-level block."""
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        if not line[0].isspace():
            return index
    return len(lines)


def child_indent(lines, start, end):
    for line in lines[start + 1 : end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^(\s+)\S", line)
        if match:
            return match.group(1)
    return "  "


def keys_at_indent(lines, start, end, indent):
    pattern = re.compile(r"^" + re.escape(indent) + r"([A-Za-z0-9_.-]+)\s*:")
    return [m.group(1) for line in lines[start + 1 : end] if (m := pattern.match(line))]


def change_indent(text, base="  ", target="  "):
    if base == target:
        return text
    if target.startswith(base):
        extra = target[len(base) :]
        return "\n".join(
            extra + line if line.startswith(base) else line
            for line in text.splitlines()
        )
    return "\n".join(
        target + line[len(base) :] if line.startswith(base) else line
        for line in text.splitlines()
    )


def insert_lines(lines, index, new_lines):
    new = list(new_lines)
    if index > 0 and lines[index - 1].strip():
        new.insert(0, "")
    if index < len(lines) and lines[index].strip():
        new.append("")
    lines[index:index] = new


def compose_services(root, image):
    """Best-effort app services from a compose file.

    Prefer services whose image is the one this workflow publishes; never
    return database/sidecar services just because they are in the file. With a
    single-service project there is no ambiguity, so use it.
    """
    for name in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml"):
        path = root / name
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        services = find_top_level_key(lines, "services")
        if services is None:
            continue
        end = block_end(lines, services)
        indent = child_indent(lines, services, end)
        pattern = re.compile(r"^" + re.escape(indent) + r"([A-Za-z0-9_.-]+)\s*:")
        starts = [
            (index, match.group(1))
            for index in range(services + 1, end)
            if (match := pattern.match(lines[index]))
        ]
        matched = []
        for position, (start, service) in enumerate(starts):
            stop = starts[position + 1][0] if position + 1 < len(starts) else end
            if any(image in line for line in lines[start:stop]):
                matched.append(service)
        if matched:
            return matched, path
        if len(starts) == 1:
            return [starts[0][1]], path
    return [], None


def has_push_trigger(lines):
    on_index = find_top_level_key(lines, "on")
    if on_index is None:
        return False
    if "push" in lines[on_index]:
        return True
    end = block_end(lines, on_index)
    return any(
        re.match(r"^\s+push\s*:", line) for line in lines[on_index + 1 : end]
    )


def workflow_job_names(text):
    lines = text.splitlines()
    jobs_index = find_top_level_key(lines, "jobs")
    if jobs_index is None:
        return []
    end = block_end(lines, jobs_index)
    indent = child_indent(lines, jobs_index, end)
    return keys_at_indent(lines, jobs_index, end, indent)


def discover_workflows(root):
    directory = root / WORKFLOW_DIR
    if not directory.is_dir():
        return []
    return sorted(
        path.resolve()
        for path in directory.iterdir()
        if path.suffix in (".yml", ".yaml")
    )


# --------------------------------------------------------------------------
# Local and SSH targets
# --------------------------------------------------------------------------


def split_remote_target(target):
    """Return (host, path) for host:/path or user@host:/path, else (None, path)."""
    host, sep, path = target.partition(":")
    if sep and path and host and "/" not in host and "\\" not in host:
        return host, path
    return None, target


def host_from_target(target):
    if not target:
        return None
    return split_remote_target(target)[0]


def ssh_run(host, command, input_text=None):
    return subprocess.run(
        ["ssh", host, command],
        input=input_text,
        capture_output=True,
        text=True,
    )


def target_read(host, path):
    if host:
        proc = ssh_run(host, "cat " + shlex.quote(path))
        if proc.returncode != 0:
            raise RuntimeError(
                "cannot read %s:%s: %s"
                % (host, path, proc.stderr.strip() or "ssh failed")
            )
        return proc.stdout
    return Path(path).read_text(encoding="utf-8")


def target_write(host, path, text):
    if host:
        tmp = path + ".tmp"
        command = "cat > %s && mv %s %s" % (
            shlex.quote(tmp),
            shlex.quote(tmp),
            shlex.quote(path),
        )
        proc = ssh_run(host, command, input_text=text)
        if proc.returncode != 0:
            raise RuntimeError(
                "cannot write %s:%s: %s"
                % (host, path, proc.stderr.strip() or "ssh failed")
            )
        return

    path_obj = Path(path)
    mode = stat.S_IMODE(path_obj.stat().st_mode) if path_obj.exists() else None
    fd, tmp = tempfile.mkstemp(prefix=path_obj.name + ".", dir=str(path_obj.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path_obj)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def target_exists(host, path):
    if host:
        return ssh_run(host, "test -f " + shlex.quote(path)).returncode == 0
    return Path(path).is_file()


def target_mkdir(host, path):
    if host:
        proc = ssh_run(host, "mkdir -p " + shlex.quote(path))
        if proc.returncode != 0:
            raise RuntimeError(
                "cannot create %s:%s: %s"
                % (host, path, proc.stderr.strip() or "ssh failed")
            )
        return
    path_obj = Path(path)
    if not path_obj.parent.exists():
        raise RuntimeError(
            "%s does not exist; mount the NAS share or pass --nas-host" % path_obj.parent
        )
    path_obj.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# projects.json and .env
# --------------------------------------------------------------------------


def dump_projects(data):
    """Serialize projects.json in the style of projects.example.json."""
    entries = []
    for name, cfg in data.items():
        if not isinstance(cfg, dict):
            raise RuntimeError("%s: entry must be a JSON object" % name)
        lines = ["  %s: {" % json.dumps(name, ensure_ascii=False)]
        items = list(cfg.items())
        for index, (key, value) in enumerate(items):
            if key == "services" and isinstance(value, list):
                rendered = "[" + ", ".join(
                    json.dumps(item, ensure_ascii=False) for item in value
                ) + "]"
            else:
                rendered = json.dumps(value, ensure_ascii=False)
            comma = "," if index + 1 < len(items) else ""
            lines.append(
                "    %s: %s%s"
                % (json.dumps(key, ensure_ascii=False), rendered, comma)
            )
        lines.append("  }")
        entries.append("\n".join(lines))
    if not entries:
        return "{}\n"
    return "{\n" + ",\n".join(entries) + "\n}\n"


def plan_projects_update(target, project, entry, overwrite):
    """Return (new_text, status) without writing.

    status is "updated" or "unchanged". Raises RuntimeError for a missing or
    invalid file, or a conflicting existing entry when overwrite is false.
    """
    host, path = split_remote_target(target)
    raw = target_read(host, path)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError("%s: invalid JSON: %s" % (target, exc))
    if not isinstance(data, dict):
        raise RuntimeError("%s: top level must be a JSON object" % target)

    if data.get(project) == entry:
        return None, "unchanged"
    if project in data and not overwrite:
        raise RuntimeError(
            '%s: project "%s" already exists with different values; '
            "pass --overwrite-project to replace it" % (target, project)
        )
    data[project] = entry
    return dump_projects(data), "updated"


ENV_LINE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")


def get_env_value(text, key):
    for line in text.splitlines():
        match = ENV_LINE.match(line.strip())
        if match and match.group("key") == key:
            return match.group("value")
    return None


def set_env_value(text, key, value):
    pattern = re.compile(r"^" + re.escape(key) + r"=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub("%s=%s" % (key, value), text, count=1)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + "%s=%s\n" % (key, value)


# --------------------------------------------------------------------------
# Workflow building
# --------------------------------------------------------------------------


def validate_build_args(values):
    result = []
    for value in values:
        if "=" not in value:
            raise SystemExit("error: --build-arg must be KEY=VALUE: %s" % value)
        key = value.split("=", 1)[0]
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_.-]*$", key):
            raise SystemExit("error: invalid build arg name: %s" % key)
        result.append(value)
    return result


def validate_build_secrets(values):
    result = []
    for value in values:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_.-]*$", value):
            raise SystemExit("error: invalid build secret name: %s" % value)
        result.append(value)
    return result


def build_args_block(values):
    if not values:
        return ""
    return "          build-args: |\n" + "".join(
        "            %s\n" % value for value in values
    )


def build_secrets_block(values):
    if not values:
        return ""
    return "          secrets: |\n" + "".join(
        "            %s=${{ secrets.%s }}\n" % (value, value) for value in values
    )


def build_job_texts(args, image, project):
    run_if = "github.ref == 'refs/heads/%s'" % args.branch
    needs = "    needs: %s\n" % args.needs if args.needs else ""
    concurrency = "" if args.no_concurrency else CONCURRENCY

    publish = (
        PUBLISH_JOB.replace("__NEEDS__", needs)
        .replace("__IF__", run_if)
        .replace("__IMAGE__", yaml_string(image))
        .replace("__CONTEXT__", yaml_string(args.context))
        .replace("__DOCKERFILE__", yaml_string(args.dockerfile))
        .replace("__PLATFORM__", yaml_string(args.platform))
        .replace("__BUILD_ARGS__", build_args_block(args.build_arg))
        .replace("__BUILD_SECRETS__", build_secrets_block(args.build_secret))
    )
    deploy = (
        DEPLOY_JOB.replace("__CONCURRENCY__", concurrency)
        .replace("__IF__", run_if)
        .replace("__PROJECT__", project)
        .replace("__RETRIES__", str(args.deploy_retries))
    )
    return publish, deploy


def add_jobs(text, publish, deploy):
    """Insert missing publish/deploy jobs. Returns (text, added, existing)."""
    lines = text.splitlines()
    jobs_index = find_top_level_key(lines, "jobs")

    if jobs_index is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("jobs:")
        new_lines = (publish + "\n\n" + deploy).splitlines()
        insert_lines(lines, len(lines), new_lines)
        return "\n".join(lines).rstrip("\n") + "\n", ["publish", "deploy"], []

    if re.match(r"^jobs\s*:\s*(\{\}|null)?\s*(#.*)?$", lines[jobs_index]):
        lines[jobs_index] = "jobs:"

    end = block_end(lines, jobs_index)
    indent = child_indent(lines, jobs_index, end)
    existing = keys_at_indent(lines, jobs_index, end, indent)

    wanted = [("publish", publish), ("deploy", deploy)]
    missing = [(name, body) for name, body in wanted if name not in existing]
    if not missing:
        return "\n".join(lines).rstrip("\n") + "\n", [], existing

    chunk = "\n\n".join(
        change_indent(body, base="  ", target=indent) for _, body in missing
    ).splitlines()
    insert_lines(lines, end, chunk)
    added = [name for name, _ in missing]
    return "\n".join(lines).rstrip("\n") + "\n", added, existing


def choose_workflow(root, explicit):
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            if path.parent == Path("."):
                path = root / WORKFLOW_DIR / path
            else:
                path = root / path
        return path.resolve(), None

    found = discover_workflows(root)
    if not found:
        return (root / DEFAULT_WORKFLOW).resolve(), None
    if len(found) == 1:
        return found[0], None
    listing = "\n".join("  " + str(path.relative_to(root)) for path in found)
    return None, (
        "found multiple workflow files; pass --workflow to choose one:\n" + listing
    )


# --------------------------------------------------------------------------
# NAS compose file
# --------------------------------------------------------------------------


def compose_candidates(project_dir):
    return [
        project_dir.rstrip("/") + "/" + name
        for name in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")
    ]


def find_compose(host, project_dir):
    for path in compose_candidates(project_dir):
        if target_exists(host, path):
            return path
    return None


def plan_nas(args, project, services, image):
    """Return (messages, actions) for the NAS-side compose file."""
    if not (args.update_projects or args.nas_host or args.create_compose):
        return [], []

    host = args.nas_host or host_from_target(args.projects_file)
    project_dir = args.project_dir or "/volume1/docker/" + project
    if not project_dir.startswith("/"):
        raise RuntimeError("--project-dir must be an absolute path")

    messages = []
    actions = []
    existing = find_compose(host, project_dir)

    if existing:
        if host:
            text = target_read(host, existing)
        else:
            text = Path(existing).read_text(encoding="utf-8")
        if image in text:
            messages.append(("ok", "%s already references %s" % (existing, image)))
        else:
            messages.append(
                (
                    "warn",
                    "%s does not reference %s; add image: %s:latest"
                    % (existing, image, image),
                )
            )
        return messages, actions

    if not args.create_compose:
        messages.append(
            (
                "warn",
                "no compose file in %s; pass --create-compose to create one"
                % project_dir,
            )
        )
        return messages, actions

    service = sanitize_service((services or ["app"])[0])
    text = (
        COMPOSE_TEMPLATE.replace("__PROJECT__", project)
        .replace("__SERVICE__", service)
        .replace("__IMAGE__", image)
    )
    path = compose_candidates(project_dir)[0]
    actions.append({"host": host, "dir": project_dir, "path": path, "text": text})
    messages.append(("create", path))
    return messages, actions


# --------------------------------------------------------------------------
# Secrets, health, package visibility
# --------------------------------------------------------------------------


def gh_available():
    return shutil.which("gh") is not None


def gh_run(root, *args):
    return subprocess.run(
        ["gh", *args], cwd=str(root), capture_output=True, text=True
    )


def resolve_webhook_secret(args):
    """Return the webhook secret to use, or None.

    Order: --webhook-secret, --generate-secret, DEPLOY_WEBHOOK_SECRET, then the
    agent's .env.
    """
    if args.webhook_secret:
        return args.webhook_secret
    if args.generate_secret:
        if not args.agent_env:
            raise RuntimeError(
                "--generate-secret needs --agent-env so the agent uses the same secret"
            )
        return secrets.token_hex(32)
    env = os.environ.get("DEPLOY_WEBHOOK_SECRET", "")
    if env:
        return env
    if args.agent_env:
        host, path = split_remote_target(args.agent_env)
        try:
            text = target_read(host, path)
        except (OSError, RuntimeError):
            text = ""
        found = get_env_value(text, "DEPLOY_WEBHOOK_SECRET")
        if found:
            return found
    return None


def set_repo_secrets(root, slug, deploy_url, webhook_secret):
    if not gh_available():
        raise RuntimeError("gh is not installed; cannot set repository secrets")
    if "/" not in slug or slug == "owner/repo":
        raise RuntimeError("cannot determine the GitHub repository; pass --image")
    proc = gh_run(
        root,
        "secret",
        "set",
        "DEPLOY_WEBHOOK_URL",
        "--repo",
        slug,
        "--body",
        deploy_url,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh secret set failed")
    proc = gh_run(
        root,
        "secret",
        "set",
        "DEPLOY_WEBHOOK_SECRET",
        "--repo",
        slug,
        "--body",
        webhook_secret,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh secret set failed")


def health_url(deploy_url):
    base = deploy_url.rstrip("/")
    if base.endswith("/deploy"):
        base = base[: -len("/deploy")]
    return base + "/healthz"


def check_health(deploy_url):
    url = health_url(deploy_url)
    request = urllib.request.Request(
        url, headers={"User-Agent": "setup-auto-deploy"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read(300).decode("utf-8", "replace").strip()
            return True, "%s -> %s %s" % (url, response.status, body)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, "%s -> %s" % (url, exc)


def make_package_public(root, slug, image):
    if not gh_available():
        raise RuntimeError("gh is not installed; cannot change package visibility")
    if "/" not in slug:
        raise RuntimeError("cannot determine the GitHub repository")
    owner = slug.split("/", 1)[0]
    package = image.rstrip("/").rsplit("/", 1)[-1]
    package = urllib.parse.quote(package, safe="")

    user_info = gh_run(root, "api", "/users/" + owner, "--jq", ".type")
    owner_type = user_info.stdout.strip() if user_info.returncode == 0 else ""
    endpoints = []
    if owner_type == "Organization":
        endpoints.append("/orgs/%s/packages/container/%s" % (owner, package))
    endpoints.append("/user/packages/container/%s" % package)

    errors = []
    for endpoint in endpoints:
        proc = gh_run(
            root,
            "api",
            "--method",
            "PATCH",
            endpoint,
            "-f",
            "visibility=public",
        )
        if proc.returncode == 0:
            return True
        errors.append(proc.stderr.strip() or proc.stdout.strip())
    raise RuntimeError(
        "could not make the package public (publish the image first, then rerun): "
        + "; ".join(filter(None, errors))
    )


# --------------------------------------------------------------------------
# Interactive mode
# --------------------------------------------------------------------------


def ask(prompt, default=None):
    suffix = " [%s]" % default if default else ""
    try:
        answer = input("%s%s: " % (prompt, suffix)).strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\ninteractive setup cancelled")
    return answer or default


def ask_yes_no(prompt, default=True):
    suffix = " [Y/n]" if default else " [y/N]"
    try:
        answer = input("%s%s: " % (prompt, suffix)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\ninteractive setup cancelled")
    if not answer:
        return default
    return answer in ("y", "yes")


def ask_secret(prompt):
    try:
        return getpass.getpass(prompt + " (leave blank to use env/agent .env): ")
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\ninteractive setup cancelled")


def interactive_configure(args, ctx):
    print("Interactive setup. Press Enter to accept the default.\n")

    args.project = sanitize_project(ask("Project name", args.project or ctx["project"]))
    args.image = ask("Image", args.image or ctx["image"])
    args.branch = ask("Deploy branch", args.branch or ctx["branch"])
    args.platform = ask("Platform", args.platform or "linux/amd64")
    args.context = ask("Build context", args.context or ".")
    args.dockerfile = ask("Dockerfile", args.dockerfile or "Dockerfile")
    default_services = args.services or ", ".join(ctx["services"])
    args.services = ask("Compose services for projects.json", default_services)

    if ctx["workflow_options"]:
        print("\nWorkflow files:")
        for path in ctx["workflow_options"]:
            print("  " + str(path.relative_to(ctx["root"])))
    args.workflow = ask("Workflow file", args.workflow or ctx["default_workflow"])

    path, error = choose_workflow(ctx["root"], args.workflow)
    if not error and path.is_file():
        jobs = workflow_job_names(path.read_text(encoding="utf-8"))
        candidates = [
            job for job in jobs if job in TEST_JOB_NAMES and job not in ("publish", "deploy")
        ]
        if candidates:
            default_needs = args.needs or (candidates[0] if len(candidates) == 1 else "")
            answer = ask(
                "Gate publish on job (blank for none)", default_needs or ""
            )
            args.needs = answer or None

    print()
    update_default = (
        args.update_projects
        or bool(args.projects_file)
        or bool(os.environ.get("PROJECTS_FILE"))
    )
    if ask_yes_no("Update/verify the NAS project now?", update_default):
        default_target = args.projects_file or os.environ.get("PROJECTS_FILE", "")
        target = ask(
            "projects.json (local path or host:/path)", default_target or None
        )
        if target:
            args.update_projects = True
            args.projects_file = target
        else:
            print("  no projects.json target; skipping the projects.json update")
            args.update_projects = False
        args.project_dir = ask(
            "Project directory on the NAS",
            args.project_dir or "/volume1/docker/" + args.project,
        )
        derived_host = host_from_target(args.projects_file)
        args.nas_host = ask(
            "NAS SSH host (blank to skip remote checks)",
            args.nas_host or derived_host or None,
        )
        args.create_compose = ask_yes_no(
            "Create dir/compose.yaml if missing?",
            args.create_compose or bool(args.nas_host),
        )
    else:
        args.update_projects = False

    print()
    set_default = args.set_secrets or gh_available()
    if ask_yes_no("Set repository secrets with gh?", set_default):
        args.set_secrets = True
        args.deploy_url = ask(
            "Webhook URL",
            args.deploy_url or os.environ.get("DEPLOY_WEBHOOK_URL") or None,
        )
        if not args.deploy_url:
            print("  no webhook URL; skipping secrets")
            args.set_secrets = False
        else:
            if not args.webhook_secret:
                entered = ask_secret("Webhook secret")
                if entered:
                    args.webhook_secret = entered
            args.agent_env = ask(
                "Agent .env path (blank to skip)",
                args.agent_env or None,
            )
            has_secret = bool(
                args.webhook_secret or os.environ.get("DEPLOY_WEBHOOK_SECRET")
            )
            if not has_secret and not args.agent_env:
                args.generate_secret = ask_yes_no(
                    "Generate a new secret? (requires an agent .env path)", False
                )
            if args.generate_secret and not args.agent_env:
                print("  cannot generate without an agent .env path; skipping secrets")
                args.set_secrets = False
            if not (
                args.webhook_secret
                or os.environ.get("DEPLOY_WEBHOOK_SECRET")
                or args.agent_env
                or args.generate_secret
            ):
                print("  no webhook secret available; skipping secrets")
                args.set_secrets = False
    else:
        args.set_secrets = False

    print()
    args.check = ask_yes_no(
        "Check the agent health endpoint?", args.check or bool(args.deploy_url)
    )
    args.make_package_public = ask_yes_no(
        "Make the GHCR package public after the first publish?",
        args.make_package_public,
    )


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def print_next_steps(args, image, project, services, compose_path, workflow_path, root, result):
    url = args.deploy_url or "https://deploy.example.com/deploy"
    try:
        workflow_rel = workflow_path.relative_to(root)
    except ValueError:
        workflow_rel = workflow_path

    print()
    print("Next steps")
    print("----------")
    print()
    if result["workflow"] == "unchanged":
        print("1. The workflow already has the publish and deploy jobs.")
    else:
        print("1. Review and commit the workflow:")
        print(f"     git add {workflow_rel}")
        print('     git commit -m "ci: publish to GHCR and deploy to the NAS"')
    print()

    if result["secrets_set"]:
        print("2. Repository secrets were set with gh:")
        print("     DEPLOY_WEBHOOK_URL, DEPLOY_WEBHOOK_SECRET")
    else:
        print("2. Add the repository secrets (Settings > Secrets and variables > Actions),")
        print("   or use the GitHub CLI:")
        print(f"     gh secret set DEPLOY_WEBHOOK_URL --body {url!r}")
        print("     gh secret set DEPLOY_WEBHOOK_SECRET")
        print("   The secret must match DEPLOY_WEBHOOK_SECRET in the agent's .env.")
        print("   Generate one with: openssl rand -hex 32")
    print()

    if result["projects_status"] in ("updated", "unchanged"):
        print(f"3. projects.json ({result['projects_target']}) contains this project:")
    else:
        print("3. Add this entry to projects.json on the NAS:")
    print(f'     "{project}": {{')
    print(f'       "dir": {json.dumps(args.project_dir or "/volume1/docker/" + project)},')
    print(f'       "services": {services}')
    print("     }")
    if result["projects_status"] == "updated":
        print("   projects.json was updated; the agent reloads it on the next request.")
    elif result["projects_status"] == "unchanged":
        print("   The entry was already present and does not need changing.")
    else:
        print("   The agent reloads projects.json on the next request; no restart needed.")
    print()

    print("4. Check the NAS compose file:")
    for level, message in result["nas_messages"]:
        if level == "ok":
            print("     OK: " + message)
        elif level == "create":
            print("     Created: " + message)
        else:
            print("     ! " + message)
    if not result["nas_messages"]:
        print("     Make sure the project directory's compose.yaml pulls:")
        print(f"       image: {image}:latest")
        if compose_path is None:
            print("     (No compose file found in this repo; edit the one on the NAS.)")
        elif image.split("/", 1)[-1] not in compose_path.read_text(encoding="utf-8"):
            print(f"     (This repo's {compose_path.name} does not reference {image} yet.)")
    print()

    if result["package_public"]:
        print("5. The GHCR package is public.")
    else:
        print("5. Make the GHCR package public, or give the NAS pull credentials.")
        print("   Repo > Packages > the package > Package settings > Change visibility.")
        print("   Private packages: see the agent README's authenticated-pull section.")
    print()

    print("6. Trigger it: push to your deploy branch, or run the workflow manually")
    print("   from the Actions tab (workflow_dispatch is enabled on new workflows).")
    print()
    print("The publish job builds with context %r and Dockerfile %r. Adjust those"
          % (args.context, args.dockerfile))
    print("with --context/--dockerfile if needed. Build target: %s." % args.platform)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Add GHCR publish + NAS deploy jobs to a repo's GitHub Actions workflow."
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="ask for values; any flags passed become the defaults",
    )
    parser.add_argument(
        "--project",
        help="projects.json key / compose project name (default: repository name)",
    )
    parser.add_argument(
        "--image",
        help="image name to publish (default: ghcr.io/<owner>/<repo>)",
    )
    parser.add_argument(
        "--workflow",
        help="workflow file to edit, e.g. ci.yml or .github/workflows/ci.yml",
    )
    parser.add_argument(
        "--branch",
        help="branch that deploys (default: repository default branch)",
    )
    parser.add_argument(
        "--needs",
        metavar="JOB",
        help="make the publish job depend on an existing job, e.g. test",
    )
    parser.add_argument(
        "--deploy-url",
        help="webhook URL, used for secrets, checks, and instructions",
    )
    parser.add_argument(
        "--services",
        help="comma-separated service names for projects.json (default: detected)",
    )
    parser.add_argument(
        "--dockerfile",
        default=None,
        help="Dockerfile path relative to the build context (default: Dockerfile)",
    )
    parser.add_argument(
        "--context",
        default=None,
        help="Docker build context (default: .)",
    )
    parser.add_argument(
        "--platform",
        default=None,
        help="image platform (default: linux/amd64)",
    )
    parser.add_argument(
        "--build-arg",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="build argument passed to docker/build-push-action (repeatable)",
    )
    parser.add_argument(
        "--build-secret",
        action="append",
        default=[],
        metavar="NAME",
        help="expose a repository secret to the build (repeatable)",
    )
    parser.add_argument(
        "--deploy-retries",
        type=int,
        default=None,
        help="attempts when the agent returns 409 (default: 3)",
    )
    parser.add_argument(
        "--no-concurrency",
        action="store_true",
        help="do not add a job-level concurrency group to the deploy job",
    )
    parser.add_argument(
        "--update-projects",
        action="store_true",
        help="add this project to projects.json on the NAS",
    )
    parser.add_argument(
        "--projects-file",
        metavar="TARGET",
        help="projects.json to update: a local path or [user@]host:/absolute/path "
        "(default: $PROJECTS_FILE; implies --update-projects)",
    )
    parser.add_argument(
        "--project-dir",
        metavar="PATH",
        help="compose directory for the projects.json entry "
        "(default: /volume1/docker/<project>)",
    )
    parser.add_argument(
        "--overwrite-project",
        action="store_true",
        help="replace an existing projects.json entry with different values",
    )
    parser.add_argument(
        "--nas-host",
        metavar="HOST",
        help="SSH host for NAS checks and compose scaffolding "
        "(default: derived from --projects-file)",
    )
    parser.add_argument(
        "--create-compose",
        action="store_true",
        help="create the project directory and a minimal compose.yaml if missing",
    )
    parser.add_argument(
        "--set-secrets",
        action="store_true",
        help="set DEPLOY_WEBHOOK_URL and DEPLOY_WEBHOOK_SECRET with gh",
    )
    parser.add_argument(
        "--webhook-secret",
        help="webhook secret for --set-secrets (or set DEPLOY_WEBHOOK_SECRET)",
    )
    parser.add_argument(
        "--generate-secret",
        action="store_true",
        help="generate a new webhook secret; requires --agent-env",
    )
    parser.add_argument(
        "--agent-env",
        metavar="TARGET",
        help="agent .env to read or update: a local path or [user@]host:/absolute/path",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="check the agent's /healthz endpoint after setup",
    )
    parser.add_argument(
        "--make-package-public",
        action="store_true",
        help="make the GHCR package public with gh api (after the first publish)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the result without writing anything",
    )
    args = parser.parse_args()

    interactive = args.interactive or len(sys.argv) == 1

    git_root = run_git(Path.cwd(), "rev-parse", "--show-toplevel")
    root = Path(git_root) if git_root else Path.cwd()
    if root != Path.cwd():
        print(f"using repository root: {root}")
    os.chdir(root)

    slug = github_slug(root)
    if not slug and interactive:
        slug = ask("GitHub repository (owner/repo)", None)
    if not slug:
        print(
            "warning: could not determine the GitHub repository; using owner/repo.\n"
            "         pass --image ghcr.io/<owner>/<repo> to fix it.",
            file=sys.stderr,
        )
        slug = "owner/repo"

    default_image = f"ghcr.io/{slug}"
    default_project = sanitize_project(
        slug.rsplit("/", 1)[-1] if slug != "owner/repo" else root.name
    )
    default_branch = detect_default_branch(root)
    detected_services, compose_path = compose_services(root, args.image or default_image)
    workflow_options = discover_workflows(root)
    if args.workflow:
        default_workflow = args.workflow
    elif workflow_options:
        default_workflow = str(workflow_options[0].relative_to(root))
    else:
        default_workflow = str(DEFAULT_WORKFLOW)

    ctx = {
        "root": root,
        "project": default_project,
        "image": default_image,
        "branch": default_branch,
        "services": detected_services or ["app"],
        "workflow_options": workflow_options,
        "default_workflow": default_workflow,
    }

    if interactive:
        interactive_configure(args, ctx)

    if args.projects_file:
        args.update_projects = True
    if args.set_secrets and not args.deploy_url:
        parser.error("--set-secrets requires --deploy-url (or use -i)")
    if args.check and not args.deploy_url:
        parser.error("--check requires --deploy-url (or use -i)")

    args.project = sanitize_project(args.project or default_project)
    args.image = args.image or default_image
    args.branch = args.branch or default_branch
    args.platform = (args.platform or "linux/amd64").strip()
    args.context = args.context or "."
    args.dockerfile = args.dockerfile or "Dockerfile"
    args.deploy_retries = args.deploy_retries if args.deploy_retries is not None else 3
    if args.deploy_retries < 1:
        parser.error("--deploy-retries must be at least 1")
    if not re.match(r"^[A-Za-z0-9._/-]+$", args.branch):
        parser.error("--branch must be a simple branch name")
    if args.project_dir and not args.project_dir.startswith("/"):
        parser.error("--project-dir must be an absolute path")
    args.build_arg = validate_build_args(args.build_arg)
    args.build_secret = validate_build_secrets(args.build_secret)

    if args.deploy_url and not args.deploy_url.rstrip("/").endswith("/deploy"):
        print(
            "warning: the agent listens on /deploy; --deploy-url should usually "
            "end with /deploy",
            file=sys.stderr,
        )

    if args.services:
        services = [s.strip() for s in args.services.split(",") if s.strip()]
        bad = [s for s in services if not re.match(r"^[A-Za-z0-9_.-]+$", s)]
        if bad:
            parser.error("--services contains an invalid name: " + ", ".join(bad))
    else:
        services = detected_services or ["app"]
    services_json = "[" + ", ".join('"%s"' % s for s in services) + "]"
    args.project_dir = args.project_dir or "/volume1/docker/" + args.project

    # Auto-gate publishing on a test-like job when one is unambiguous.
    chosen_workflow, workflow_error = choose_workflow(root, args.workflow)
    if workflow_error:
        print("error: " + workflow_error, file=sys.stderr)
        return 2
    if chosen_workflow.is_file():
        existing_jobs = workflow_job_names(chosen_workflow.read_text(encoding="utf-8"))
        candidates = [
            job
            for job in existing_jobs
            if job in TEST_JOB_NAMES and job not in ("publish", "deploy")
        ]
        if args.needs is None and len(candidates) == 1:
            args.needs = candidates[0]
            print("gating publishing on job %r (use --needs to override)" % args.needs)
        if args.needs and args.needs not in existing_jobs:
            print(
                "error: no job named %r in %s (jobs: %s)"
                % (
                    args.needs,
                    chosen_workflow.relative_to(root),
                    ", ".join(existing_jobs) or "none",
                ),
                file=sys.stderr,
            )
            return 2
    elif args.needs:
        print(
            "error: --needs requires an existing workflow containing that job",
            file=sys.stderr,
        )
        return 2

    # Plan projects.json.
    projects_target = None
    projects_status = None
    projects_text = None
    if args.update_projects:
        projects_target = (
            args.projects_file or os.environ.get("PROJECTS_FILE", "").strip()
        )
        if not projects_target:
            print(
                "error: --update-projects needs --projects-file, or PROJECTS_FILE "
                "set in the environment",
                file=sys.stderr,
            )
            return 2
        entry = {"dir": args.project_dir, "services": services}
        try:
            projects_text, projects_status = plan_projects_update(
                projects_target, args.project, entry, args.overwrite_project
            )
        except (OSError, RuntimeError) as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1

    # Plan NAS compose work.
    try:
        nas_messages, nas_actions = plan_nas(
            args, args.project, services, args.image
        )
    except (OSError, RuntimeError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    # Resolve secrets and agent .env.
    webhook_secret = None
    agent_env_text = None
    if args.set_secrets or args.agent_env:
        try:
            webhook_secret = resolve_webhook_secret(args)
        except (OSError, RuntimeError) as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1
        if args.set_secrets and not webhook_secret:
            print(
                "error: no webhook secret; pass --webhook-secret, set "
                "DEPLOY_WEBHOOK_SECRET, read it from --agent-env, or use "
                "--generate-secret",
                file=sys.stderr,
            )
            return 1
        if webhook_secret and len(webhook_secret) < 32:
            print(
                "error: the webhook secret must be at least 32 characters",
                file=sys.stderr,
            )
            return 1
        if args.agent_env and webhook_secret:
            host, path = split_remote_target(args.agent_env)
            try:
                original = target_read(host, path) if target_exists(host, path) else ""
            except (OSError, RuntimeError) as exc:
                print("error: %s" % exc, file=sys.stderr)
                return 1
            updated = set_env_value(original, "DEPLOY_WEBHOOK_SECRET", webhook_secret)
            if updated != original:
                agent_env_text = updated

    # Build the workflow text.
    publish, deploy = build_job_texts(args, args.image, args.project)
    exists = chosen_workflow.is_file()
    old_text = chosen_workflow.read_text(encoding="utf-8") if exists else ""
    if exists and not old_text.strip():
        exists = False
        old_text = ""

    if exists:
        new_text, added, existing = add_jobs(old_text, publish, deploy)
        workflow_status = "unchanged" if new_text == old_text else "updated"
    else:
        jobs = publish + "\n\n" + deploy
        new_text = (
            NEW_WORKFLOW.replace("__BRANCH__", args.branch)
            .replace("__JOBS__", jobs)
            .rstrip("\n")
            + "\n"
        )
        added = ["publish", "deploy"]
        existing = []
        workflow_status = "created"

    if exists and not has_push_trigger(new_text.splitlines()):
        print(
            "warning: this workflow has no push trigger; the deploy job only runs "
            "when the workflow runs on the deploy branch.",
            file=sys.stderr,
        )

    if args.dry_run:
        print()
        if workflow_status == "created":
            print("--- dry run: would create %s ---" % chosen_workflow.relative_to(root))
        elif workflow_status == "updated":
            print("--- dry run: would update %s ---" % chosen_workflow.relative_to(root))
        else:
            print("--- dry run: %s is unchanged ---" % chosen_workflow.relative_to(root))
        print(new_text)
        if projects_text is not None:
            print()
            print("--- dry run: would update %s ---" % projects_target)
            print(projects_text, end="")
        elif projects_status == "unchanged":
            print()
            print("%s already has this project; no update needed." % projects_target)
        for action in nas_actions:
            print()
            print("--- dry run: would create %s ---" % action["path"])
            print(action["text"], end="")
        if args.set_secrets:
            print()
            print("--- dry run: would set DEPLOY_WEBHOOK_URL and DEPLOY_WEBHOOK_SECRET ---")
        if agent_env_text is not None:
            print()
            print("--- dry run: would update %s ---" % args.agent_env)
        if args.check:
            print()
            print("--- dry run: would check %s ---" % health_url(args.deploy_url))
        if args.make_package_public:
            print()
            print("--- dry run: would make the GHCR package public ---")
        return 0

    chosen_workflow.parent.mkdir(parents=True, exist_ok=True)
    chosen_workflow.write_text(new_text, encoding="utf-8")
    if workflow_status == "created":
        print("Created %s" % chosen_workflow.relative_to(root))
    elif workflow_status == "updated":
        print(
            "Updated %s (added: %s)"
            % (chosen_workflow.relative_to(root), ", ".join(added))
        )
        for name in ("publish", "deploy"):
            if name in existing and name not in added:
                print("  kept existing job: %s" % name)
    else:
        print("No changes: %s already has the deploy jobs." % chosen_workflow.relative_to(root))

    if projects_text is not None:
        try:
            host, path = split_remote_target(projects_target)
            target_write(host, path, projects_text)
        except (OSError, RuntimeError) as exc:
            print(
                "error: the workflow was written, but %s" % exc,
                file=sys.stderr,
            )
            return 1
        print("Updated %s with project %r" % (projects_target, args.project))

    for action in nas_actions:
        try:
            target_mkdir(action["host"], action["dir"])
            target_write(action["host"], action["path"], action["text"])
        except (OSError, RuntimeError) as exc:
            print(
                "error: the workflow was written, but could not create the compose "
                "file: %s" % exc,
                file=sys.stderr,
            )
            return 1
        print("Created %s" % action["path"])

    if agent_env_text is not None:
        try:
            host, path = split_remote_target(args.agent_env)
            target_write(host, path, agent_env_text)
        except (OSError, RuntimeError) as exc:
            print(
                "error: could not update the agent .env: %s" % exc,
                file=sys.stderr,
            )
            return 1
        print("Updated DEPLOY_WEBHOOK_SECRET in %s" % args.agent_env)

    secrets_set = False
    if args.set_secrets:
        try:
            set_repo_secrets(root, slug, args.deploy_url, webhook_secret)
        except RuntimeError as exc:
            print("error: could not set repository secrets: %s" % exc, file=sys.stderr)
            return 1
        secrets_set = True
        print("Set DEPLOY_WEBHOOK_URL and DEPLOY_WEBHOOK_SECRET on %s" % slug)

    health = None
    if args.check:
        health = check_health(args.deploy_url)
        print(("Health check OK: " if health[0] else "Health check FAILED: ") + health[1])

    package_public = False
    if args.make_package_public:
        try:
            package_public = make_package_public(root, slug, args.image)
            if package_public:
                print("Made the GHCR package public")
        except RuntimeError as exc:
            print("warning: %s" % exc, file=sys.stderr)

    result = {
        "workflow": workflow_status,
        "projects_status": projects_status,
        "projects_target": projects_target,
        "nas_messages": nas_messages,
        "secrets_set": secrets_set,
        "health": health,
        "package_public": package_public,
    }
    print_next_steps(
        args,
        args.image,
        args.project,
        services_json,
        compose_path,
        chosen_workflow,
        root,
        result,
    )
    if health is not None and not health[0]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
