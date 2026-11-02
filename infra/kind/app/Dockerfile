# One image per (service, version) of RECAP's Kind demo (infra/kind/README.md). Not built yet.
#
#   docker build --build-arg SERVICE=backend --build-arg VERSION=v1.1.0 \
#     --tag recap-demo/backend:v1.1.0 infra/kind/app          (setup.sh builds all five)
#
# The base image is pinned by its multi-arch index digest (python 3.12.16 on Alpine 3.24,
# read from Docker Hub's public tag metadata on 2026-11-02; about 18.5 MB compressed for
# arm64), so every build starts from the same bytes. The program needs only the standard
# library: nothing is installed into the image.
FROM python:3.12.16-alpine3.24@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8

ARG SERVICE
ARG VERSION

# Refuse to build an image without a role, or with a tag RECAP's version rule cannot read.
RUN case "$SERVICE" in frontend|backend) ;; *) echo "SERVICE must be frontend or backend" >&2; exit 1 ;; esac \
 && { echo "$VERSION" | grep -Eqx 'v[0-9]+\.[0-9]+\.[0-9]+' || { echo "VERSION must look like v1.2.3" >&2; exit 1; }; }

# The role and the build are baked in, so a rollback that changes only the image also
# changes the behaviour, and the version the program reports is always its image tag.
ENV DEMO_SERVICE=$SERVICE \
    DEMO_BUILD=$VERSION

COPY demo_app.py /app/demo_app.py

LABEL org.opencontainers.image.title="recap-demo-$SERVICE" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.description="RECAP Kind demo service with deliberate defects; not for any other use"

# A numeric non-root user, so the kubelet can check runAsNonRoot (Pod Security "restricted").
# The root filesystem is mounted read-only in the cluster; the program writes nothing to disk.
USER 10001:10001
EXPOSE 8080

# -I: ignore PYTHON* variables and the user's site packages; -B: write no bytecode.
ENTRYPOINT ["python", "-I", "-B", "/app/demo_app.py"]
