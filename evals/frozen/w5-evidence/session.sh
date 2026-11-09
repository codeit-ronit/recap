#!/usr/bin/env bash
# M6 W5 Stage B: the five held-out SREGym problems, one after another, in the frozen order
# (docs/w5-protocol.md, step 3). The owner runs it in their own terminal and stays reachable:
#
#   bash ~/recap-w5-evidence/stage-b/run-heldout.sh      # problems 1 to 5
#   bash ~/recap-w5-evidence/stage-b/run-heldout.sh 3    # carry on at problem 3 after a stop
#
# For each problem: the owner presses Enter (present); the freeze check and the cost meter; the
# VM (started again, with SREGym's cluster rebuilt, if its idle guard stopped it) and the tunnel
# with the pinned model; a rolling shutdown backstop; SREGym's harness deploys and injects the
# problem; RECAP runs and the owner approves or rejects each proposed action (nothing else);
# SREGym's grader grades it. The harness's and the grader's output go to files only, so no
# held-out answer, and no grade that could change how the next problem is approved, is shown
# before all five are done. A failed gate stops before the next problem; no problem is replaced.
# After the fifth: SREGym's cluster is torn down and the VM stopped.
set -uo pipefail

REPO="$HOME/Desktop/RECAP"
OUT="$HOME/recap-w5-evidence/stage-b"
DATA="$HOME/recap-w5-heldout"
INSTANCE=i-09a55033889ca1466
PINNED="sha256:a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f"
PROBLEMS=(
  duplicate_pvc_mounts_hotel_reservation
  liveness_probe_too_aggressive_hotel_reservation
  init_container_dependency_hang_hotel_reservation
  readiness_probe_misconfiguration_hotel_reservation
  sidecar_port_conflict_hotel_reservation
)
FIRST="${1:-1}"
BACKSTOP_MINUTES=110   # one problem: a restart and rebuild, a deploy, at most ~80 min of run and approvals
NEXT_HOURS=1.5         # the cost meter's room for one more problem

[[ "$FIRST" =~ ^[1-5]$ ]] || { echo "usage: $0 [first problem, 1-5]" >&2; exit 2; }
cd "$REPO" || exit 1
mkdir -p "$OUT"
n="$FIRST"
WATCHER=""
TUNNEL=""
trap '[[ -n "$WATCHER" ]] && kill "$WATCHER" 2>/dev/null; [[ -n "$TUNNEL" ]] && kill "$TUNNEL" 2>/dev/null' EXIT

log() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*" | tee -a "$OUT/timeline.txt"; }
halt() {
  log "STOPPED at problem $n: $*"
  printf '\n>>> Stopped. Tell Claude: "stopped at problem %s"\n' "$n"
  exit 1
}
vm() { bash infra/aws/ec2.sh ssh "$1" </dev/null; }  # stdin closed: ssh must not eat your keys
vm_state() {
  aws --profile amlc-personal --region ap-south-1 ec2 describe-instances --instance-ids "$INSTANCE" \
    --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || echo unknown
}

db() {  # one value from the held-out data folder (read only; empty when there is none yet)
  .venv/bin/python - "$DATA/recap.db" "$1" <<'EOF' 2>/dev/null
import sqlite3, sys
try:
    row = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True).execute(sys.argv[2]).fetchone()
    print("" if row is None or row[0] is None else row[0])
except sqlite3.Error:
    print("")
EOF
}
runs() { local c; c="$(db 'select count(*) from runs')"; echo "${c:-0}"; }
LATEST="(select run_id from runs order by created_at desc limit 1)"

model_ok() {  # the pinned weights, through the SSH tunnel (no inference)
  .venv/bin/python - "$PINNED" <<'EOF'
import json, sys, urllib.request
from recap.providers.ollama_provider import weights_digest
request = urllib.request.Request(
    "http://127.0.0.1:11435/api/show", data=json.dumps({"model": "qwen3:8b"}).encode(),
    headers={"Content-Type": "application/json"})
weights = weights_digest(json.load(urllib.request.urlopen(request, timeout=30)))
print("model through the tunnel: qwen3:8b", weights)
sys.exit(0 if weights == sys.argv[1] else 1)
EOF
}

