#!/usr/bin/env bash
# SREGym on RECAP's M5 VM (W5): one pinned SREGym problem in SREGym's external-harness mode, for
# RECAP to investigate from the Mac. Run ON the VM (infra/aws/ec2.sh ssh), from ~/recap:
#
#   bash infra/sregym/sregym.sh setup           pinned tools (checksums compared), SREGym at its
#                                               pinned commit, SREGym's own Kind cluster
#   bash infra/sregym/sregym.sh deploy PROBLEM  SREGym deploys the app and injects PROBLEM, then
#                                               RECAP's three identities are granted
#   bash infra/sregym/sregym.sh grant           RECAP's identities again, with fresh 8 h tokens
#   bash infra/sregym/sregym.sh grade PROBLEM   SREGym's own mitigation oracle (exit 0 = pass)
#   bash infra/sregym/sregym.sh status          SREGym's nodes, the app's pods
#   bash infra/sregym/sregym.sh teardown        delete SREGym's cluster; RECAP's demo comes back
#
# Safety: SREGym's harness acts as cluster-admin on the CURRENT kube context and resets the whole
# cluster to its baseline. Every command that reaches a cluster first checks that the current
# context is SREGym's own (kind-kind), on 127.0.0.1, with the CA recorded at setup. RECAP's demo
# cluster keeps its credentials in a private file SREGym never reads, and is stopped while SREGym
# runs. No token is ever printed or passed on a command line: kubectl prints it into a variable,
# which a here-document writes under umask 077.
set -euo pipefail
set +x # never trace this script: a trace would print the tokens
unset KUBECONFIG # SREGym reads ~/.kube/config; RECAP's demo admin file is never involved

SREGYM_REPO="https://github.com/SREGym/SREGym.git"
SREGYM_COMMIT="a90b43cc973158120119a9f388081eac4f7adb36" # release 1.1
APPS_COMMIT="afd5b947fbafefc110e7dc00acd698cb2683dc7f"   # its SREGym-applications submodule
HELM_VERSION="v4.3.1"                                    # SREGym needs Helm 4 (server-side apply)
UV_VERSION="0.13.1"
KUBECTL_VERSION="v1.32.1" # SREGym's node image runs Kubernetes 1.32.1: kubectl within one minor
CONTEXT="kind-kind"
NAMESPACE="hotel-reservation"
DEMO_NODE="recap-m5-control-plane"
SREGYM_DIR="$HOME/sregym"
TOOLS="$HOME/.local/recap-sregym/bin"   # pinned helm, uv and kubectl for SREGym's harness
RECAP_HOME="$HOME/.config/recap/sregym" # RECAP's identity kubeconfigs and ca.sha256
IDENTITIES="recap-reader recap-writer recap-oracle"
HERE="$(cd "$(dirname "$0")" && pwd -P)"
export PATH="$TOOLS:$PATH"

say() { printf '[recap-sregym] %s\n' "$*"; }
die() { printf '[recap-sregym] error: %s\n' "$*" >&2; exit 1; }

# --- pinned tools -------------------------------------------------------------------------

checked() { # file, expected sha256: refuse anything else
  [[ "$(sha256sum "$1" | cut -d' ' -f1)" == "$2" ]] || die "$1: checksum mismatch"
}

install_tools() {
  local work
  work="$(mktemp -d)"
  mkdir -p "$TOOLS"
  (
    cd "$work"
    curl -fsSLo helm.tgz "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz"
    curl -fsSLo helm.sum "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz.sha256sum"
    checked helm.tgz "$(cut -d' ' -f1 helm.sum)"
    tar -xzf helm.tgz linux-amd64/helm
    install -m 0755 linux-amd64/helm "$TOOLS/helm"

    local uv="uv-x86_64-unknown-linux-gnu"
    curl -fsSLo uv.tgz "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${uv}.tar.gz"
    curl -fsSLo uv.sum "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${uv}.tar.gz.sha256"
    checked uv.tgz "$(cut -d' ' -f1 uv.sum)"
    tar -xzf uv.tgz
    install -m 0755 "$uv/uv" "$TOOLS/uv"

    curl -fsSLo kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
    curl -fsSLo kubectl.sum "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl.sha256"
    checked kubectl "$(cat kubectl.sum)"
    install -m 0755 kubectl "$TOOLS/kubectl"
  )
  rm -rf "$work"
  say "tools: $(helm version --short), uv $(uv --version | cut -d' ' -f2), kubectl $KUBECTL_VERSION"
}

