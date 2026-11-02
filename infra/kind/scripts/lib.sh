#!/usr/bin/env bash
# Shared settings, guards and helpers for RECAP's Kind demo scripts (infra/kind/README.md).
# Every script in this folder sources this file; on its own it does nothing.
#
# The rules every script keeps:
#   - kubectl runs only through kadmin (the administrator credential) or kas (one of RECAP's
#     four identities). Both always pass --kubeconfig, a file in the private credentials
#     folder, and --context kind-recap-m5. KUBECONFIG points at the private admin file as
#     well, so not even a stray call can read or change ~/.kube/config.
#   - Before its first cluster call a script runs require_our_cluster: the context exists,
#     its API server is on 127.0.0.1, and the cluster's certificate authority has the
#     SHA-256 fingerprint recorded when setup.sh created the cluster. Anything else is refused.
#   - Writes into recap-demo (inject-fault.sh, reset.sh) run as the recap-scenario identity,
#     so Kubernetes RBAC and the admission policy check them exactly as they check RECAP's
#     own reset. Each write is a server-side dry run first, then the same request for real.
# Written for bash 3.2 (macOS) and later. Nothing here has been run against a cluster yet.

# The settings below are read by the scripts that source this file.
# shellcheck disable=SC2034

CLUSTER_NAME="recap-m5"
KUBE_CONTEXT="kind-recap-m5"
DEMO_NS="recap-demo"
SYSTEM_NS="recap-system"
BASELINE_VERSION="v1.0.0"
# The five demo images (recap-demo/<service>:<version>) that setup.sh builds and loads into the
# node; the admission policy allows RECAP's identities exactly these.
DEMO_IMAGES="frontend:v1.0.0 backend:v1.0.0 backend:v1.1.0 backend:v1.1.1 backend:v1.2.0"
IDENTITIES="recap-reader recap-writer recap-oracle recap-scenario"
FRONTEND_URL="http://127.0.0.1:30080"
BACKEND_URL="http://127.0.0.1:30081"
PROMETHEUS_URL="http://127.0.0.1:30090"

# The private credentials folder, outside git: the admin kubeconfig, the four identity
# kubeconfigs and ca.sha256. RECAP_KIND_HOME may name another absolute path.
KIND_HOME="${RECAP_KIND_HOME:-$HOME/.config/recap/kind}"
# The container engine: the dedicated colima profile's socket (README, setup order), never
# whatever Docker context happens to be current, so the cluster cannot land in another engine.
# A private Docker client configuration (no credential helper: every image used here is
# public) leaves the user's own ~/.docker untouched, whatever credential store it names.
export DOCKER_HOST="${RECAP_DOCKER_HOST:-unix://$HOME/.colima/recap-m5/docker.sock}"
unset DOCKER_CONTEXT
export DOCKER_CONFIG="$KIND_HOME/docker-client"
if [[ ! -f "$DOCKER_CONFIG/config.json" ]]; then
  (umask 077 && mkdir -p "$DOCKER_CONFIG" && printf '{}\n' >"$DOCKER_CONFIG/config.json")
fi
ADMIN_KUBECONFIG="$KIND_HOME/admin.kubeconfig"
CA_FINGERPRINT_FILE="$KIND_HOME/ca.sha256"

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
KIND_DIR="$(dirname "$SCRIPTS_DIR")"
REPO_ROOT="$(cd "$KIND_DIR/../.." && pwd -P)"

export KUBECONFIG="$ADMIN_KUBECONFIG"

# Who kube() acts as: "admin", or one of the four identities.
ACTOR="admin"

say() { printf '[recap-kind] %s\n' "$*"; }
warn() { printf '[recap-kind] WARNING: %s\n' "$*" >&2; }
die() {
  printf '[recap-kind] ERROR: %s\n' "$*" >&2
  exit 1
}

kadmin() {
  kubectl --kubeconfig "$ADMIN_KUBECONFIG" --context "$KUBE_CONTEXT" "$@"
}

# kas <identity> <kubectl arguments...>: kubectl as one of RECAP's four identities.
kas() {
  local identity="$1"
  shift
  case " $IDENTITIES " in
    *" $identity "*) ;;
    *) die "not one of RECAP's identities: $identity" ;;
  esac
  kubectl --kubeconfig "$KIND_HOME/$identity.kubeconfig" --context "$KUBE_CONTEXT" "$@"
}

kube() {
  if [[ "$ACTOR" == "admin" ]]; then
    kadmin "$@"
  else
    kas "$ACTOR" "$@"
  fi
}

require_tools() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 \
      || die "$tool is not installed (infra/kind/README.md lists what the setup needs)"
  done
}

