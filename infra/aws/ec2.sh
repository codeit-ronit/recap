#!/usr/bin/env bash
# RECAP's M5 cluster host on AWS: one EC2 VM that runs the Kind cluster (and later SREGym),
# while RECAP and the local model stay on the Mac and reach the cluster through an SSH tunnel.
#
#   infra/aws/ec2.sh up        create the VM (key pair, security group for your IP, encrypted
#                              disk, IMDSv2), or start it if it is stopped; waits for SSH
#   infra/aws/ec2.sh status    the VM's state and the hourly price it is paying for
#   infra/aws/ec2.sh ssh [cmd] a shell (or one command) on the VM
#   infra/aws/ec2.sh sync      copy infra/kind/ to the VM's ~/recap/infra/kind (nothing else)
#   infra/aws/ec2.sh tunnel    forward the cluster's loopback ports to this Mac's loopback
#                              (API server, NodePorts 30080/30081, Prometheus 30090)
#   infra/aws/ec2.sh stop      stop the VM: compute billing ends, the disk stays (~$0.45/day)
#   infra/aws/ec2.sh down      terminate the VM and delete its security group and key pair
#
# Settings (environment): AWS_PROFILE (default amlc-personal), AWS_REGION (default ap-south-1),
# RECAP_EC2_TYPE (default m6a.2xlarge), RECAP_EC2_DISK_GB (default 150).
# State outside git: ~/.config/recap/aws/recap-m5.env (instance and group ids) and
# ~/.ssh/recap-m5.pem (the private key, mode 400). Only port 22 is open, to this Mac's
# public IP; the cluster's ports are never exposed.
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
  ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$STATE_DIR/known_hosts" \
    -o ServerAliveInterval=30 -o ConnectTimeout=10 "ubuntu@$(public_ip)" "$@"
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
  # Kind's node container restarts only on failure, so a stopped VM brings the cluster back
  # down: start it again and wait until the API server answers.
  if ssh_ "docker inspect recap-m5-control-plane >/dev/null 2>&1"; then
    ssh_ "docker start recap-m5-control-plane >/dev/null && for i in \$(seq 1 60); do \
      kubectl --kubeconfig ~/.config/recap/kind/admin.kubeconfig get --raw=/readyz >/dev/null 2>&1 \
      && exit 0; sleep 3; done; exit 1" && say "cluster recap-m5 is up" \
      || die "the cluster did not come back; rebuild it with infra/kind/scripts/setup.sh on the VM"
  fi
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
    "$REPO_ROOT/infra/kind" "ubuntu@$(public_ip):recap/infra/"
  say "infra/kind copied to ~/recap/infra/kind on the VM"
}

cmd_tunnel() {
  load
  local api_port
  api_port="$(ssh_ "kubectl config view --kubeconfig ~/.config/recap/kind/admin.kubeconfig \
    --minify -o jsonpath='{.clusters[0].cluster.server}'" | sed -E 's|.*:([0-9]+)$|\1|')"
  [[ "$api_port" =~ ^[0-9]+$ ]] || die "could not read the API server's port on the VM"
  say "forwarding 127.0.0.1:{$api_port,30080,30081,30090} to the VM (Ctrl-C ends the tunnel)"
  exec ssh -i "$KEY" -o UserKnownHostsFile="$STATE_DIR/known_hosts" -o ServerAliveInterval=30 \
    -o ExitOnForwardFailure=yes -N \
    -L "127.0.0.1:$api_port:127.0.0.1:$api_port" -L 127.0.0.1:30080:127.0.0.1:30080 \
    -L 127.0.0.1:30081:127.0.0.1:30081 -L 127.0.0.1:30090:127.0.0.1:30090 "ubuntu@$(public_ip)"
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
  tunnel) cmd_tunnel ;;
  stop) cmd_stop ;;
  down) cmd_down ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