clone_sregym() {
  [[ -d "$SREGYM_DIR/.git" ]] || git clone --quiet "$SREGYM_REPO" "$SREGYM_DIR"
  git -C "$SREGYM_DIR" fetch --quiet origin "$SREGYM_COMMIT"
  git -C "$SREGYM_DIR" checkout --quiet --detach "$SREGYM_COMMIT"
  git -C "$SREGYM_DIR" submodule update --init --recursive --quiet
  [[ "$(git -C "$SREGYM_DIR" rev-parse HEAD)" == "$SREGYM_COMMIT" ]] || die "SREGym is not at $SREGYM_COMMIT"
  [[ "$(git -C "$SREGYM_DIR/SREGym-applications" rev-parse HEAD)" == "$APPS_COMMIT" ]] \
    || die "SREGym-applications is not at its pinned commit $APPS_COMMIT"
  [[ -z "$(git -C "$SREGYM_DIR" status --porcelain)" ]] || die "the SREGym checkout has local changes"
  (cd "$SREGYM_DIR" && uv sync --frozen --quiet) # uv.lock as pinned; Python per .python-version
  say "SREGym at $SREGYM_COMMIT (applications at $APPS_COMMIT), its environment synced"
}

# --- the cluster and its guard --------------------------------------------------------------

stop_demo() {
  if docker inspect "$DEMO_NODE" >/dev/null 2>&1 \
    && [[ "$(docker inspect -f '{{.State.Running}}' "$DEMO_NODE")" == "true" ]]; then
    docker stop "$DEMO_NODE" >/dev/null && say "RECAP's demo cluster stopped while SREGym runs"
  fi
}

ca_data() {
  kubectl config view --raw --minify --context "$CONTEXT" \
    -o jsonpath='{.clusters[0].cluster.certificate-authority-data}'
}

fingerprint() { # the lowercase hex SHA-256 of the CA certificate (DER), as RECAP checks it
  printf '%s' "$1" | openssl base64 -d -A | openssl x509 -outform der | sha256sum | cut -d' ' -f1
}

start_sregym() {
  # Kind's node containers do not start by themselves after the VM was stopped (the VM's idle
  # guard stops it): start them, and wait until SREGym's API server answers.
  local nodes
  nodes="$(docker ps -a --filter label=io.x-k8s.kind.cluster=kind --format '{{.Names}}')"
  [[ -n "$nodes" ]] || return 0
  if [[ -n "$(docker ps -a --filter label=io.x-k8s.kind.cluster=kind --filter status=exited --format '{{.Names}}')" ]]; then
    # shellcheck disable=SC2086
    docker start $nodes >/dev/null
    say "SREGym's nodes started again"
  fi
  local tries=0
  until kubectl --context "$CONTEXT" get --raw=/readyz >/dev/null 2>&1; do
    tries=$((tries + 1))
    [[ $tries -le 60 ]] || die "SREGym's API server did not come back: run teardown, then setup"
    sleep 3
  done
}

