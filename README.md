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

## Setting up an app repo automatically

Rather than wiring up the publish and deploy jobs by hand, run
`setup_auto_deploy.py` from the root of the app repository:

```sh
cd /path/to/app-repo
/path/to/nas-deploy-agent/setup_auto_deploy.py \
  --deploy-url https://deploy.example.com/deploy
```

It adds a `publish` job (GHCR, `:latest` and `:sha-<short>`, `linux/amd64`) and
a `deploy` job to the repo's workflow. If there is no workflow it creates
`.github/workflows/deploy.yml`; if there is one workflow file it appends to it;
if there are several, pass `--workflow ci.yml` to choose.

The deploy job gives each project its own concurrency group so two deploys of
the same project cannot overlap. If the agent returns `409` ("another deploy is
running"), the job retries up to `--deploy-retries` times (default 3).

### Interactive setup

Run the script with no arguments, or with `-i`/`--interactive`, to be prompted
for every value:

```sh
cd /path/to/app-repo
/path/to/nas-deploy-agent/setup_auto_deploy.py -i
```

Any flags you pass alongside `-i` become the defaults for the prompts, so you
can pre-fill the common values and only answer the rest:

```sh
/path/to/nas-deploy-agent/setup_auto_deploy.py -i \
  --project my-app --platform linux/amd64
```

Interactive mode can also update `projects.json`, set repository secrets, write
the secret to the agent's `.env`, scaffold the NAS compose file, and run the
health check. Press Ctrl-C or Ctrl-D to cancel without writing anything.

### Flags

| Flag | Purpose |
|---|---|
| `--project NAME` | `projects.json` key / compose project name (default: repo name) |
| `--image IMAGE` | Override `ghcr.io/<owner>/<repo>` |
| `--branch NAME` | Branch that deploys (default: the repo's default branch) |
| `--needs JOB` | Make publishing wait for an existing job such as `test` (auto-detected when exactly one test-like job exists) |
| `--workflow PATH` | Workflow to edit, e.g. `ci.yml` |
| `--platform PLATFORM` | Image platform (default `linux/amd64`) |
| `--context PATH` | Docker build context (default `.`) |
| `--dockerfile PATH` | Dockerfile path relative to the context (default `Dockerfile`) |
| `--build-arg KEY=VALUE` | Build argument for the image build (repeatable) |
| `--build-secret NAME` | Repository secret exposed to the build (repeatable) |
| `--deploy-retries N` | Attempts when the agent returns `409` (default 3) |
| `--no-concurrency` | Do not add a concurrency group to the deploy job |
| `--services a,b` | Services for the `projects.json` snippet (default: the compose service that uses the image) |
| `--update-projects` | Add this project to `projects.json` |
| `--projects-file TARGET` | Local path or `[user@]host:/absolute/path` to update (implies `--update-projects`; defaults to `$PROJECTS_FILE`) |
| `--project-dir PATH` | Compose directory for the entry (default `/volume1/docker/<project>`) |
| `--overwrite-project` | Replace an existing entry that has different values |
| `--nas-host HOST` | SSH host for NAS checks and compose scaffolding (default: derived from `--projects-file`) |
| `--create-compose` | Create the project directory and a minimal `compose.yaml` if missing |
| `--set-secrets` | Set `DEPLOY_WEBHOOK_URL` and `DEPLOY_WEBHOOK_SECRET` with `gh` |
| `--webhook-secret VALUE` | Secret for `--set-secrets` (or set `DEPLOY_WEBHOOK_SECRET`) |
| `--generate-secret` | Generate a new secret; requires `--agent-env` |
| `--agent-env TARGET` | Agent `.env` to write the secret to: local path or `[user@]host:/absolute/path` |
| `--check` | Check the agent's `/healthz` endpoint and exit non-zero on failure |
| `--make-package-public` | Make the GHCR package public with `gh api` (only for images safe to publish) |
| `--dry-run` | Print the result without writing anything |

The secret must be supplied: `--webhook-secret`, `DEPLOY_WEBHOOK_SECRET` in the
environment, or `--generate-secret`. The script no longer reads the secret from
the agent's `.env`. `--generate-secret` requires `--agent-env` so the agent and
the repository end up with the same value, and `--agent-env` is used to write
that value where the agent can read it. A secret must be at least 32
characters.

`--make-package-public` is only for images that are safe to publish. If the
repository is private, keep the package private and give the agent GHCR pull
credentials instead (see *Private images* below); the script warns but does not
require the package to be public.

### Updating the NAS over SSH

To update the allowlist and scaffold the compose file on the NAS while setting
up the repo:

```sh
cd /path/to/app-repo
/path/to/nas-deploy-agent/setup_auto_deploy.py \
  --deploy-url https://deploy.example.com/deploy \
  --projects-file nas:/volume1/docker/deploy/projects.json \
  --agent-env nas:/volume1/docker/deploy/.env \
  --set-secrets --create-compose
```

Replace `nas` with your SSH host or `user@host`. Local paths also work if the
NAS share is mounted. The script refuses to overwrite an existing `projects.json`
entry with different values unless `--overwrite-project` is given.

With `--create-compose`, a missing project directory is created and a
`compose.yaml` is written there. If the repo has a `compose.yaml`, the NAS file
is seeded from it: ports, environment, volumes, and other service settings are
kept, while `build:` is removed and `image:` is pointed at the published image.
Without a repo compose file, a minimal one-service file is written. If a NAS
compose file already exists but does not reference the image, the script warns
instead of editing it.

The script never prints or embeds the webhook secret in the workflow. It prints
the remaining steps: the repository secrets, the `projects.json` entry, the
image the host's compose file should pull, and how to make the package public.

The publish job assumes a Dockerfile at the repository root; use
`--context`/`--dockerfile` if yours lives elsewhere. The manual publish and
deploy jobs are described in the next two sections.

## Publishing an image to GHCR

The agent never builds anything. It only runs `docker compose pull` on the host,
so the image referenced by each project's `compose.yaml` must already exist in a
registry the host can reach. GitHub Container Registry (GHCR) is the natural
choice here: Actions pushes with the built-in `GITHUB_TOKEN`, and a public
package pulls anonymously, so the host needs no registry credentials.

Requirements for the published image:

- Build `linux/amd64` (or whatever your host runs). An ARM-only image fails at
  `pull` or `up` time.
- Push a movable `:latest`, which is what `compose.yaml` should follow, plus an
  immutable `:sha-<short>` for rollback.
- Grant the job `packages: write`. `GITHUB_TOKEN` is enough; no PAT is needed.
- Make the package **public** if the host should pull without credentials:
  repo → *Packages* → the package → *Package settings* → *Change visibility*.
  Otherwise log the host in to `ghcr.io` first.

A complete publish workflow:

```yaml
name: Publish

on:
  push:
    branches: [main]

permissions:
  contents: read
  packages: write

jobs:
  publish:
    name: Publish image
    runs-on: ubuntu-latest
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
          images: ghcr.io/${{ github.repository }}
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
          cache-to: type=gha,mode=max
```

Then point the project at it:

```yaml
services:
  app:
    image: ghcr.io/your-org/my-app:latest
```

Add a version tag to the metadata action if you cut releases
(`type=raw,value=${{ steps.version.outputs.value }}` or `type=semver,pattern={{version}}`).
Image names must be lowercase.

If the same workflow runs tests, gate publishing on them (`needs: test`) so a
broken build never replaces `:latest`.

### Private images (authenticated pulls)

A package attached to a **private** repo is private too, so the host must
authenticate before `docker compose pull` can fetch it. The agent mounts
`./docker` from its project directory at `/root/.docker`, which is where the
Docker CLI looks for `config.json`.

1. Create a token that can read packages: a **classic** PAT with `read:packages`
   (add `repo` if the package is linked to a private repository). Fine-grained
   tokens do not currently work for GHCR.
2. Write the config on the host, then make it owner-only:

   ```sh
   mkdir -p /volume1/docker/nas-deploy-agent/docker
   umask 077
   AUTH=$(printf '%s:%s' dogeared "$GHCR_TOKEN" | base64)
   printf '{"auths":{"ghcr.io":{"auth":"%s"}}}\n' "$AUTH" \
     > /volume1/docker/nas-deploy-agent/docker/config.json
   ```

   `docker login ghcr.io` on another machine writes the same structure; you can
   copy that file instead.
3. Recreate the agent so the mount takes effect:
   `docker compose -p nas-deploy-agent up -d --build`.

Verify without involving the agent:

```sh
echo "$GHCR_TOKEN" | docker login ghcr.io -u dogeared --password-stdin
docker pull ghcr.io/your-org/private-app:latest
```

Rotating the token is just rewriting `config.json`; no agent restart is needed
because Docker reads the file on each pull.

## Triggering the deploy from GitHub Actions

After the image is published, call the agent from the app repo. Keep the
secrets in the repo, not in the workflow. This job must run after the publish
job above (`needs: publish`):

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
