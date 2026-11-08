# `infra/compose/`: RECAP with durable workers, in Docker Compose (M6 W3)

> **Status: written and statically checked, never started.** The build machine has the Docker
> CLI but no Compose plugin and no running engine, and installing them waits for the owner's
> approval. `tests/unit/test_compose_files.py` checks these files offline (ports, privileges,
> health checks, volumes, no secrets); nothing here has been built or run yet. The
> "Starting it" section is what to run once Docker works.

Docker is **optional**. Without it RECAP runs as it always has (the repository README):
`uvicorn apps.api.main:app` executes incidents in the API process (the default in-process
backend), and no Redis or worker is involved.

## What it runs

| Service | Image | Port (127.0.0.1 only) | What it does |
|---|---|---|---|
| `redis` | `redis:7.4.2-alpine` | not published | the job queue; append-only file, fsync every second |
| `api` | `recap:local` (built) | 8000 | the FastAPI backend; queues runs (`RECAP_EXECUTION_BACKEND=redis`) |
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

## Starting it (once Docker and Compose are installed)

```bash
cp infra/compose/.env.example infra/compose/.env            # optional; every value has a default
docker compose -f infra/compose/compose.yaml up --build -d
docker compose -f infra/compose/compose.yaml ps             # every service "healthy"
docker compose -f infra/compose/compose.yaml exec api cat /data/operator-token   # sign in with it
open http://127.0.0.1:8080                                   # Mission Control
```

Start a run from Mission Control (or `POST /incidents/run` with the token); `GET /queue` shows
the queue and its workers. More workers: `docker compose -f infra/compose/compose.yaml up -d
--scale worker=3`.

Metrics: set `RECAP_METRICS=1` in `infra/compose/.env`, then
`docker compose -f infra/compose/compose.yaml --profile metrics up -d` and open
http://127.0.0.1:9090.

## Stopping and cleaning up

```bash
docker compose -f infra/compose/compose.yaml down        # stop; the volumes (runs, queue) stay
docker compose -f infra/compose/compose.yaml down -v     # stop and delete every volume
docker image rm recap:local recap-dashboard:local        # the two images built here
```

`docker compose stop` sends SIGTERM: a worker stops claiming, gives running incidents
`RECAP_WORKER_GRACE_SECONDS` (25 s) to finish, then cancels them and settles each job from its
evidence (`stop_grace_period` is 45 s, so Docker does not kill it first).

## The real-Redis test

`tests/integration/test_workers_redis.py` runs only when `RECAP_TEST_REDIS_URL` is set. With
Docker working, a throwaway Redis on 127.0.0.1:

```bash
docker run --rm -d --name recap-test-redis -p 127.0.0.1:6379:6379 redis:7.4.2-alpine
RECAP_TEST_REDIS_URL=redis://127.0.0.1:6379/15 .venv/bin/pytest tests/integration/test_workers_redis.py
docker stop recap-test-redis
```

## Pinning digests

The Python base image is pinned by digest (the same one as `infra/kind/app/Dockerfile`). The
other images are pinned by version tag only; after a first successful pull, pin each by digest:

```bash
docker buildx imagetools inspect redis:7.4.2-alpine --format '{{json .Manifest.Digest}}'
```

and write `image: redis:7.4.2-alpine@sha256:...` (likewise for node, nginx-unprivileged and
prometheus in `Dockerfile` and `compose.yaml`).

## What is not verified yet

- that the images build (in particular, that every Python dependency installs from a wheel on
  Alpine/musl for both amd64 and arm64);
- that the services start, become healthy and stop cleanly in the order above;
- the redis user id (`999:1000`) the compose file runs Redis as;
- a container's runs record `code_version: "unknown"` (the image holds no git checkout).
