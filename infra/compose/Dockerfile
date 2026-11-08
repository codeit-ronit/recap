# RECAP's images for Docker Compose (M6 W3): infra/compose/compose.yaml builds both targets.
#
#   recap      the API and the worker (one image, two commands): Python, RECAP's source tree,
#              its configs, scenarios, recordings and published evidence; no secrets, no tests
#   dashboard  Mission Control, built with Node and served by an unprivileged nginx that
#              proxies /api/* to the API (as the Vite dev server does)
#
# Build context: the repository root (../.. from here); .dockerignore keeps out data/, .env,
# .git, node_modules, caches and the test suite.
#
# Base images are pinned by version. The python image is the one infra/kind/app/Dockerfile
# pins by digest; the node, nginx, redis and prometheus tags are pinned by version only:
# after a first pull, pin each by digest too (infra/compose/README.md, "Pinning digests").
# Every base image is published for linux/amd64 and linux/arm64.

ARG PYTHON_IMAGE=python:3.12.16-alpine3.24@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8
ARG NODE_IMAGE=node:22.12.0-alpine3.21
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.27.3-alpine

# --- the dashboard's static files ----------------------------------------------------------
FROM ${NODE_IMAGE} AS dashboard-build
WORKDIR /build
COPY apps/dashboard/package.json apps/dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/dashboard/ ./
# The browser reaches the API through nginx at /api (same origin: no CORS, the API's host and
# origin checks still apply).
ENV VITE_API_URL=/api
RUN npm run build

# --- the dashboard: static files behind an unprivileged nginx ----------------------------------
FROM ${NGINX_IMAGE} AS dashboard
COPY infra/compose/nginx.conf /etc/nginx/nginx.conf
COPY --from=dashboard-build /build/dist /usr/share/nginx/html
# nginx-unprivileged runs as uid 101 and listens on 8080; every temporary path is under /tmp,
# so the root filesystem can be mounted read-only (compose.yaml).
EXPOSE 8080

# --- the API and the worker -------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS recap
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    RECAP_DATA_DIR=/data
WORKDIR /app
# Dependencies only, at the versions constraints.txt pins (a layer that changes only with
# pyproject.toml or constraints.txt): install the package, then remove it again.
COPY pyproject.toml constraints.txt README.md ./
COPY recap/__init__.py recap/__init__.py
RUN pip install -c constraints.txt . \
 && pip uninstall -y recap \
 && rm -rf /root/.cache recap build recap.egg-info
# RECAP finds configs/, fixtures/ and evals/ from its source tree (recap/core/config.py,
# REPO_ROOT), so it runs from the tree itself, under /app (PYTHONPATH).
COPY recap/ recap/
COPY apps/__init__.py apps/__init__.py
COPY apps/api/ apps/api/
COPY apps/worker/ apps/worker/
COPY integrations/ integrations/
COPY evals/ evals/
COPY configs/ configs/
COPY fixtures/ fixtures/
COPY scripts/ scripts/
# A numeric non-root user; /data (the runs, traces, approvals, simulator state and the
# operator token) belongs to it, and a new named volume mounted there inherits that owner.
RUN mkdir -p /data && chown 10001:10001 /data
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8000
# The API by default; the worker service runs `python -m apps.worker` instead.
CMD ["python", "-m", "uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
