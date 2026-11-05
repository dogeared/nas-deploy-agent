# nas-deploy-agent

A small, dependency-free webhook that deploys Docker Compose projects on a
Synology NAS (or any Docker host) when a signed request arrives. It is the last
hop in a CI pipeline:

```
merge → build image → push to registry → POST /deploy → compose pull + up -d
```

No polling, no inbound port, no source checkout on the host. The agent does not
build anything; it only makes the host resolve a new image tag and reapply one
project.

## Why this exists

Render-style auto-deploy needs something on the host to receive a "deploy now".
A NAS behind NAT has no public ingress. Polling works but adds minutes of lag.
This agent sits on a private Docker network, is exposed through an existing
Cloudflare tunnel (or any reverse proxy) under one hostname, and authenticates
every request with an HMAC — so CI gets a push-based deploy without opening a
port.

## How it works

```
GitHub Actions
  └─ POST https://deploy.example.com/deploy
       X-Deploy-Timestamp: 1762300000
       X-Deploy-Signature: sha256=<hmac>
       {"project": "my-app"}
            │
            ▼
  nas-deploy-agent
    ├─ verify signature and timestamp
    ├─ look up "my-app" in projects.json (allowlist)
    └─ docker compose -p my-app pull <services>
       docker compose -p my-app up -d --remove-orphans
```

Both commands are idempotent: `pull` resolves `:latest` to a digest and `up -d`
only recreates containers whose image or config changed, so a deploy with
nothing new is cheap and harmless.

## Security model

The container mounts `/var/run/docker.sock`, so **anyone who can call it can run
arbitrary containers as root on the host**. Treat it as a root service.

- Every request is signed with HMAC-SHA256 over `<unix timestamp>.<raw body>`.
  The signature is compared in constant time and requests older than 5 minutes
  are rejected.
- The request body carries only a project name. The directory and services come
  from the allowlist in `projects.json`; no path or command is taken from the
  request.
- Only one deploy runs at a time; a concurrent request gets `409`.
- Bodies are capped at 8 KiB; compose has a 15-minute timeout.
- The published port is bound to `127.0.0.1` for smoke tests. Public traffic
  should reach the container through the tunnel/reverse proxy, never directly.
- There is no TLS or user auth beyond the HMAC: terminate TLS at the tunnel. For
  defence in depth, put Cloudflare Access (or equivalent) in front with a
  service token.

If a host is compromised, the agent adds no new capability — it already has the
Docker socket. Its job is to make sure only someone holding the shared secret
can ask it to do anything.

## Quick start

```sh
git clone https://github.com/dogeared/nas-deploy-agent.git
cd nas-deploy-agent
cp .env.example .env          # set PROJECTS_FILE and DEPLOY_WEBHOOK_SECRET
cp projects.example.json /volume1/docker/deploy/projects.json   # then edit
docker compose up -d --build
curl -s http://127.0.0.1:19000/healthz
```

Both variables in `.env` are required. Without `PROJECTS_FILE` the agent logs a
failure and exits; without a 32+ character `DEPLOY_WEBHOOK_SECRET` it does the
same. Compose also refuses to start if either is missing.

### On a Synology

1. Copy this directory to `/volume1/docker/nas-deploy-agent` and create `.env`
   there (mode 600).
2. Container Manager → **Project → Create**, name `nas-deploy-agent`, path
   `/volume1/docker/nas-deploy-agent`, source *Use existing docker-compose.yml*,
   then Build.
3. Point the tunnel's public hostname at `http://nas-deploy-agent:9000`. The
   container joins the external network named in `DEPLOY_NETWORK` (default
   `nas-shared`), which the tunnel connector also joins.

The Docker socket on DSM is root-only; the container runs as root, so it works
without extra group configuration.

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `PROJECTS_FILE` | **yes** | — | Absolute path to the project list. No default: a misconfigured deployment fails at startup. |
| `DEPLOY_WEBHOOK_SECRET` | **yes** | — | Shared HMAC secret, 32+ characters (`openssl rand -hex 32`). |
| `PORT` | no | `9000` | Listen port inside the container. |
| `DOCKER_ROOT` | no | `/volume1/docker` | Compose only. Host path mounted at the same path in the container. |
| `DEPLOY_NETWORK` | no | `nas-shared` | Compose only. External network shared with the tunnel/reverse proxy. |

`PROJECTS_FILE` and every `dir` in the project list are interpreted **inside the
container**. Because `DOCKER_ROOT` is mounted at the identical path, the same
absolute paths work on the host and in the container.

## Project list

`projects.json` is the allowlist. The agent reloads it on change (checked per
request) and keeps the last good copy if an edit is invalid.