close_tunnel() { [[ -n "$TUNNEL" ]] && kill "$TUNNEL" 2>/dev/null; TUNNEL=""; }

ensure_vm() {  # the VM, SREGym's cluster and the tunnel, as each problem needs them
  local state
  state="$(vm_state)"
  if [[ "$state" != "running" ]]; then
    # A four-node Kind cluster does not reliably survive a stopped VM: always rebuild it.
    log "problem $n: the VM is $state (its idle guard stops it when unused): starting it and rebuilding SREGym's cluster (about 6 minutes)"
    close_tunnel
    { date -u +%FT%TZ; bash infra/aws/ec2.sh up </dev/null; } >>"$OUT/up.txt" 2>&1 || halt "the VM could not be started (up.txt)"
    vm "sudo shutdown -h +$BACKSTOP_MINUTES" >>"$OUT/safeguards.txt" 2>&1 || halt "the VM's shutdown backstop could not be set"
    { date -u +%FT%TZ; vm "cd ~/recap && bash infra/sregym/sregym.sh teardown && bash infra/sregym/sregym.sh setup"; } \
      >>"$OUT/rebuild.txt" 2>&1 || halt "SREGym's cluster could not be rebuilt (rebuild.txt)"
    log "problem $n: the VM is running and SREGym's cluster is new"
  fi
  model_ok >/dev/null 2>&1 && return 0
  close_tunnel
  date -u +%FT%TZ >>"$OUT/tunnel.log"
  bash infra/aws/ec2.sh tunnel </dev/null >>"$OUT/tunnel.log" 2>&1 &
  TUNNEL=$!
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    sleep 2
    model_ok >/dev/null 2>&1 && { log "problem $n: the tunnel to the VM is open"; return 0; }
  done
  halt "the tunnel to the VM did not open (tunnel.log)"
}

watch_approval() {  # ring and notify when the run waits for you (best effort)
  local seen="" id
  while sleep 10; do
    id="$(db "select proposal_id from approval_requests where status = 'pending' order by requested_at desc limit 1")"
    if [[ -n "$id" && "$id" != "$seen" ]]; then
      seen="$id"
      printf '\a' >/dev/tty 2>/dev/null
      osascript -e "display notification \"RECAP is waiting for your approval (it expires in 15 minutes)\" with title \"W5 held-out problem $n\" sound name \"Glass\"" >/dev/null 2>&1
    fi
  done
}

# SREGym's harness serves its own agent's MCP port (9954) on every interface of the VM; RECAP
# never uses it. The security group admits SSH only, and this keeps the port on loopback: the
# harness gets the port free before a deploy, and its forward is replaced by a loopback one after.
FREE_9954='P=$(ss -ltnpH "sport = :9954" | grep -o "pid=[0-9]*" | cut -d= -f2 | sort -u); [ -z "$P" ] || kill $P'
LOOPBACK_9954='P=$(ss -ltnpH "sport = :9954" | grep -v "127.0.0.1:9954" | grep -o "pid=[0-9]*" | cut -d= -f2 | sort -u)
[ -z "$P" ] || kill $P; sleep 1
ss -ltnH "sport = :9954" | grep -q . || { setsid nohup kubectl --context kind-kind port-forward svc/mcp-server 9954:9954 -n sregym --address 127.0.0.1 >/dev/null 2>&1 </dev/null & sleep 3; }
ss -ltnH "sport = :9954"'

