#!/usr/bin/env bash
# RECAP's M5 cluster host on AWS: one EC2 VM that runs the Kind demo cluster, SREGym's own
# cluster (infra/sregym/) and the model (Ollama), while RECAP stays on the Mac and reaches them
# through an SSH tunnel.
#
#   infra/aws/ec2.sh up        create the VM (key pair, security group for your IP, encrypted
#                              disk, IMDSv2), or start it if it is stopped; waits for SSH
#   infra/aws/ec2.sh status    the VM's state and the hourly price it is paying for
#   infra/aws/ec2.sh ssh [cmd] a shell (or one command) on the VM
#   infra/aws/ec2.sh sync      copy infra/kind/ and infra/sregym/ to the VM's ~/recap/infra/
#                              (nothing else of the repository)
#   infra/aws/ec2.sh tunnel    forward the clusters' loopback ports to this Mac's loopback
#                              (the Kind demo's API server, NodePorts 30080/30081, Prometheus
#                              30090; SREGym's API server when its cluster exists) and the
#                              VM's Ollama (Mac 127.0.0.1:11435 -> VM 127.0.0.1:11434)
#   infra/aws/ec2.sh creds kind|sregym
#                              copy RECAP's identity kubeconfigs and ca.sha256 for that cluster
#                              from the VM to this Mac's private ~/.config/recap/<cluster>/
#   infra/aws/ec2.sh hold CMD  run CMD on this Mac (a RECAP run), kept awake, with the VM marked
#                              in use until CMD ends (an approval wait uses nothing on the VM)
#   infra/aws/ec2.sh stop      stop the VM: compute billing ends, the disk stays (~$0.45/day)
#   infra/aws/ec2.sh down      terminate the VM and delete its security group and key pair
#
# Settings (environment): AWS_PROFILE (default amlc-personal), AWS_REGION (default ap-south-1),
# RECAP_EC2_TYPE (default m6a.2xlarge), RECAP_EC2_DISK_GB (default 150).
# State outside git: ~/.config/recap/aws/recap-m5.env (instance and group ids) and
# ~/.ssh/recap-m5.pem (the private key, mode 400). Only port 22 is open, to this Mac's
# public IP; the cluster's ports are never exposed. `up` also installs the VM's own idle
# guard (infra/aws/idle-guard.sh), which stops the VM even while this Mac sleeps: once the model
# is unloaded (10 minutes after RECAP's last request), no command runs there and no ec2.sh call
# (`hold` makes one every 10 minutes) touched it for 45 minutes.
set -euo pipefail

PROFILE="${AWS_PROFILE:-amlc-personal}"
REGION="${AWS_REGION:-ap-south-1}"
TYPE="${RECAP_EC2_TYPE:-m6a.2xlarge}"
DISK_GB="${RECAP_EC2_DISK_GB:-150}"
NAME="recap-m5"
STATE_DIR="$HOME/.config/recap/aws"
STATE="$STATE_DIR/$NAME.env"
KEY="$HOME/.ssh/$NAME.pem"
HERE="$(cd "$(dirname "$0")" && pwd -P)"
REPO_ROOT="$(cd "$HERE/../.." && pwd -P)"
AMI_PARAM="/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"

aws_() { aws --profile "$PROFILE" --region "$REGION" "$@"; }
say() { printf '[recap-aws] %s\n' "$*"; }
die() { printf '[recap-aws] error: %s\n' "$*" >&2; exit 1; }
load() { [[ -f "$STATE" ]] && source "$STATE"; true; }
save() { (umask 077 && mkdir -p "$STATE_DIR" && printf 'INSTANCE_ID=%s\nGROUP_ID=%s\n' \
  "${INSTANCE_ID:-}" "${GROUP_ID:-}" >"$STATE"); }