```json
{
  "my-app": {
    "dir": "/volume1/docker/my-app",
    "services": ["app"]
  },
  "paused-app": {
    "dir": "/volume1/docker/paused-app",
    "services": ["app"],
    "enabled": false
  }
}
```

- `dir` — the Compose project directory (must contain `compose.yaml`/`.env`).
- `services` — only these are pulled. Keep it to services that carry a registry
  image; pulling `postgres`/`mysql` on every deploy can hit Docker Hub's
  anonymous rate limit. `up -d` still applies the whole project, so one-shot
  services (migrations) run too.
- `enabled` — `false` keeps an entry documented but ignored. Defaults to true.

## API

### `GET /healthz`

Returns `{"status": "ok"}`. Does not check the project list.

### `POST /deploy`

Headers:

| Header | Value |
|---|---|
| `X-Deploy-Timestamp` | Unix seconds; rejected if more than 300 seconds off |
| `X-Deploy-Signature` | `sha256=` + hex HMAC-SHA256 of `<timestamp>.<raw body>` |

Body: `{"project": "<name>"}` (extra fields are ignored).

```sh
URL=http://127.0.0.1:19000/deploy
BODY='{"project":"my-app"}'
TS=$(date +%s)
SIG=$(python3 -c 'import hashlib,hmac,sys; print(hmac.new(sys.argv[1].encode(), sys.argv[2].encode()+b"."+sys.argv[3].encode(), hashlib.sha256).hexdigest())' "$DEPLOY_WEBHOOK_SECRET" "$TS" "$BODY")
curl -sS -X POST "$URL" \
  -H 'Content-Type: application/json' \
  -H "X-Deploy-Timestamp: $TS" \
  -H "X-Deploy-Signature: sha256=$SIG" \
  --data "$BODY"
```

Responses:

| Code | Meaning |
|---|---|
| `200` | Compose succeeded. Body: `{"project", "ok": true, "seconds", "output"}` |
| `400` | Missing/oversized body or bad JSON |
| `401` | Bad signature or stale timestamp |
| `404` | Unknown path or project not enabled in `projects.json` |
| `409` | Another deploy is in progress |
| `500` | Compose failed or timed out. Body has `"ok": false` and recent output |

## GitHub Actions

After the image is published, call the agent from the app repo. Keep the
secrets in the repo, not in the workflow:

```yaml
  deploy:
    name: Deploy
    runs-on: ubuntu-latest
    needs: publish
    if: github.ref == 'refs/heads/main'
    steps:
      - name: Ask the NAS to deploy
        env:
          DEPLOY_WEBHOOK_URL: ${{ secrets.DEPLOY_WEBHOOK_URL }}
          DEPLOY_WEBHOOK_SECRET: ${{ secrets.DEPLOY_WEBHOOK_SECRET }}
          PROJECT: my-app
        run: |
          BODY=$(printf '{"project":"%s","sha":"%s"}' "$PROJECT" "$GITHUB_SHA")
          TS=$(date +%s)
          SIG=$(python3 -c 'import hashlib,hmac,sys; print(hmac.new(sys.argv[1].encode(), sys.argv[2].encode()+b"."+sys.argv[3].encode(), hashlib.sha256).hexdigest())' "$DEPLOY_WEBHOOK_SECRET" "$TS" "$BODY")
          curl -fsS -X POST "$DEPLOY_WEBHOOK_URL" \
            -H 'Content-Type: application/json' \
            -H "X-Deploy-Timestamp: $TS" \
            -H "X-Deploy-Signature: sha256=$SIG" \
            --data "$BODY"
```

If the webhook is not configured yet, guard the step: `if [ -z "$DEPLOY_WEBHOOK_URL" ]; then echo "not configured"; exit 0; fi`.

## Running without Docker

The agent uses only the standard library (Python 3.8+). It shells out to
`docker compose`, so that must be on `PATH` and able to reach the same Docker
daemon:

```sh
export PROJECTS_FILE=/volume1/docker/deploy/projects.json
export DEPLOY_WEBHOOK_SECRET=$(openssl rand -hex 32)
python3 agent.py
```

## Logs and troubleshooting

The agent logs to stdout: startup config, accepted/rejected requests, and the
compose output for each deploy. With Compose:

```sh
docker compose logs -f agent
```

- `FAIL: PROJECTS_FILE is not set` — add it to `.env` (and recreate the
  container; environment is read at start).
- `rejected unsigned request` — the caller's secret or signature construction
  does not match.
- `409 another deploy is running` — retry after the current one finishes; CI
  jobs usually retry a few times.
- `401` despite a correct secret — check the clock; the skew limit is 5 minutes.

## Versioning

Semantic version tags (`v0.0.1`, …). The running version is in `VERSION`.

## License

MIT — see [LICENSE](LICENSE).