for ((n = FIRST; n <= 5; n++)); do
  problem="${PROBLEMS[n - 1]}"
  scenario="sregym-w5-$n"
  printf '\n================ held-out problem %s of 5 (scenario %s) ================\n' "$n" "$scenario"
  read -r -p "Press Enter to start it (Ctrl-C stops here; nothing has started) " _
  log "problem $n: gate"
  grep -q "problem: $problem" "fixtures/sregym-scenarios/$scenario.yaml" || halt "$scenario.yaml does not name its problem"
  [[ "$(runs)" == "$((n - 1))" ]] || halt "the held-out data folder holds $(runs) runs, not $((n - 1))"
  .venv/bin/python scripts/w5_freeze.py check 2>&1 | tee "$OUT/freeze-w5-$n.txt" || halt "the freeze check failed"
  .venv/bin/python scripts/aws_cost_meter.py status --next-hours "$NEXT_HOURS" 2>&1 | tee "$OUT/cost-w5-$n.txt" \
    || halt "the cost meter says stop"
  ensure_vm
  model_ok 2>&1 | tee "$OUT/model-w5-$n.txt" || halt "the model is not reachable through the tunnel with its pinned weights"
  vm "sudo shutdown -h +$BACKSTOP_MINUTES" >>"$OUT/safeguards.txt" 2>&1 || halt "the VM's shutdown backstop could not be set"
  tail -1 "$OUT/safeguards.txt"

  log "problem $n: SREGym deploys and injects it (about 5 minutes; its output goes to a file)"
  vm "$FREE_9954" >/dev/null 2>&1
  vm "cd ~/recap && bash infra/sregym/sregym.sh deploy $problem" >"$OUT/deploy-w5-$n.txt" 2>&1 \
    || halt "SREGym's deploy failed (deploy-w5-$n.txt); RECAP did not run"
  vm "$LOOPBACK_9954" >"$OUT/port-9954-w5-$n.txt" 2>&1
  awk '$4 ~ /:9954$/ && $4 != "127.0.0.1:9954" { bad = 1 } END { exit bad }' "$OUT/port-9954-w5-$n.txt" \
    || halt "port 9954 listens beyond the VM's loopback"
  bash infra/aws/ec2.sh creds sregym </dev/null >"$OUT/creds-w5-$n.txt" 2>&1 || halt "RECAP's identities could not be copied"

  log "problem $n: deployed. RECAP runs now: approve (type the service's name) or reject (anything else)"
  watch_approval &
  WATCHER=$!
  bash infra/aws/ec2.sh hold env -u RECAP_PROVIDER -u RECAP_ENABLE_EMBEDDINGS -u RECAP_KNOWLEDGE_OVERLAYS -u RECAP_REPLAY \
    RECAP_TARGET=sregym-hotel RECAP_DATA_DIR="$DATA" \
    .venv/bin/python scripts/demo.py --scenario "$scenario" --provider ollama-vm --approve ask
  rc=$?
  kill "$WATCHER" 2>/dev/null
  wait "$WATCHER" 2>/dev/null
  WATCHER=""
  log "problem $n: RECAP's run ended (exit $rc): $(db "select run_id || ' ' || state from runs where run_id = $LATEST")"
  [[ "$(runs)" == "$n" ]] || halt "RECAP left no run record (exit $rc): deployed, not run, not graded"

  log "problem $n: SREGym grades it (its output goes to a file)"
  vm "cd ~/recap && bash infra/sregym/sregym.sh grade $problem" >"$OUT/grader-w5-$n.txt" 2>&1
  echo "exit $?" >>"$OUT/grader-w5-$n.txt"
  log "problem $n: graded"
  [[ "$rc" -le 1 ]] || halt "RECAP's run ended abnormally (exit $rc); it was graded"
  [[ "$(db "select count(*) from approval_requests where status = 'expired' and run_id = $LATEST")" == "0" ]] \
    || halt "an approval expired (nobody answered within 15 minutes); it was graded"
done

log "all five held-out problems attempted: SREGym's cluster is torn down and the VM stopped"
vm "cd ~/recap && bash infra/sregym/sregym.sh teardown" >"$OUT/teardown.txt" 2>&1 || log "teardown failed (teardown.txt)"
close_tunnel
bash infra/aws/ec2.sh stop </dev/null 2>&1 | tee "$OUT/stop.txt"
printf '\nAll five are done. SREGym grader, problem by problem:\n'
for i in 1 2 3 4 5; do
  printf '  sregym-w5-%s  %s\n' "$i" "$(grep -m1 -o 'Success: [A-Za-z]*' "$OUT/grader-w5-$i.txt" 2>/dev/null || echo 'not graded')"
done
printf '\n>>> Tell Claude: done\n'
