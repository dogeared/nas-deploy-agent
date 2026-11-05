#!/usr/bin/env python3
"""Deploy webhook for a Docker host.

POST /deploy with a JSON body {"project": "<name>"} and two headers:

    X-Deploy-Timestamp: <unix seconds>
    X-Deploy-Signature: sha256=<hex HMAC of "<timestamp>.<raw body>" with the shared secret>

The project must be enabled in the file named by the PROJECTS_FILE environment
variable (required; there is no default). The agent then runs, in that project's
directory:

    docker compose -p <project> pull <its services>
    docker compose -p <project> up -d --remove-orphans

It holds the Docker socket, so it is effectively root on the host. The only
path in should be a tunnel or reverse-proxy hostname; the HMAC is what
authorises a deploy. Keep the secret in .env (mode 600) beside compose.yaml and
in each app repo as the DEPLOY_WEBHOOK_SECRET Actions secret.

Nothing here builds an image: GitHub Actions does that and pushes to GHCR, and
this only makes the NAS pick the new tag up immediately instead of on the next
timer tick.
"""

import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = os.environ.get("DEPLOY_WEBHOOK_SECRET", "")
PORT = int(os.environ.get("PORT", "9000"))
# Required: set in .env beside compose.yaml. No default, so a misconfigured
# deployment fails at startup with a clear message instead of guessing a path.
PROJECTS_FILE = os.environ.get("PROJECTS_FILE", "").strip()
MAX_SKEW_SECONDS = 300
MAX_BODY_BYTES = 8192
COMPOSE_TIMEOUT = 900

# The project list is external and its path is required from .env, so the
# webhook and any timer script read one file. The shape is:
#
#   {
#     "find-my-game-parts": {
#       "dir": "/volume1/docker/find-my-game-parts",
#       "services": ["migrate", "app"]
#     }
#   }
#
# `services` are the ones carrying the app image; pulling only those keeps the
# timer off Docker Hub (Postgres/MySQL would otherwise be re-checked and can hit
# its anonymous rate limit). `up -d` still touches the whole project, so
# one-shot services such as find-my-game-parts' `migrate` run when the app image
# changes. `"enabled": false` keeps an entry documented but skipped.

_config_lock = threading.Lock()
_config = {"mtime": None, "projects": {}}


def load_projects(path):
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError("top level must be an object keyed by project name")

    projects = {}
    for name, cfg in raw.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"{name}: entry must be an object")
        if cfg.get("enabled", True) is False:
            continue
        directory = cfg.get("dir")
        services = cfg.get("services", [])
        if not isinstance(directory, str) or not directory.startswith("/"):
            raise ValueError(f"{name}: dir must be an absolute path")
        if not isinstance(services, list) or not all(isinstance(s, str) for s in services):
            raise ValueError(f"{name}: services must be a list of strings")
        projects[name] = {"dir": directory, "services": services}
    return projects


def get_projects():
    """Read projects.json, reloading when it changes.

    An edit takes effect on the next request, no restart needed. A broken edit
    keeps the last good list rather than silently disabling deploys.
    """
    try:
        mtime = os.stat(PROJECTS_FILE).st_mtime_ns
    except OSError as exc:
        log(f"cannot stat {PROJECTS_FILE}: {exc}; keeping the last good config")
        return _config["projects"]

    with _config_lock:
        if mtime != _config["mtime"]:
            try:
                projects = load_projects(PROJECTS_FILE)
            except (OSError, ValueError) as exc:
                log(f"bad {PROJECTS_FILE}: {exc}; keeping the last good config")
                return _config["projects"]
            _config["projects"] = projects
            _config["mtime"] = mtime
            names = ", ".join(sorted(projects)) or "-"
            log(f"loaded {len(projects)} project(s): {names}")
        return _config["projects"]

deploy_lock = threading.Lock()


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def run_compose(project, target):
    """Pull the app services, then apply the project. Returns (rc, output)."""
    base = ["docker", "compose", "-p", project]
    steps = [
        [*base, "pull", *target["services"]],
        [*base, "up", "-d", "--remove-orphans"],
    ]
    chunks = []
    for cmd in steps:
        log(f"{project}: {' '.join(cmd[3:])}")
        proc = subprocess.run(
            cmd,
            cwd=target["dir"],
            capture_output=True,
            text=True,
            timeout=COMPOSE_TIMEOUT,
        )
        chunks.append(f"$ {' '.join(cmd[3:])}\n{proc.stdout}{proc.stderr}")
        if proc.returncode != 0:
            return proc.returncode, "\n".join(chunks)
    return 0, "\n".join(chunks)


class Handler(BaseHTTPRequestHandler):
    server_version = "nas-deploy-agent/1"

    def do_GET(self):
        if self.path == "/healthz":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/deploy":
            self._json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(400, {"error": "bad body"})
            return

        body = self.rfile.read(length)
        if not self._authorized(body):
            log(f"rejected unsigned request from {self.client_address[0]}")
            self._json(401, {"error": "bad signature"})
            return

        try:
            payload = json.loads(body)
            project = payload["project"]
        except (ValueError, KeyError, TypeError):
            self._json(400, {"error": "expected {\"project\": \"<name>\"}"})
            return

        target = get_projects().get(project)
        if target is None:
            self._json(404, {"error": "unknown project"})
            return

        if not deploy_lock.acquire(blocking=False):
            self._json(409, {"error": "another deploy is running"})
            return

        started = time.time()
        try:
            rc, output = run_compose(project, target)
        except subprocess.TimeoutExpired:
            rc, output = 124, f"compose timed out after {COMPOSE_TIMEOUT}s"
        except FileNotFoundError as exc:
            rc, output = 127, f"cannot run docker compose: {exc}"
        finally:
            deploy_lock.release()

        took = time.time() - started
        log(f"{project}: {'ok' if rc == 0 else 'FAILED rc=' + str(rc)} in {took:.1f}s")
        self._json(
            200 if rc == 0 else 500,
            {
                "project": project,
                "ok": rc == 0,
                "seconds": round(took, 1),
                "output": output[-4000:],
            },
        )

    def _authorized(self, body):
        timestamp = self.headers.get("X-Deploy-Timestamp", "")
        signature = self.headers.get("X-Deploy-Signature", "")
        try:
            sent_at = int(timestamp)
        except ValueError:
            return False
        if abs(time.time() - sent_at) > MAX_SKEW_SECONDS:
            return False
        expected = "sha256=" + hmac.new(
            SECRET.encode(), timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        log(f"{self.client_address[0]} {fmt % args}")


def main():
    if not PROJECTS_FILE:
        log(
            "FAIL: PROJECTS_FILE is not set; set it in .env to the absolute path "
            "of projects.json"
        )
        sys.exit(1)
    if not os.path.isfile(PROJECTS_FILE):
        log(f"FAIL: PROJECTS_FILE does not exist: {PROJECTS_FILE}")
        sys.exit(1)
    if len(SECRET) < 32:
        log("FAIL: DEPLOY_WEBHOOK_SECRET must be set to at least 32 characters")
        sys.exit(1)
    projects = get_projects()
    if not projects:
        log(f"FAIL: no enabled projects in {PROJECTS_FILE}")
        sys.exit(1)
    for name, target in projects.items():
        if not os.path.isfile(os.path.join(target["dir"], "compose.yaml")):
            log(f"warning: {name} has no compose.yaml in {target['dir']}")

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"listening on :{PORT} for {', '.join(sorted(projects))}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
