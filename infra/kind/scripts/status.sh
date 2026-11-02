#!/usr/bin/env bash
# Show RECAP's Kind demo, read-only: the node, the workloads and their images, the rollout
# state, recent warning events, whether the four identity tokens are still accepted, the three
# loopback endpoints and the node container's memory. Changes nothing.
#
#   infra/kind/scripts/status.sh [--evidence DIR]
#
# --evidence DIR also saves what a run's record needs into DIR/<UTC time>/: Deployments,
# ReplicaSets, pods and events as JSON, the rollout histories, and each service's recent logs
# (current and previous containers), so a failed run's state survives the next reset. Choose
# DIR outside the credentials folder; the demo's logs hold no secrets.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

usage() { die "usage: status.sh [--evidence DIR]"; }

evidence=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --evidence)
      [[ $# -ge 2 && -n "$2" ]] || usage
      evidence="$2"
      shift 2
      ;;
    *) usage ;;
  esac
done

require_tools kind kubectl openssl curl
check_kind_home
kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME" \
  || die "no Kind cluster named $CLUSTER_NAME (run setup.sh)"
require_our_cluster

section() { printf '\n== %s\n' "$*"; }

section "node"
kadmin get nodes -o wide

section "recap-demo"
kadmin --namespace "$DEMO_NS" get deployments \
  -o custom-columns='NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image,READY:.status.readyReplicas,WANTED:.spec.replicas,CHANGE-CAUSE:.metadata.annotations.kubernetes\.io/change-cause'
for deploy in frontend backend; do
  say "$deploy: rollout $(rollout_state "$deploy")"
done
kadmin --namespace "$DEMO_NS" get pods -o wide

section "recap-system"
kadmin --namespace "$SYSTEM_NS" get pods -o wide

section "backend revisions"
kadmin --namespace "$DEMO_NS" rollout history deployment/backend

section "recent warnings in recap-demo"
kadmin --namespace "$DEMO_NS" get events --field-selector type=Warning --sort-by=.lastTimestamp \
  | tail -n 15 || true

section "identities"
for identity in $IDENTITIES; do
  if [[ ! -f "$KIND_HOME/$identity.kubeconfig" ]]; then
    say "$identity: no kubeconfig (run mint-tokens.sh)"
  elif kas "$identity" auth can-i --list --namespace "$DEMO_NS" >/dev/null 2>&1; then
    say "$identity: token accepted"
  else
    say "$identity: token refused (expired? run mint-tokens.sh)"
  fi
done

section "endpoints on 127.0.0.1"
result="$(probe "$FRONTEND_URL/" 10)"
say "frontend $FRONTEND_URL/: ${result%% *} of 10 requests failed"
say "backend $BACKEND_URL/readyz: HTTP $(http_code "$BACKEND_URL/readyz") (through the Service: any ready pod)"
say "prometheus $PROMETHEUS_URL/-/ready: HTTP $(http_code "$PROMETHEUS_URL/-/ready")"

if command -v docker >/dev/null 2>&1; then
  section "node container"
  docker stats --no-stream --format '{{.Name}}: CPU {{.CPUPerc}}, memory {{.MemUsage}}' \
    "$CLUSTER_NAME-control-plane" || true
fi

if [[ -n "$evidence" ]]; then
  folder="$evidence/$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$folder"
  kadmin --namespace "$DEMO_NS" get deployments,replicasets,pods -o json >"$folder/workloads.json"
  kadmin --namespace "$DEMO_NS" get events -o json >"$folder/events.json"
  for deploy in frontend backend; do
    kadmin --namespace "$DEMO_NS" rollout history "deployment/$deploy" >"$folder/history-$deploy.txt"
    kadmin --namespace "$DEMO_NS" logs --selector "app=$deploy" --prefix --timestamps --tail=500 \
      --max-log-requests=10 >"$folder/logs-$deploy.txt" 2>&1 || true
    kadmin --namespace "$DEMO_NS" logs --selector "app=$deploy" --prefix --timestamps --tail=500 \
      --max-log-requests=10 --previous >"$folder/logs-$deploy-previous.txt" 2>&1 || true
  done
  say "evidence saved in $folder"
fi