instance_state() {
  [[ -n "${INSTANCE_ID:-}" ]] || { echo none; return; }
  aws_ ec2 describe-instances --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || echo none
}
public_ip() {
  aws_ ec2 describe-instances --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text
}
ssh_() {
  # Every command marks the VM as in use for the idle guard (an interactive login counts by
  # itself while it lasts).
  local keepalive=()
  [[ $# -gt 0 ]] && keepalive=("touch ~/.recap-keepalive 2>/dev/null;")
  ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$STATE_DIR/known_hosts" \
    -o ServerAliveInterval=30 -o ConnectTimeout=10 "ubuntu@$(public_ip)" \
    ${keepalive[@]+"${keepalive[@]}"} "$@"  # (bash 3.2: an empty array is "unbound" under set -u)
}

ensure_key() {
  if [[ ! -f "$KEY" ]]; then
    aws_ ec2 describe-key-pairs --key-names "$NAME" >/dev/null 2>&1 \
      && die "key pair $NAME exists in AWS but $KEY is missing: run 'down' first"
    (umask 077 && aws_ ec2 create-key-pair --key-name "$NAME" --key-type ed25519 \
      --query KeyMaterial --output text >"$KEY")
    chmod 400 "$KEY"
    say "created key pair $NAME (private key in ~/.ssh, never in the repository)"
  fi
}

ensure_group() {
  local ip vpc
  ip="$(curl -fsS --max-time 10 https://checkip.amazonaws.com)/32"
  if [[ -z "${GROUP_ID:-}" ]]; then
    vpc="$(aws_ ec2 describe-vpcs --filters Name=isDefault,Values=true \
      --query 'Vpcs[0].VpcId' --output text)"
    GROUP_ID="$(aws_ ec2 create-security-group --group-name "$NAME" --vpc-id "$vpc" \
      --description "RECAP M5 - SSH from the owner IP only" --query GroupId --output text)"
    save
    say "created security group $GROUP_ID"
  fi
  # Exactly one inbound rule: SSH from this Mac's current public IP.
  local rules
  rules="$(aws_ ec2 describe-security-groups --group-ids "$GROUP_ID" \
    --query 'SecurityGroups[0].IpPermissions' --output json)"
  if [[ "$rules" != "[]" ]]; then
    aws_ ec2 revoke-security-group-ingress --group-id "$GROUP_ID" --ip-permissions "$rules" >/dev/null
  fi
  aws_ ec2 authorize-security-group-ingress --group-id "$GROUP_ID" \
    --protocol tcp --port 22 --cidr "$ip" >/dev/null
  say "SSH allowed from $ip only"
}

wait_ssh() {
  local tries=0
  until ssh_ true 2>/dev/null; do
    tries=$((tries + 1)); [[ $tries -le 60 ]] || die "SSH did not answer within 5 minutes"
    sleep 5
  done
}

cmd_up() {
  load
  ensure_key
  ensure_group
  case "$(instance_state)" in
    running) say "already running" ;;
    stopped) aws_ ec2 start-instances --instance-ids "$INSTANCE_ID" >/dev/null
      aws_ ec2 wait instance-running --instance-ids "$INSTANCE_ID"; say "started" ;;
    stopping) aws_ ec2 wait instance-stopped --instance-ids "$INSTANCE_ID"
      aws_ ec2 start-instances --instance-ids "$INSTANCE_ID" >/dev/null
      aws_ ec2 wait instance-running --instance-ids "$INSTANCE_ID"; say "started" ;;
    none|terminated|shutting-down)
      local ami
      ami="$(aws_ ssm get-parameter --name "$AMI_PARAM" --query Parameter.Value --output text)"
      INSTANCE_ID="$(aws_ ec2 run-instances --image-id "$ami" --instance-type "$TYPE" \
        --key-name "$NAME" --security-group-ids "$GROUP_ID" \
        --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$DISK_GB,VolumeType=gp3,DeleteOnTermination=true,Encrypted=true}" \
        --metadata-options HttpTokens=required,HttpEndpoint=enabled \
        --instance-initiated-shutdown-behavior stop \
        --user-data "file://$HERE/cloud-init.sh" \
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME},{Key=project,Value=recap}]" \
          "ResourceType=volume,Tags=[{Key=Name,Value=$NAME},{Key=project,Value=recap}]" \
        --query 'Instances[0].InstanceId' --output text)"
      save
      say "launched $INSTANCE_ID ($TYPE, ${DISK_GB} GB gp3, $REGION)"
      aws_ ec2 wait instance-running --instance-ids "$INSTANCE_ID" ;;
    *) die "instance $INSTANCE_ID is $(instance_state)" ;;
  esac
  wait_ssh
  say "first-boot setup: waiting for Docker, kind and kubectl"
  local tries=0
  until ssh_ test -f /var/lib/recap-ready; do
    tries=$((tries + 1)); [[ $tries -le 90 ]] || die "first-boot setup did not finish (see /var/log/cloud-init-output.log)"
    sleep 10
  done
  say "ready: $(ssh_ 'docker --version; kind version; kubectl version --client | head -1' | tr '\n' ' ')"
  install_idle_guard
  # Kind's node container restarts only on failure, so a stopped VM brings the cluster back
  # down: start it again and wait until the API server answers. Not while SREGym's cluster
  # exists (infra/sregym): one cluster at a time, and its teardown starts the demo again.
  if ssh_ "docker ps -aq --filter label=io.x-k8s.kind.cluster=kind | grep -q ."; then
    say "SREGym's cluster exists: RECAP's demo cluster stays stopped (sregym.sh teardown starts it)"
  elif ssh_ "docker inspect recap-m5-control-plane >/dev/null 2>&1"; then
    ssh_ "docker start recap-m5-control-plane >/dev/null && for i in \$(seq 1 60); do \
      kubectl --kubeconfig ~/.config/recap/kind/admin.kubeconfig get --raw=/readyz >/dev/null 2>&1 \
      && exit 0; sleep 3; done; exit 1" && say "cluster recap-m5 is up" \
      || die "the cluster did not come back; rebuild it with infra/kind/scripts/setup.sh on the VM"
  fi
}

