# `infra/` — how RECAP runs (V1) and how it would be hosted later (not done)

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

A Docker/Compose setup is **not included**: it could not be tested on the build machine, and an
untested container recipe would be a false claim. It is listed in the V2 backlog.

## Later (V2+): a real benchmark host — plan only, nothing provisioned

The plan for running RECAP against SREGym on a Kubernetes-in-Docker (Kind) cluster is in
[`docs/benchmark-plan.md`](../docs/benchmark-plan.md). The hosting rules that plan follows:

1. **Explicit owner consent before any provisioning.** No script in this repo creates cloud resources.
2. **Check before spending:** the account's EC2 quotas, whether promotional credits actually apply
   to the chosen instance type and region, on-demand price, EBS volume and public-IPv4 charges.
3. **Budget alerts are not spending caps.** Set an AWS Budget with alerts *and* a hard personal
   rule: stop and terminate the instance after each benchmark session.
4. **Smallest instance that fits the pinned benchmark subset**, in one region, with a scheduled
   auto-stop; verify whether the chosen SREGym problems need privileged containers first.
5. **Keep the benchmark cluster separate** from RECAP's own SQLite memory and data, so benchmark
   state can never leak into experience memory, and test faults never into training memory.
6. **Tear down completely** (instance, volumes, snapshots, elastic IPs) and check the billing
   console afterwards.
