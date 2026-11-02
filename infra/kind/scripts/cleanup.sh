#!/usr/bin/env bash
# Delete RECAP's Kind demo cluster: the last level of the kill switch (infra/kind/README.md).
#
#   infra/kind/scripts/cleanup.sh [--yes] [--purge] [--images]
#
#   --yes     do not ask (otherwise the cluster's name must be typed to confirm)
#   --purge   also delete the private credentials: the administrator and the four identity
#             kubeconfigs and the recorded CA fingerprint (the folder too, once empty)
#   --images  also remove the five demo images from the local Docker engine
#
# Only `kind delete cluster --name recap-m5` touches the cluster, with --kubeconfig pointing at
# the private admin file, so ~/.kube/config is never read or changed. The container engine is
# left running; the README says how to stop and delete its profile.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

usage() { die "usage: cleanup.sh [--yes] [--purge] [--images]"; }

assume_yes=0
purge=0
images=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) assume_yes=1 ;;
    --purge) purge=1 ;;
    --images) images=1 ;;
    *) usage ;;
  esac
  shift
done

require_tools kind
check_kind_home

if [[ "$assume_yes" -ne 1 ]]; then
  [[ -t 0 ]] || die "not a terminal: pass --yes to delete the cluster without a question"
  printf 'Delete the Kind cluster %s and everything in it? Type its name to confirm: ' "$CLUSTER_NAME"
  read -r answer
  [[ "$answer" == "$CLUSTER_NAME" ]] || die "not confirmed: nothing was deleted"
fi

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  if [[ -f "$ADMIN_KUBECONFIG" ]]; then
    kind delete cluster --name "$CLUSTER_NAME" --kubeconfig "$ADMIN_KUBECONFIG"
  else
    # kind removes the cluster's entry from the kubeconfig it is given: give it a scratch file.
    scratch="$(mktemp -d)"
    kind delete cluster --name "$CLUSTER_NAME" --kubeconfig "$scratch/kubeconfig"
    rm -rf "$scratch"
  fi
  say "deleted cluster $CLUSTER_NAME"
else
  say "no Kind cluster named $CLUSTER_NAME: nothing to delete"
fi

if [[ "$images" -eq 1 ]]; then
  require_tools docker
  for image in $DEMO_IMAGES; do
    if docker image rm "recap-demo/$image" >/dev/null 2>&1; then
      say "removed image recap-demo/$image"
    fi
  done
fi

if [[ "$purge" -eq 1 ]]; then
  # Only the files the scripts write, by name (and the temporary files mint-tokens.sh may have
  # left after an interruption); never a recursive delete.
  rm -f "$ADMIN_KUBECONFIG" "$CA_FINGERPRINT_FILE"
  for identity in $IDENTITIES; do
    rm -f "$KIND_HOME/$identity.kubeconfig" "$KIND_HOME/.$identity".??????
  done
  if [[ -d "$KIND_HOME" ]] && ! rmdir "$KIND_HOME" 2>/dev/null; then
    warn "$KIND_HOME holds other files: left in place"
  fi
  say "purged the credentials in $KIND_HOME"
fi
say "optional, by hand: colima stop --profile recap-m5 && colima delete --profile recap-m5"