install_idle_guard() {
  # The guard and a systemd timer that runs it every 5 minutes (first 15 minutes after boot).
  ssh_ "sudo install -m 0755 /dev/stdin /usr/local/sbin/recap-idle-guard" <"$HERE/idle-guard.sh"
  ssh_ "sudo tee /etc/systemd/system/recap-idle-guard.service >/dev/null" <<'EOF'
[Unit]
Description=Stop this VM when RECAP does not use it (infra/aws/idle-guard.sh)

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/recap-idle-guard
EOF
  ssh_ "sudo tee /etc/systemd/system/recap-idle-guard.timer >/dev/null" <<'EOF'
[Unit]
Description=Check every 5 minutes whether RECAP uses this VM

[Timer]
OnBootSec=15min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
  ssh_ "sudo systemctl daemon-reload && sudo systemctl enable --now recap-idle-guard.timer >/dev/null 2>&1"
  say "idle guard on: the VM stops itself after 45 minutes unused"
}

cmd_status() {
  load
  say "instance ${INSTANCE_ID:-none}: $(instance_state) ($TYPE in $REGION)"
}

cmd_sync() {
  load
  # The repository's layout (~/recap/infra/kind), so the scripts find their repository root
  # there and keep the credentials folder (~/.config/recap/kind) outside it.
  ssh_ "mkdir -p recap/infra && rm -rf recap-infra"
  rsync -az --delete -e "ssh -i $KEY -o UserKnownHostsFile=$STATE_DIR/known_hosts" \
    "$REPO_ROOT/infra/kind" "$REPO_ROOT/infra/sregym" "ubuntu@$(public_ip):recap/infra/"
  say "infra/kind and infra/sregym copied to ~/recap/infra/ on the VM"
}

cmd_tunnel() {
  load
  local api_port
  api_port="$(ssh_ "kubectl config view --kubeconfig ~/.config/recap/kind/admin.kubeconfig \
    --minify -o jsonpath='{.clusters[0].cluster.server}'" | sed -E 's|.*:([0-9]+)$|\1|')"
  [[ "$api_port" =~ ^[0-9]+$ ]] || die "could not read the API server's port on the VM"
  local forwards=(-L "127.0.0.1:$api_port:127.0.0.1:$api_port")
  forwards+=(-L 127.0.0.1:30080:127.0.0.1:30080 -L 127.0.0.1:30081:127.0.0.1:30081)
  forwards+=(-L 127.0.0.1:30090:127.0.0.1:30090 -L 127.0.0.1:11435:127.0.0.1:11434)
  # SREGym's own cluster (infra/sregym/), when it exists: its API server on the VM's loopback.
  local sregym_port
  sregym_port="$(ssh_ "kubectl config view --context kind-kind --minify \
    -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null" | sed -E 's|.*:([0-9]+)$|\1|' || true)"
  if [[ "$sregym_port" =~ ^[0-9]+$ ]]; then
    forwards+=(-L "127.0.0.1:$sregym_port:127.0.0.1:$sregym_port")
    say "SREGym's API server: 127.0.0.1:$sregym_port"
  fi
  say "forwarding the clusters' ports and the model (11435 -> 11434) to the VM (Ctrl-C ends the tunnel)"
  exec ssh -i "$KEY" -o UserKnownHostsFile="$STATE_DIR/known_hosts" -o ServerAliveInterval=30 \
    -o ExitOnForwardFailure=yes -N "${forwards[@]}" "ubuntu@$(public_ip)"
}