require_sregym() {
  local current server
  current="$(kubectl config current-context 2>/dev/null || true)"
  [[ "$current" == "$CONTEXT" ]] \
    || die "the current kube context is '${current:-none}', not SREGym's $CONTEXT: refused"
  server="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
  [[ "$server" =~ ^https://127\.0\.0\.1:[0-9]+$ ]] || die "SREGym's API server is not on 127.0.0.1"
  [[ -f "$RECAP_HOME/ca.sha256" ]] || die "no CA recorded for SREGym's cluster: run setup"
  [[ "$(fingerprint "$(ca_data)")" == "$(cat "$RECAP_HOME/ca.sha256")" ]] \
    || die "SREGym's cluster CA differs from the one recorded at setup: refused"
}

cmd_setup() {
  install_tools
  clone_sregym
  stop_demo
  if kind get clusters 2>/dev/null | grep -qx kind; then
    say "SREGym's cluster already exists"
  else
    (cd "$SREGYM_DIR" && bash kind/setup_kind_cluster.sh)
  fi
  kubectl config use-context "$CONTEXT" >/dev/null
  (umask 077 && mkdir -p "$RECAP_HOME" && fingerprint "$(ca_data)" >"$RECAP_HOME/ca.sha256")
  require_sregym
  say "ready: $(kubectl get nodes --no-headers | wc -l | tr -d ' ') nodes in $CONTEXT"
}

# --- a problem, RECAP's identities, the grade ---------------------------------------------

problem_arg() {
  [[ "${1:-}" =~ ^[a-z0-9_]{1,80}$ ]] || die "name a SREGym problem id (e.g. misconfig_app_hotel_res)"
  printf '%s' "$1"
}

cmd_deploy() {
  local problem
  problem="$(problem_arg "${1:-}")"
  stop_demo
  start_sregym
  require_sregym
  say "SREGym's harness deploys the app and injects $problem (this takes several minutes)"
  (cd "$SREGYM_DIR" && uv run main.py --problem "$problem" --use-external-harness)
  cmd_grant
}

cmd_grant() {
  start_sregym
  require_sregym
  kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || die "no $NAMESPACE namespace: deploy a problem first"
  kubectl apply -f "$HERE/recap-rbac.yaml" >/dev/null
  local server ca identity token tmp
  server="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
  ca="$(ca_data)"
  (umask 077 && mkdir -p "$RECAP_HOME")
  for identity in $IDENTITIES; do
    token="$(kubectl --namespace default create token "$identity" --duration=8h)"
    [[ -n "$token" ]] || die "kubectl returned no token for $identity"
    tmp="$(umask 077 && mktemp "$RECAP_HOME/.$identity.XXXXXX")"
    cat >"$tmp" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: $CONTEXT
    cluster:
      server: $server
      certificate-authority-data: $ca
users:
  - name: $identity
    user:
      token: $token
contexts:
  - name: $CONTEXT
    context:
      cluster: $CONTEXT
      namespace: $NAMESPACE
      user: $identity
current-context: $CONTEXT
EOF
    token=""
    chmod 600 "$tmp"
    mv -f "$tmp" "$RECAP_HOME/$identity.kubeconfig"
    kubectl --kubeconfig "$RECAP_HOME/$identity.kubeconfig" auth can-i --list \
      --namespace "$NAMESPACE" >/dev/null || die "the API server refused the new $identity token"
  done
  say "RECAP's identities are ready in $NAMESPACE (tokens valid for 8 h): copy them with ec2.sh creds sregym"
}

cmd_grade() {
  local problem
  problem="$(problem_arg "${1:-}")"
  start_sregym
  require_sregym
  (cd "$SREGYM_DIR" && uv run python run-oracle.py --problem "$problem")
}

cmd_status() {
  start_sregym
  require_sregym
  kubectl get nodes
  kubectl --namespace "$NAMESPACE" get deployments,pods 2>/dev/null || say "no $NAMESPACE namespace yet"
}

cmd_teardown() {
  if kind get clusters 2>/dev/null | grep -qx kind; then
    kind delete cluster --name kind
  fi
  rm -f "$RECAP_HOME"/recap-*.kubeconfig "$RECAP_HOME/ca.sha256"
  if docker inspect "$DEMO_NODE" >/dev/null 2>&1; then
    docker start "$DEMO_NODE" >/dev/null && say "RECAP's demo cluster started again"
  fi
}

case "${1:-}" in
  setup) cmd_setup ;;
  deploy) cmd_deploy "${2:-}" ;;
  grant) cmd_grant ;;
  grade) cmd_grade "${2:-}" ;;
  status) cmd_status ;;
  teardown) cmd_teardown ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
