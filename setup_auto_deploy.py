#!/usr/bin/env python3
"""Wire an app repository up for nas-deploy-agent auto-deploys.

Run this from the root of the app repository:

    cd /path/to/app-repo
    /path/to/nas-deploy-agent/setup_auto_deploy.py

It adds two jobs to a GitHub Actions workflow:

  publish  builds a linux/amd64 image and pushes :latest plus :sha-<short> to
           GitHub Container Registry using the built-in GITHUB_TOKEN.
  deploy   calls the NAS webhook after publish succeeds, with an HMAC signature.

If the repository has no workflow, a complete one is created at
.github/workflows/deploy.yml. If it has exactly one workflow, the jobs are
appended to it; with more than one, pass --workflow to pick one explicitly.

The script never stores the webhook URL or secret in the workflow. When done it
prints the manual steps: repository secrets, the projects.json entry, and the
image reference the NAS should pull.

Only the Python standard library is used.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

WORKFLOW_DIR = Path(".github/workflows")
DEFAULT_WORKFLOW = WORKFLOW_DIR / "deploy.yml"

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
          context: .
          platforms: linux/amd64
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha
          cache-to: type=gha,mode=max"""

DEPLOY_JOB = """\
  deploy:
    name: Deploy
    runs-on: ubuntu-latest
    needs: publish
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
          curl -fsS -X POST "$DEPLOY_WEBHOOK_URL" \\
            -H 'Content-Type: application/json' \\
            -H "X-Deploy-Timestamp: $TS" \\
            -H "X-Deploy-Signature: sha256=$SIG" \\
            --data "$BODY"
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


def sanitize_project(name):
    """Docker Compose project names are lowercase [a-z0-9][a-z0-9_-]*."""
    name = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")
    return name or "app"


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


# --------------------------------------------------------------------------
# Workflow editing
# --------------------------------------------------------------------------


def build_job_texts(args, image, project):
    run_if = "github.ref == 'refs/heads/%s'" % args.branch
    needs = "    needs: %s\n" % args.needs if args.needs else ""

    publish = (
        PUBLISH_JOB.replace("__NEEDS__", needs)
        .replace("__IF__", run_if)
        .replace("__IMAGE__", image)
    )
    deploy = DEPLOY_JOB.replace("__IF__", run_if).replace("__PROJECT__", project)
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

    # Handle `jobs: {}` / `jobs: null` by turning it into a block mapping.
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

    if not WORKFLOW_DIR.is_dir():
        return (root / DEFAULT_WORKFLOW).resolve(), None

    found = sorted(
        p.resolve()
        for p in WORKFLOW_DIR.iterdir()
        if p.suffix in (".yml", ".yaml")
    )
    if not found:
        return (root / DEFAULT_WORKFLOW).resolve(), None
    if len(found) == 1:
        return found[0].resolve(), None
    listing = "\n".join("  " + str(p.relative_to(root)) for p in found)
    return None, (
        "found multiple workflow files; pass --workflow to choose one:\n" + listing
    )


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def print_next_steps(args, image, project, services, compose_path, workflow_path, root):
    url = args.deploy_url or "https://deploy.example.com/deploy"
    try:
        workflow_rel = workflow_path.relative_to(root)
    except ValueError:
        workflow_rel = workflow_path

    print()
    print("Next steps")
    print("----------")
    print()
    print("1. Review and commit the workflow:")
    print(f"     git add {workflow_rel}")
    print('     git commit -m "ci: publish to GHCR and deploy to the NAS"')
    print()
    print("2. Add the repository secrets (Settings > Secrets and variables > Actions),")
    print("   or use the GitHub CLI:")
    print(f"     gh secret set DEPLOY_WEBHOOK_URL --body {url!r}")
    print("     gh secret set DEPLOY_WEBHOOK_SECRET")
    print("   The secret must be the same value as DEPLOY_WEBHOOK_SECRET in the")
    print("   agent's .env. Generate one with: openssl rand -hex 32")
    print()
    print("3. Add this entry to projects.json on the NAS:")
    print(f'     "{project}": {{')
    print(f'       "dir": "/volume1/docker/{project}",')
    print(f'       "services": {services}')
    print("     }")
    print("   The agent reloads projects.json on the next request; no restart needed.")
    print()
    print("4. Make sure that directory's compose.yaml pulls the published image:")
    print(f"     image: {image}:latest")
    if compose_path is None:
        print("   (No compose file found in this repo; edit the one on the NAS.)")
    elif image.split("/", 1)[-1] not in compose_path.read_text(encoding="utf-8"):
        print(f"   (This repo's {compose_path.name} does not reference {image} yet.)")
    print()
    print("5. Make the GHCR package public, or give the NAS pull credentials.")
    print("   Repo > Packages > the package > Package settings > Change visibility.")
    print("   Private packages: see the agent README's authenticated-pull section.")
    print()
    print("6. Trigger it: push to your deploy branch, or run the workflow manually")
    print("   from the Actions tab (workflow_dispatch is enabled on new workflows).")
    print()
    print("The publish job assumes a Dockerfile at the repository root. If your")
    print("Dockerfile lives elsewhere, adjust context/file in the build step.")
    if not args.needs:
        print()
        print("If this workflow already runs tests, rerun with --needs <job> to make")
        print("publishing wait for them.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Add GHCR publish + NAS deploy jobs to a repo's GitHub Actions workflow."
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
        default="main",
        help="branch that deploys (default: main)",
    )
    parser.add_argument(
        "--needs",
        metavar="JOB",
        help="make the publish job depend on an existing job, e.g. test",
    )
    parser.add_argument(
        "--deploy-url",
        help="webhook URL to show in the setup instructions",
    )
    parser.add_argument(
        "--services",
        help="comma-separated service names for projects.json (default: detected)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the result without writing anything",
    )
    args = parser.parse_args()

    git_root = run_git(Path.cwd(), "rev-parse", "--show-toplevel")
    root = Path(git_root) if git_root else Path.cwd()
    if root != Path.cwd():
        print(f"using repository root: {root}")
    os.chdir(root)

    if not re.match(r"^[A-Za-z0-9._/-]+$", args.branch):
        parser.error("--branch must be a simple branch name")

    if args.deploy_url and not args.deploy_url.rstrip("/").endswith("/deploy"):
        print(
            "warning: the agent listens on /deploy; --deploy-url should usually "
            "end with /deploy",
            file=sys.stderr,
        )

    slug = github_slug(root)
    if not slug:
        print(
            "warning: could not determine the GitHub repository; using owner/repo.\n"
            "         pass --image ghcr.io/<owner>/<repo> to fix it.",
            file=sys.stderr,
        )
        slug = "owner/repo"
    image = args.image or f"ghcr.io/{slug}"
    project = sanitize_project(args.project or slug.rsplit("/", 1)[-1])

    detected_services, compose_path = compose_services(root, image)
    if args.services:
        services = [s.strip() for s in args.services.split(",") if s.strip()]
        bad = [s for s in services if not re.match(r"^[A-Za-z0-9_.-]+$", s)]
        if bad:
            parser.error("--services contains an invalid name: " + ", ".join(bad))
    else:
        services = detected_services or ["app"]
    services_json = "[" + ", ".join('"%s"' % s for s in services) + "]"

    workflow_path, error = choose_workflow(root, args.workflow)
    if error:
        print("error: " + error, file=sys.stderr)
        return 2

    try:
        display_path = workflow_path.relative_to(root)
    except ValueError:
        display_path = workflow_path

    exists = workflow_path.is_file()
    old_text = workflow_path.read_text(encoding="utf-8") if exists else ""
    if exists and not old_text.strip():
        exists = False
        old_text = ""

    if args.needs:
        if not exists:
            print(
                "error: --needs requires an existing workflow containing that job",
                file=sys.stderr,
            )
            return 2
        names = workflow_job_names(old_text)
        if args.needs not in names:
            print(
                "error: no job named %r in %s (jobs: %s)"
                % (args.needs, display_path, ", ".join(names) or "none"),
                file=sys.stderr,
            )
            return 2

    publish, deploy = build_job_texts(args, image, project)

    if exists:
        new_text, added, existing = add_jobs(old_text, publish, deploy)
        if new_text == old_text:
            print(f"No changes: {display_path} already has the deploy jobs.")
        else:
            print(f"Updated {display_path}")
            if added:
                print("  added jobs: " + ", ".join(added))
        if "publish" in existing and "publish" not in added:
            print("  kept existing job: publish")
        if "deploy" in existing and "deploy" not in added:
            print("  kept existing job: deploy")
        if not has_push_trigger(new_text.splitlines()):
            print(
                "warning: this workflow has no push trigger; the deploy job only runs "
                "when the workflow runs on the deploy branch.",
                file=sys.stderr,
            )
    else:
        jobs = publish + "\n\n" + deploy
        new_text = (
            NEW_WORKFLOW.replace("__BRANCH__", args.branch)
            .replace("__JOBS__", jobs)
            .rstrip("\n")
            + "\n"
        )
        print(f"Created {display_path}")

    if args.dry_run:
        print()
        print("--- dry run: would write ---")
        print(new_text)
        return 0

    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(new_text, encoding="utf-8")
    print_next_steps(args, image, project, services_json, compose_path, workflow_path, root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
