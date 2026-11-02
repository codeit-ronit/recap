#!/usr/bin/env bash
# Create RECAP's Kind demo from nothing, or finish a setup that stopped halfway.
#
#   infra/kind/scripts/setup.sh
#
# Steps, each checked before the next (the script stops at the first failure):
#   1. tools: docker answers; kind, kubectl, openssl and curl are installed;
#   2. cluster: `kind create cluster` from infra/kind/cluster.yaml (one node; the API server and
#      the NodePorts on 127.0.0.1 only), with the administrator kubeconfig written into the
#      private credentials folder (~/.config/recap/kind/, mode 600), never ~/.kube/config, and
#      the fingerprint of the new cluster's CA recorded next to it (ca.sha256);
#   3. images: the five demo images built from infra/kind/app and loaded into the node, so
#      the node never pulls them;
#   4. manifests: infra/kind/manifests/ applied with the administrator credential;
#   5. healthy: every Deployment rolled out, the frontend answering on 127.0.0.1:30080.
#
# Run it again at any time: an existing cluster is reused after the same checks, images are
# rebuilt from Docker's cache and the manifests re-applied. Next: mint-tokens.sh.
# Nothing in infra/kind has been run yet (infra/kind/README.md).
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

[[ $# -eq 0 ]] || die "usage: setup.sh (no arguments)"

require_tools docker kind kubectl openssl curl
check_kind_home
docker info >/dev/null 2>&1 \
  || die "the recap-m5 engine does not answer ($DOCKER_HOST): colima start --profile recap-m5 --cpu 4 --memory 4 --disk 20 (README, setup order)"
say "docker engine: $DOCKER_HOST"
kind_version="$(kind version)"
say "$kind_version"
case "$kind_version" in
  *" v0.33."*) ;;
  *) warn "cluster.yaml pins kind v0.33.0's default node image; this kind may not support it" ;;
esac

mkdir -p "$KIND_HOME"
chmod 700 "$KIND_HOME"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  say "cluster $CLUSTER_NAME exists: reusing it"
  [[ -s "$CA_FINGERPRINT_FILE" ]] \
    || die "cluster $CLUSTER_NAME exists, but its CA fingerprint was never recorded in $KIND_HOME: run cleanup.sh, then setup.sh"
  if [[ ! -f "$ADMIN_KUBECONFIG" ]]; then
    (umask 077 && kind export kubeconfig --name "$CLUSTER_NAME" --kubeconfig "$ADMIN_KUBECONFIG")
  fi
else
  say "creating cluster $CLUSTER_NAME (one node; API server and NodePorts on 127.0.0.1)"
  (umask 077 && kind create cluster --name "$CLUSTER_NAME" --config "$KIND_DIR/cluster.yaml" \
    --kubeconfig "$ADMIN_KUBECONFIG" --wait 120s)
  fingerprint="$(ca_fingerprint "$ADMIN_KUBECONFIG")" || die "cannot read the new cluster's CA"
  (umask 077 && printf '%s\n' "$fingerprint" >"$CA_FINGERPRINT_FILE")
  say "recorded the new cluster's CA fingerprint in $CA_FINGERPRINT_FILE"
fi
chmod 600 "$ADMIN_KUBECONFIG" "$CA_FINGERPRINT_FILE"
require_our_cluster
kadmin get nodes
kadmin version || warn "kubectl could not report the server's version"

say "building the five demo images"
images=""
for image in $DEMO_IMAGES; do
  service="${image%%:*}"
  version="${image#*:}"
  docker build --quiet --build-arg "SERVICE=$service" --build-arg "VERSION=$version" \
    --tag "recap-demo/$image" "$KIND_DIR/app" >/dev/null
  say "built recap-demo/$image"
  images="$images recap-demo/$image"
done
# One word per image, so the list is split on purpose.
# shellcheck disable=SC2086
kind load docker-image --name "$CLUSTER_NAME" $images

say "applying infra/kind/manifests"
kadmin apply --filename "$KIND_DIR/manifests/"

for deploy in frontend backend; do
  kadmin --namespace "$DEMO_NS" rollout status "deployment/$deploy" --timeout=180s
done
for deploy in prometheus traffic; do
  kadmin --namespace "$SYSTEM_NS" rollout status "deployment/$deploy" --timeout=180s
done
wait_for_frontend 90 \
  || die "the frontend does not answer on $FRONTEND_URL (does the container engine forward 127.0.0.1 ports? see the README)"
if [[ "$(http_code "$PROMETHEUS_URL/-/ready")" == "200" ]]; then
  say "prometheus: ready on $PROMETHEUS_URL"
else
  warn "prometheus: not ready on $PROMETHEUS_URL yet (status.sh shows it later)"
fi
say "setup complete. Next: infra/kind/scripts/mint-tokens.sh"
