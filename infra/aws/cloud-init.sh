#!/bin/bash
# First boot of RECAP's M5 cluster host (Ubuntu 24.04, x86_64): Docker, kind and kubectl at
# the versions used on the Mac, checksums compared. Written by infra/aws/ec2.sh as user data;
# its end is marked by /var/lib/recap-ready. Nothing here opens a port or stores a secret.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y docker.io git rsync curl jq openssl
usermod -aG docker ubuntu
systemctl enable --now docker

cd /tmp
curl -fsSLo kind https://kind.sigs.k8s.io/dl/v0.33.0/kind-linux-amd64
curl -fsSLo kind.sha256 https://kind.sigs.k8s.io/dl/v0.33.0/kind-linux-amd64.sha256sum
[ "$(sha256sum kind | cut -d' ' -f1)" = "$(cut -d' ' -f1 kind.sha256)" ]
install -m 0755 kind /usr/local/bin/kind

curl -fsSLo kubectl https://dl.k8s.io/release/v1.37.2/bin/linux/amd64/kubectl
curl -fsSLo kubectl.sha256 https://dl.k8s.io/release/v1.37.2/bin/linux/amd64/kubectl.sha256
[ "$(sha256sum kubectl | cut -d' ' -f1)" = "$(cat kubectl.sha256)" ]
install -m 0755 kubectl /usr/local/bin/kubectl

# Kind runs every node as a container: give its kubelets enough inotify watches.
cat >/etc/sysctl.d/90-kind.conf <<'EOF'
fs.inotify.max_user_instances = 1024
fs.inotify.max_user_watches = 1048576
EOF
sysctl --system
touch /var/lib/recap-ready
