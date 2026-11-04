#!/bin/bash
# Stop this VM when RECAP does not use it (M5; infra/aws/ec2.sh up installs it as
# /usr/local/sbin/recap-idle-guard with a systemd timer that runs it every 5 minutes).
#
# The owner's rule is that the VM must not run idle. A check on the Mac cannot keep that rule:
# a sleeping Mac checks nothing. This one runs on the VM itself. The VM is busy while any of
# these holds, and idle otherwise:
#   - the model is loaded in Ollama (RECAP asks Ollama to keep it 10 minutes after a request);
#   - an SSH session runs a command or a shell (`ec2.sh ssh ...`, a login; the tunnel alone,
#     `ssh -N`, runs nothing and does not count);
#   - an ec2.sh command touched ~ubuntu/.recap-keepalive in the last 45 minutes (`ec2.sh hold`
#     does every 10 minutes while a run lasts: its approval wait and recovery window use neither
#     the model nor a command here).
# Idle means shut down. The instance's shutdown behaviour is "stop" (ec2.sh sets it at launch),
# so its disk and both clusters are kept, compute billing ends, and `ec2.sh up` starts it again.
set -u

KEEPALIVE=/home/ubuntu/.recap-keepalive
GRACE_SECONDS=$((45 * 60))

busy() {
  if curl -fsS --max-time 3 http://127.0.0.1:11434/api/ps 2>/dev/null | grep -q '"name"'; then
    echo "the model is loaded"
    return 0
  fi
  local session
  for session in $(pgrep -u ubuntu -x sshd 2>/dev/null); do
    if pgrep -P "$session" >/dev/null 2>&1; then
      echo "an SSH session runs a command"
      return 0
    fi
  done
  if [ -f "$KEEPALIVE" ] && [ $(($(date +%s) - $(stat -c %Y "$KEEPALIVE"))) -lt "$GRACE_SECONDS" ]; then
    echo "an ec2.sh command ran in the last 45 minutes"
    return 0
  fi
  return 1
}

if reason="$(busy)"; then
  exit 0
fi
logger -t recap-idle-guard "idle (no model, no command, nothing from ec2.sh for 45 min): stopping"
shutdown -h now
