#!/usr/bin/env bash
# Return RECAP's Kind demo to its baseline, as the recap-scenario identity: both services on
# v1.0.0, every pod ready, and the frontend answering 20 requests in a row.
#
#   infra/kind/scripts/reset.sh [--timeout SECONDS] [--restart-backend]
#
# A Deployment on another image is set back to v1.0.0 (one PATCH, which also replaces its pods).
# A Deployment already on v1.0.0 whose pods are not all ready (the report-cache leak) gets a
# rolling restart, which starts its pods with empty memory. A healthy baseline is left exactly
# as it is: no new revision, no restart. --restart-backend restarts the backend anyway, to clear
# a report cache that a burst filled only part of the way.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

usage() { die "usage: reset.sh [--timeout SECONDS] [--restart-backend]"; }

timeout=180
restart_backend=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout)
      [[ $# -ge 2 && "$2" =~ ^[0-9]+$ ]] || usage
      timeout="$2"
      shift 2
      ;;
    --restart-backend)
      restart_backend=1
      shift
      ;;
    *) usage ;;
  esac
done

require_tools kubectl openssl curl
check_kind_home
require_our_cluster
require_identity recap-scenario
ACTOR="recap-scenario"

for deploy in frontend backend; do
  baseline="recap-demo/$deploy:$BASELINE_VERSION"
  image="$(image_of "$deploy")"
  counts="$(ready_of "$deploy")"
  if [[ "$image" != "$baseline" ]]; then
    say "$deploy: $image -> $baseline"
    set_release "$deploy" "$BASELINE_VERSION"
  elif [[ "${counts%% *}" != "${counts##* }" ]]; then
    say "$deploy: on $baseline, ${counts%% *} of ${counts##* } pods ready: rolling restart"
    restart_deployment "$deploy"
  elif [[ "$deploy" == "backend" && "$restart_backend" -eq 1 ]]; then
    say "backend: rolling restart (--restart-backend)"
    restart_deployment backend
  else
    say "$deploy: already on $baseline with ${counts%% *} of ${counts##* } pods ready"
  fi
done

for deploy in frontend backend; do
  wait_rollout "$deploy" "$timeout" \
    || die "$deploy did not return to its baseline within ${timeout}s (status.sh shows why)"
done
wait_for_frontend 60 || die "the frontend still fails on $FRONTEND_URL (status.sh shows why)"
say "baseline restored"
