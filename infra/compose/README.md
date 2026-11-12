# `infra/compose/`: RECAP with durable workers, in Docker Compose (M6 W3)

> **Status: built and verified with real containers on 2026-11-08** (macOS on Apple silicon,
> colima 0.10.3 with 4 CPUs, 4 GB and 20 GB, Docker Engine 29.5.2, Docker Compose 5.7.0;
> linux/arm64 images). Every service became healthy; a queued incident was investigated,
> approved by the operator and verified recovered; idempotency, cancellation, a worker
> restart and a worker kill, a Redis restart and `down`/`up` persistence all behaved as
> designed; the browser smoke test passed against it. The evidence is in
> [docs/m6-progress.md](../../docs/m6-progress.md) (W3 final verification). Not verified:
> building or running on linux/amd64. `tests/unit/test_compose_files.py` also checks these
> files offline (ports, privileges, health checks, volumes, pins, no secrets).

Docker is **optional**. Without it RECAP runs as it always has (the repository README):
`uvicorn apps.api.main:app` executes incidents in the API process (the default in-process
backend), and no Redis or worker is involved.

## What it runs

| Service | Image | Port (127.0.0.1 only) | What it does |
|---|---|---|---|
| `redis` | `redis:7.4.2-alpine` | not published | the job queue; append-only file, fsync every second |
| `api` | `recap:local` (built) | 8000 (`RECAP_API_PORT`) | the FastAPI backend; queues runs (`RECAP_EXECUTION_BACKEND=redis`) |
| `worker` | `recap:local` (built) | none | `python -m apps.worker`: claims runs and executes them through RECAP's own harness |
| `dashboard` | `recap-dashboard:local` (built) | 8080 | Mission Control (nginx; `/api/*` is proxied to the API) |
| `prometheus` | `prom/prometheus:v2.55.1` | 9090 | only with `--profile metrics` and `RECAP_METRICS=1` |

Offline by default: the simulated environment and the scripted policy (or a committed
recording's replay). No cloud, no Kubernetes, no Ollama, no key, no paid call.

Data lives in named volumes: `recap-data` (runs, traces, approvals, simulator state, the
operator token: the API and the workers share it), `redis-data` (the queue) and
`prometheus-data`. Persisted run records stay the source of truth; Redis only coordinates.

Hardening, for every service: a non-root user, every Linux capability dropped,
`no-new-privileges`, a read-only root filesystem with a small `/tmp` tmpfs, and health checks.
Startup order: Redis healthy, then the API and the worker; the dashboard and Prometheus after
the API is healthy. Redis is reachable only on the project's own network (no password: nothing
else can reach it; do not publish its port).

## Setting up Docker on a Mac (colima), as verified

```bash
brew install colima docker docker-compose        # colima and docker were already installed here
# Let the Docker CLI find the Compose plugin: add this key to ~/.docker/config.json, keeping
# every other setting:   "cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]
colima start recap --cpu 4 --memory 4 --disk 20  # a dedicated VM profile; 4 GB was enough
docker compose version                           # Docker Compose version 5.7.0
```

Two things this machine needed, which another Mac may too:

- **A Docker Desktop leftover.** If `docker pull` fails with `docker-credential-desktop:
  executable file not found`, `~/.docker/config.json` still names Docker Desktop's credential
  helper (`"credsStore": "desktop"`) after Docker Desktop was removed. Either remove that line
  (nothing is lost when `auths` is empty), or keep it and give these commands a private config
  that names no helper:
  `mkdir -p ~/.docker-recap && echo '{"cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]}' > ~/.docker-recap/config.json`
  and `export DOCKER_CONFIG=~/.docker-recap DOCKER_HOST=unix://$HOME/.colima/recap/docker.sock`.
- **A port already in use.** colima forwards a published port to the Mac's 127.0.0.1; if
  another program already listens there (a RECAP API started with uvicorn, say), the forward
  fails silently and requests reach that program. Check with
  `lsof -nP -iTCP:8000 -sTCP:LISTEN`, and choose another port: `RECAP_API_PORT=8001`.

## Starting it

```bash
cp infra/compose/.env.example infra/compose/.env            # optional; every value has a default
docker compose -f infra/compose/compose.yaml up --build -d --wait   # waits until all are healthy
docker compose -f infra/compose/compose.yaml ps             # every service "healthy"
docker compose -f infra/compose/compose.yaml exec api cat /data/operator-token   # sign in with it
open http://127.0.0.1:8080                                   # Mission Control
```

Start a run from Mission Control (or `POST /incidents/run` with the token); `GET /queue` shows
the queue and its workers. More workers: `docker compose -f infra/compose/compose.yaml up -d
--scale worker=3`.

Metrics: set `RECAP_METRICS=1` in `infra/compose/.env`, then
`docker compose -f infra/compose/compose.yaml --profile metrics up -d` and open
http://127.0.0.1:9090. Prometheus scrapes the API and every worker replica (each serves its own
runs' metrics on port 9100 inside Compose's network, never published); what was measured with
it under load: [docs/performance-and-reliability.md](../../docs/performance-and-reliability.md).

## Stopping and cleaning up

```bash
docker compose -f infra/compose/compose.yaml down        # stop; the volumes (runs, queue) stay
docker compose -f infra/compose/compose.yaml down -v     # stop and delete every volume
docker image rm recap:local recap-dashboard:local        # the two images built here
colima stop recap                                        # stop the VM (its disk stays)
docker context use default                               # point the Docker CLI back
colima delete recap                                      # only to remove the VM and its disk
```

`docker compose stop` sends SIGTERM: a worker stops claiming, gives running incidents
`RECAP_WORKER_GRACE_SECONDS` (25 s) to finish, then cancels them and settles each job from its
evidence (`stop_grace_period` is 45 s, so Docker does not kill it first).

## The real-Redis test

`tests/integration/test_workers_redis.py` runs only when `RECAP_TEST_REDIS_URL` is set. Give it
a throwaway Redis, never the stack's own (one test drops every client connection, as a Redis
restart would; the stack's Redis is not published anyway):

```bash
docker run --rm -d --name recap-test-redis -p 127.0.0.1:6390:6379 redis:7.4.2-alpine
RECAP_TEST_REDIS_URL=redis://127.0.0.1:6390/15 .venv/bin/pytest tests/integration/test_workers_redis.py
docker stop recap-test-redis                                 # --rm: the container is removed
```

## Pinning digests

Every base image is pinned by version and by its multi-arch index digest (each index lists
linux/amd64 and linux/arm64), read with `docker manifest inspect <image>@<digest>` on
2026-11-08 after the first pull. To move to a newer version, pull the new tag, take its index
digest from `docker image inspect --format '{{index .RepoDigests 0}}' <image>:<tag>`, check it
is an index with `docker manifest inspect`, and update `Dockerfile` and `compose.yaml`.

## What is not verified yet

- building or running on linux/amd64 (only linux/arm64 was built and run);
- the images were built by Docker's classic builder (no buildx plugin on the build machine);
  BuildKit should build them the same way, but that was not tried;
- a container's runs record `code_version: "unknown"` (the image holds no git checkout).
