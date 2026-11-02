#!/usr/bin/env bash
# Write a short-lived kubeconfig for each of RECAP's four identities into the private
# credentials folder (~/.config/recap/kind/):
#
#   recap-reader.kubeconfig    the seven read tools (MCP server)
#   recap-writer.kubeconfig    the one write path, execute_authorized (MCP server)
#   recap-oracle.kubeconfig    the recovery oracle's health_check (controller)
#   recap-scenario.kubeconfig  reset and fault injection (controller, and these scripts)
#
#   infra/kind/scripts/mint-tokens.sh [--duration 4h]      (from 10m to 24h; default 4h)
#
# Each token comes from the TokenRequest API (kubectl create token) and ends by itself after the
# duration; deleting its ServiceAccount ends it at once. No token ever appears on a command line
# or in shell history: kubectl prints it, the script keeps it in a variable and writes it with a
# here-document, under umask 077 (files mode 600, the folder 700). Each file names the context
# kind-recap-m5, the same API server and the same CA as the admin kubeconfig. Run it again for a
# new working session: the files are replaced.
set -euo pipefail
set +x # never trace this script: a trace would print the tokens
umask 077
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

usage() { die "usage: mint-tokens.sh [--duration 4h] (from 10m to 24h)"; }

duration="4h"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --duration)
      [[ $# -ge 2 ]] || usage
      duration="$2"
      shift 2
      ;;
    *) usage ;;
  esac
done
duration_re='^([1-9][0-9]*)(m|h)$'
[[ "$duration" =~ $duration_re ]] || usage
minutes="${BASH_REMATCH[1]}"
if [[ "${BASH_REMATCH[2]}" == "h" ]]; then
  minutes=$((minutes * 60))
fi
((minutes >= 10 && minutes <= 1440)) || usage

require_tools kubectl openssl
check_kind_home
require_our_cluster

server="$(api_server_of "$ADMIN_KUBECONFIG")"
ca_data="$(kadmin config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"
[[ -n "$ca_data" ]] || die "the admin kubeconfig holds no cluster CA"

tmp=""
trap 'rm -f "$tmp"' EXIT
for identity in $IDENTITIES; do
  target="$KIND_HOME/$identity.kubeconfig"
  tmp="$(mktemp "$KIND_HOME/.$identity.XXXXXX")"
  token="$(kadmin --namespace "$SYSTEM_NS" create token "$identity" --duration="$duration")"
  [[ -n "$token" ]] || die "kubectl returned no token for $identity"
  cat >"$tmp" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: $KUBE_CONTEXT
    cluster:
      server: $server
      certificate-authority-data: $ca_data
users:
  - name: $identity
    user:
      token: $token
contexts:
  - name: $KUBE_CONTEXT
    context:
      cluster: $KUBE_CONTEXT
      namespace: $DEMO_NS
      user: $identity
current-context: $KUBE_CONTEXT
EOF
  token=""
  chmod 600 "$tmp"
  mv -f "$tmp" "$target"
  tmp=""
  [[ "$(ca_fingerprint "$target")" == "$(recorded_fingerprint)" ]] \
    || die "$target does not trust the recorded cluster CA"
  kas "$identity" auth can-i --list --namespace "$DEMO_NS" >/dev/null \
    || die "the API server refused the new $identity token"
  say "wrote $target (valid for $duration)"
done
say "the four identities are ready; status.sh shows whether their tokens are still accepted"