# The credentials folder must be an absolute path outside the repository, so nothing in it can
# ever be committed.
check_kind_home() {
  local physical="$KIND_HOME"
  case "$KIND_HOME" in
    /*) ;;
    *) die "RECAP_KIND_HOME must be an absolute path, not $KIND_HOME" ;;
  esac
  if [[ -d "$KIND_HOME" ]]; then
    physical="$(cd "$KIND_HOME" && pwd -P)"
  fi
  case "$physical/" in
    "$REPO_ROOT"/*) die "the credentials folder $KIND_HOME is inside the repository: choose another RECAP_KIND_HOME" ;;
  esac
}

sha256_hex() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | cut -d ' ' -f 1
  else
    sha256sum | cut -d ' ' -f 1
  fi
}

# The fingerprint that ca.sha256 records and every script checks: the lowercase hex SHA-256 of
# the DER bytes of the cluster CA certificate a kubeconfig embeds (certificate-authority-data),
# which is `openssl x509 -fingerprint -sha256` without the colons. Every new Kind cluster gets a
# new CA, so the value names exactly one cluster, and kubectl's TLS check proves the API server
# belongs to it.
ca_fingerprint() {
  local kubeconfig="$1" data
  data="$(kubectl --kubeconfig "$kubeconfig" --context "$KUBE_CONTEXT" config view --raw --minify \
    -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' 2>/dev/null)" || return 1
  [[ -n "$data" ]] || return 1
  printf '%s' "$data" | openssl base64 -d -A | openssl x509 -outform der | sha256_hex
}

api_server_of() {
  kubectl --kubeconfig "$1" --context "$KUBE_CONTEXT" config view --minify \
    -o jsonpath='{.clusters[0].cluster.server}'
}

recorded_fingerprint() {
  tr -d '[:space:]' <"$CA_FINGERPRINT_FILE"
}

# Refuse unless the admin kubeconfig's context kind-recap-m5 reaches a loopback API server whose
# CA matches the fingerprint recorded at creation.
require_our_cluster() {
  local server actual loopback='^https://127\.0\.0\.1:[0-9]+$'
  [[ -f "$ADMIN_KUBECONFIG" ]] || die "no admin kubeconfig at $ADMIN_KUBECONFIG (run setup.sh first)"
  kadmin config get-contexts "$KUBE_CONTEXT" >/dev/null 2>&1 \
    || die "$ADMIN_KUBECONFIG has no context $KUBE_CONTEXT: refusing"
  server="$(api_server_of "$ADMIN_KUBECONFIG")" \
    || die "cannot read the API server of $KUBE_CONTEXT from $ADMIN_KUBECONFIG: refusing"
  [[ "$server" =~ $loopback ]] \
    || die "the API server of $KUBE_CONTEXT is '$server', not on 127.0.0.1: refusing"
  [[ -s "$CA_FINGERPRINT_FILE" ]] \
    || die "no CA fingerprint recorded in $CA_FINGERPRINT_FILE: refusing (setup.sh records it when it creates the cluster)"
  actual="$(ca_fingerprint "$ADMIN_KUBECONFIG")" || die "cannot read the cluster CA from $ADMIN_KUBECONFIG"
  [[ "$actual" == "$(recorded_fingerprint)" ]] \
    || die "the cluster CA in $ADMIN_KUBECONFIG is not the one recorded at creation: this is not RECAP's cluster; refusing"
}

# Refuse unless an identity's kubeconfig names the same API server and CA as the admin one, and
# the API server still accepts its token.
require_identity() {
  local identity="$1" file="$KIND_HOME/$1.kubeconfig"
  [[ -f "$file" ]] || die "no kubeconfig for $identity at $file (run mint-tokens.sh)"
  [[ "$(api_server_of "$file")" == "$(api_server_of "$ADMIN_KUBECONFIG")" ]] \
    || die "$file names another API server than the admin kubeconfig: refusing"
  [[ "$(ca_fingerprint "$file")" == "$(recorded_fingerprint)" ]] \
    || die "$file trusts another cluster CA: refusing"
  kas "$identity" auth can-i --list --namespace "$DEMO_NS" >/dev/null 2>&1 \
    || die "the API server does not accept the $identity token (expired? run mint-tokens.sh)"
}

# The rollout-complete rule of docs/m5-integration-plan.md section 3.7, read with one GET (so the
# recap-scenario identity, which may get but not watch, can use it): prints complete,
# progressing, failed (ProgressDeadlineExceeded) or unknown.
rollout_state() {
  local deploy="$1" fields generation observed wanted updated total available reason
  fields="$(kube --namespace "$DEMO_NS" get deployment "$deploy" -o jsonpath='{.metadata.generation}|{.status.observedGeneration}|{.spec.replicas}|{.status.updatedReplicas}|{.status.replicas}|{.status.availableReplicas}|{.status.conditions[?(@.type=="Progressing")].reason}' 2>/dev/null)" \
    || {
      echo unknown
      return 0
    }
  IFS='|' read -r generation observed wanted updated total available reason <<<"$fields"
  if [[ "$reason" == "ProgressDeadlineExceeded" ]]; then
    echo failed
  elif ((${observed:-0} >= ${generation:-1} && ${updated:-0} == ${wanted:-0} && ${total:-0} == ${updated:-0} && ${available:-0} == ${updated:-0})); then
    echo complete
  else
    echo progressing
  fi
}

# wait_rollout <deployment> <seconds>: 0 when complete, 2 when the progress deadline passed,
# 1 when time ran out.
wait_rollout() {
  local deploy="$1" timeout="$2" waited=0 state
  while ((waited < timeout)); do
    state="$(rollout_state "$deploy")"
    case "$state" in
      complete)
        say "$deploy: rollout complete"
        return 0
        ;;
      failed)
        warn "$deploy: rollout failed (ProgressDeadlineExceeded)"
        return 2
        ;;
    esac
    sleep 2
    waited=$((waited + 2))
  done
  warn "$deploy: rollout not complete after ${timeout}s"
  return 1
}

image_of() {
  kube --namespace "$DEMO_NS" get deployment "$1" -o jsonpath='{.spec.template.spec.containers[0].image}'
}

# ready_of <deployment>: prints "<ready pods> <wanted pods>".
ready_of() {
  local fields ready wanted
  fields="$(kube --namespace "$DEMO_NS" get deployment "$1" -o jsonpath='{.status.readyReplicas}|{.spec.replicas}')"
  IFS='|' read -r ready wanted <<<"$fields"
  echo "${ready:-0} ${wanted:-0}"
}

# Set one Deployment's image and its change-cause in a single PATCH, so the new ReplicaSet
# carries both. The server-side dry run goes first: RBAC and the admission policy judge the exact
# change before anything is stored.
set_release() {
  local deploy="$1" version="$2" version_re='^v[0-9]+\.[0-9]+\.[0-9]+$' patch
  [[ "$version" =~ $version_re ]] || die "not a version: $version"
  case "$deploy" in
    frontend | backend) ;;
    *) die "not a demo Deployment: $deploy" ;;
  esac
  patch="$(printf '{"metadata":{"annotations":{"kubernetes.io/change-cause":"release %s"}},"spec":{"template":{"spec":{"containers":[{"name":"%s","image":"recap-demo/%s:%s"}]}}}}' \
    "$version" "$deploy" "$deploy" "$version")"
  kube --namespace "$DEMO_NS" patch deployment "$deploy" --type strategic --patch "$patch" --dry-run=server >/dev/null
  kube --namespace "$DEMO_NS" patch deployment "$deploy" --type strategic --patch "$patch"
}

# A rolling restart, sent as the same PATCH `kubectl rollout restart` sends (one pod-template
# annotation), with the same server-side dry run first.
restart_deployment() {
  local deploy="$1" patch
  case "$deploy" in
    frontend | backend) ;;
    *) die "not a demo Deployment: $deploy" ;;
  esac
  patch="$(printf '{"spec":{"template":{"metadata":{"annotations":{"kubectl.kubernetes.io/restartedAt":"%s"}}}}}' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)")"
  kube --namespace "$DEMO_NS" patch deployment "$deploy" --type strategic --patch "$patch" --dry-run=server >/dev/null
  kube --namespace "$DEMO_NS" patch deployment "$deploy" --type strategic --patch "$patch"
}

# http_code <url>: the HTTP status within 3 s, or 000 when nothing answered. Never via a proxy.
http_code() {
  curl --noproxy '*' --silent --output /dev/null --write-out '%{http_code}' --max-time 3 "$1" || true
}

# probe <url> <count>: prints "<failed> <count>"; anything but a 2xx answer counts as failed.
probe() {
  local url="$1" count="$2" failed=0 i code
  for ((i = 0; i < count; i++)); do
    code="$(http_code "$url")"
    case "$code" in
      2??) ;;
      *) failed=$((failed + 1)) ;;
    esac
  done
  echo "$failed $count"
}

# wait_for_frontend <seconds>: until 20 requests in a row succeed on 127.0.0.1:30080.
wait_for_frontend() {
  local timeout="$1" started result="" now
  started="$(date +%s)"
  while true; do
    result="$(probe "$FRONTEND_URL/" 20)"
    if [[ "${result%% *}" == "0" ]]; then
      say "frontend: 20 of 20 requests answered"
      return 0
    fi
    now="$(date +%s)"
    if ((now - started >= timeout)); then
      warn "frontend: still failing after ${timeout}s (last probe: ${result%% *} of 20 failed)"
      return 1
    fi
    sleep 3
  done
}
