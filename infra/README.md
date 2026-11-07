# `infra/`: how RECAP runs (V1), its local Kind demo (M5, files only) and later hosting (not done)

## V1: everything is local, nothing is deployed

| Piece | How it runs | Network |
|---|---|---|
| API | `uvicorn apps.api.main:app` (FastAPI) | localhost only |
| Dashboard | `npm run dev` in `apps/dashboard` (or `npm run build`, served by the API) | localhost only |
| MCP tool server | spawned per run as a subprocess (`python -m recap.tools.mcp_server`), stdio transport | none |
| Simulator state | one SQLite file per run under `data/sandbox/` | none |
| Runs, traces, approvals, memory | SQLite + JSONL under `data/` | none |

V1 makes **no cloud calls, no paid API calls, and needs no Kubernetes or GPU**. The optional
Anthropic provider only runs if you explicitly select it and supply your own API key.

A Docker/Compose setup for RECAP itself is **not included**: it could not be tested on the build
machine, and an untested container recipe would be a false claim. It is listed in the V2 backlog.

## M5: a local Kind cluster as a real target (files only, nothing run yet)

[`infra/kind/`](kind/README.md) holds everything M5's first real target needs, as files: the
Kind cluster config (one node, the API server and every host port on 127.0.0.1), a
standard-library demo program with two services and deliberate defects, its Dockerfile, the
manifests (Pod Security `restricted`, RECAP's four least-privilege identities, the admission
policy, a small Prometheus, a traffic generator) and the owner's scripts to set up, inspect,
break, reset and delete it. The plan behind it is
[`docs/m5-integration-plan.md`](../docs/m5-integration-plan.md), sections 3 and 4.

**No cluster exists yet, and no image was built.** `kind`, `kubectl` and a running container
engine are not installed on the build machine; installing them waits for the owner's
approval. The demo program and the manifests are checked offline by
`tests/unit/test_kind_demo_app.py` and `tests/unit/test_kind_manifests.py`. The cluster's
credentials (the administrator and RECAP's four kubeconfigs, the recorded CA fingerprint)
will live in `~/.config/recap/kind/`, outside the repository, and are never committed.

## The benchmark host (M5): the owner's AWS VM

The plan for running RECAP against SREGym is in
[`docs/benchmark-plan.md`](../docs/benchmark-plan.md) and
[`docs/m5-integration-plan.md`](../docs/m5-integration-plan.md) (sections 6 and 8). The hosting
rules those plans follow:

1. **Explicit owner consent before any provisioning.** Only [`infra/aws/ec2.sh`](aws/ec2.sh) creates
   cloud resources, and only when the owner runs it (M5: one VM for the clusters and the model).
2. **Check before spending:** the account's EC2 quotas, whether promotional credits actually apply
   to the chosen instance type and region, on-demand price, EBS volume and public-IPv4 charges.
3. **Budget alerts are not spending caps.** Set an AWS Budget with alerts *and* a hard personal
   rule: stop the instance after each session (in M5 the VM's own idle guard also stops it after
   45 minutes unused) and terminate it at the end.
4. **Smallest instance that fits the pinned benchmark subset**, in one region, with a scheduled
   auto-stop; verify whether the chosen SREGym problems need privileged containers first.
5. **Keep the benchmark cluster separate** from RECAP's own SQLite memory and data, so benchmark
   state can never leak into experience memory, and test faults never into training memory.
6. **Tear down completely** (instance, volumes, snapshots, elastic IPs) and check the billing
   console afterwards.
