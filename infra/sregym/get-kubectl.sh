#!/usr/bin/env bash
# kubectl v1.32.1 for RECAP's SREGym target, on the machine RECAP runs on (M5 W5).
#
# SREGym's pinned node image runs Kubernetes 1.32.1, and RECAP's preflight refuses a kubectl more
# than one minor version away from the API server (Kubernetes' own support policy). The Kind demo
# keeps its own kubectl. This downloads the official binary into RECAP's private SREGym folder
# (never a system path), checks its published SHA-256 and prints its version; the target profile
# (configs/targets/sregym-hotel/environment.yaml) names that path.
set -euo pipefail

VERSION="v1.32.1"
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) platform="darwin/arm64" ;;
  Darwin-x86_64) platform="darwin/amd64" ;;
  Linux-x86_64) platform="linux/amd64" ;;
  *) echo "[recap-sregym] error: no kubectl download for $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac
dest="$HOME/.config/recap/sregym/bin"
(umask 077 && mkdir -p "$dest")
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
curl -fsSLo "$work/kubectl" "https://dl.k8s.io/release/$VERSION/bin/$platform/kubectl"
curl -fsSLo "$work/kubectl.sha256" "https://dl.k8s.io/release/$VERSION/bin/$platform/kubectl.sha256"
if [[ "$(shasum -a 256 "$work/kubectl" | cut -d' ' -f1)" != "$(cat "$work/kubectl.sha256")" ]]; then
  echo "[recap-sregym] error: kubectl checksum mismatch: nothing installed" >&2
  exit 1
fi
install -m 0755 "$work/kubectl" "$dest/kubectl"
echo "[recap-sregym] $("$dest/kubectl" version --client 2>/dev/null | head -1) at $dest/kubectl"