cmd_creds() {
  # RECAP's identity kubeconfigs (short-lived tokens) and the CA fingerprint, VM -> this Mac,
  # as one tar stream over SSH: no token on a command line, files mode 600 in a 700 folder.
  local cluster="${1:-}" files
  case "$cluster" in
    kind) files="recap-reader.kubeconfig recap-writer.kubeconfig recap-oracle.kubeconfig recap-scenario.kubeconfig ca.sha256" ;;
    sregym) files="recap-reader.kubeconfig recap-writer.kubeconfig recap-oracle.kubeconfig ca.sha256" ;;
    *) die "usage: ec2.sh creds kind|sregym" ;;
  esac
  local dest="$HOME/.config/recap/$cluster"
  (umask 077 && mkdir -p "$dest")
  load
  ssh_ "tar -C ~/.config/recap/$cluster -cf - $files" | (umask 077 && tar -C "$dest" -xf -)
  say "copied RECAP's $cluster identities to ~/.config/recap/$cluster"
}

HOLD_SECONDS=600  # well inside the idle guard's 45 minutes

cmd_hold() {
  # A run's approval wait and recovery window use neither the model nor a command on the VM:
  # without this, the idle guard could stop the VM under a run.
  [[ $# -gt 0 ]] || die "usage: ec2.sh hold COMMAND [ARGS...]"
  load
  ssh_ true </dev/null || die "the VM is not reachable (ec2.sh up)"
  (while sleep "$HOLD_SECONDS"; do ssh_ true </dev/null >/dev/null 2>&1 || true; done) 2>/dev/null &
  TOUCHER=$!  # global: the EXIT trap runs after this function has returned
  trap 'pkill -P "$TOUCHER" 2>/dev/null; kill "$TOUCHER" 2>/dev/null || true' EXIT
  command -v caffeinate >/dev/null && set -- caffeinate -i "$@"  # macOS: no sleep mid-run
  "$@"
}

cmd_stop() {
  load
  [[ "$(instance_state)" == "running" ]] || { say "not running"; return; }
  aws_ ec2 stop-instances --instance-ids "$INSTANCE_ID" >/dev/null
  aws_ ec2 wait instance-stopped --instance-ids "$INSTANCE_ID"
  say "stopped: compute billing has ended; the disk is kept"
}

cmd_down() {
  load
  if [[ -n "${INSTANCE_ID:-}" && "$(instance_state)" != "terminated" && "$(instance_state)" != "none" ]]; then
    aws_ ec2 terminate-instances --instance-ids "$INSTANCE_ID" >/dev/null
    aws_ ec2 wait instance-terminated --instance-ids "$INSTANCE_ID"
    say "terminated $INSTANCE_ID (its disk was deleted with it)"
  fi
  [[ -n "${GROUP_ID:-}" ]] && aws_ ec2 delete-security-group --group-id "$GROUP_ID" && say "deleted $GROUP_ID"
  aws_ ec2 delete-key-pair --key-name "$NAME" >/dev/null 2>&1 && rm -f "$KEY" && say "deleted key pair"
  rm -f "$STATE"
}

case "${1:-}" in
  up) cmd_up ;;
  status) cmd_status ;;
  ssh) shift; load; ssh_ "$@" ;;
  sync) cmd_sync ;;
  creds) shift; cmd_creds "${1:-}" ;;
  tunnel) cmd_tunnel ;;
  hold) shift; cmd_hold "$@" ;;
  stop) cmd_stop ;;
  down) cmd_down ;;
  *) sed -n '2,31p' "$0"; exit 2 ;;
esac
