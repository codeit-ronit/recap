# SREGym on RECAP's M5 VM

One genuine SREGym problem, deployed and injected by SREGym's own harness, for RECAP to work
from the Mac (M5 W5, [ADR 0031](../../docs/decisions/0031-an-external-benchmark-harness-release-tags-and-pod-failure-onsets.md)).
SREGym is pinned at release 1.1, commit `a90b43cc973158120119a9f388081eac4f7adb36` (its
applications submodule at `afd5b947fbafefc110e7dc00acd698cb2683dc7f`).

## The order

```
# On the Mac
bash infra/aws/ec2.sh up                       # start the VM (installs its idle guard)
bash infra/aws/ec2.sh sync                     # infra/kind and infra/sregym to ~/recap/infra/
bash infra/aws/ec2.sh ssh 'cd ~/recap && bash infra/sregym/sregym.sh setup'
bash infra/aws/ec2.sh ssh 'cd ~/recap && bash infra/sregym/sregym.sh deploy misconfig_app_hotel_res'
bash infra/aws/ec2.sh creds sregym             # RECAP's three identities to ~/.config/recap/sregym
bash infra/sregym/get-kubectl.sh               # kubectl v1.32.1 for this target (once)
bash infra/aws/ec2.sh tunnel                   # in its own terminal: SREGym's API port too

# The run (a person approves the fix at the terminal). `hold` keeps the Mac awake and the VM
# marked in use until it ends: the approval wait uses neither the model nor a command there.
bash infra/aws/ec2.sh hold env RECAP_TARGET=sregym-hotel RECAP_DATA_DIR=$HOME/recap-sregym-runs \
  .venv/bin/python scripts/demo.py --scenario sregym-misconfig-app-hotel-res \
  --provider ollama-vm --approve ask

# SREGym's own verdict, after RECAP's fix
bash infra/aws/ec2.sh ssh 'cd ~/recap && bash infra/sregym/sregym.sh grade misconfig_app_hotel_res'

# When done
bash infra/aws/ec2.sh ssh 'cd ~/recap && bash infra/sregym/sregym.sh teardown'
bash infra/aws/ec2.sh stop
```

After the VM was stopped (by hand, or by its idle guard), SREGym's four-node Kind cluster may
not come back: `sregym.sh` then refuses to deploy because nodes stay NotReady. Recreate it with
`teardown` then `setup` (its images are cached, so this takes minutes), then `deploy` again.

## What it installs, and where

On the VM, as the `ubuntu` user: Helm v4.3.1 (SREGym needs Helm 4), uv 0.13.1 and kubectl
v1.32.1 (SREGym's Kubernetes is 1.32.1) in `~/.local/recap-sregym/bin`, each checked against
its published SHA-256; SREGym in `~/sregym` at the pinned commits, its Python environment from
its own `uv.lock` (`uv sync --frozen`); SREGym's four-node Kind cluster (`kind-kind`). On the
Mac: kubectl v1.32.1 in `~/.config/recap/sregym/bin` (private, never a system path) and RECAP's
identity kubeconfigs next to it. Nothing goes into the repository.

## Safety

- SREGym's harness acts as cluster-admin on the current kube context and resets the whole
  cluster to its baseline. Every `sregym.sh` command that reaches a cluster first checks that
  the context is SREGym's own `kind-kind`, on 127.0.0.1, with the CA recorded at setup. RECAP's
  demo cluster keeps its credentials in a private file SREGym never reads, and is stopped while
  SREGym's cluster exists: every `sregym.sh` command stops it, `ec2.sh up` leaves it stopped,
  and `teardown` starts it again.
- RECAP never uses SREGym's administrator credential or its agent tools (a kubectl passthrough
  and a shell, cluster-wide). It works through three ServiceAccounts of its own
  ([recap-rbac.yaml](recap-rbac.yaml)): read-only reader and oracle, and a writer that may only
  patch the app's Deployments, in the app's namespace only. Unlike the Kind demo, no admission
  policy checks the shape of the writer's patches here: RBAC alone limits it. The approval rules are the Kind
  demo's: a person types the target service's name, for that one action, once.
- SREGym's conductor API and MCP port-forward listen on the VM only; the VM's security group
  opens nothing but SSH, to the owner's IP.

## Grading

Two verdicts, never merged. RECAP's own oracle (rollout complete, pods ready) decides RECAP's
RESOLVED. SREGym's mitigation oracle (`run-oracle.py`, exit 0 = pass) is the benchmark's verdict:
for `misconfig_app_hotel_res` it passes once geo no longer runs the image SREGym injected. No
LLM judge is used (SREGym's diagnosis stage would call one), and the human approval makes these
runs human-in-the-loop, not comparable with SREGym's leaderboard.
