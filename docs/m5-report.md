# M5 report: RECAP on real Kubernetes, and on SREGym

M5 took RECAP from a simulated shop to real Kubernetes clusters. A real model investigates
a real failure through RECAP's own read tools. RECAP proposes one exact fix, a person approves
it, a restricted identity applies it, and an independent oracle checks the recovery. All of
it is recorded. This report is the handoff; [m5-progress.md](m5-progress.md) is the log,
workstream by workstream.

Nothing here ran in production. The clusters are Kind clusters on the owner's own machines.
The owner approved every real-model fix that ran on them; the scripted live acceptance tests
of W3 approved (or, in two tests, signed directly) their own writes as a test harness
(`operator:live-acceptance`).

## 1. What was built

| Part | What it is |
|---|---|
| Kubernetes adapter | `KubernetesEnvironment` ([recap/environments/kubernetes/](../recap/environments/kubernetes/)) behind the same `TargetEnvironment` interface as the simulator: typed `kubectl` operations, one least-privilege identity per role, cluster pinning, a namespace lease, and a fake cluster for offline tests |
| Kind demo target | A two-service app (frontend calls backend), Prometheus and a traffic generator on a Kind cluster ([infra/kind/](../infra/kind/)), with four scenarios that inject real faults: a bad release, a dependency crash, a memory leak, and logs that carry a planted instruction. The dependency crash was tested offline only: it has never run on a cluster |
| SREGym target | The same adapter in external-harness mode ([integrations/sregym/](../integrations/sregym/), [ADR 0031](decisions/0031-an-external-benchmark-harness-release-tags-and-pod-failure-onsets.md)). SREGym 1.1 deploys its hotel-reservation app on its own cluster and injects the problem; release tags are rollback keys; there is no probe or metric |
| Exact-action authorization | On a real cluster a person must type the target service's name, at the console or in the dashboard. A single-use signed capability is bound to that exact action, and a server-side dry run, read-back and audit wrap the write |
| Independent oracle | Ground truth from the cluster through the oracle's own identity, never an MCP tool: rollout complete, readiness, the oracle's own HTTP probe (Kind), or containers that stayed up (SREGym) |
| The owner's VM | The Kind demo ran first on the owner's Mac (W2, W3). In W4 the model beside the cluster exhausted the Mac's memory, so one AWS EC2 VM took over: the Kind cluster from W4's first counted run, the model from W4's second run (the first had it on the Mac), and SREGym's cluster in W5. RECAP stays on the Mac and reaches them through an SSH tunnel ([infra/aws/ec2.sh](../infra/aws/ec2.sh)), and an idle guard on the VM stops it when nothing uses it |
| Records | Published runs in [evals/live/](../evals/live/) and the `real_kubernetes` evaluation mode with two batches (Kind, SREGym), failures included |
| Mission Control | The dashboard shows the active target, checks that the cluster can be reached, asks for the typed service name before a live approval, and tells the two clusters apart in the evaluations |
| Reliability | Tests for faults on Kubernetes and at runtime: a slow rollout, a cancelled run, a tool server that dies, concurrent runs, an interrupted process. They found one real bug: a run whose tool server died went on and asked for an approval it could never use |
| Operational metrics (optional) | `GET /metrics` in the Prometheus text format, off unless `RECAP_METRICS=1`, with bounded labels only. A small in-process API load test runs with the scripted policy |

## 2. Which files changed

From v0.5.0 to this release candidate, outside the published records. The main areas:

| Area | Where |
|---|---|
| Kubernetes adapter and fake cluster | `recap/environments/kubernetes/` (engine, kubectl, translate, rollout, probe, metrics, preflight, scenario, state, fake, options) |
| SREGym adapter and profile | `integrations/sregym/`, `configs/targets/sregym-hotel/`, `fixtures/sregym-scenarios/` |
| Kind demo app, manifests, scripts | `infra/kind/`, `configs/targets/kind-demo/`, `fixtures/kind-scenarios/` |
| VM, SREGym setup, idle guard | `infra/aws/`, `infra/sregym/` |
| Approvals, verifier, controller | `recap/safety/approvals.py`, `recap/agents/verifier.py`, `recap/harness/controller.py`, `recap/tools/registry.py`, `recap/providers/protocol.py` |
| API and dashboard | `apps/api/` (live approvals, environment check, labels, batch targets), `apps/dashboard/src/` |
| Evaluation | `evals/records.py`, `evals/categorize.py`, `scripts/live_eval.py`, `scripts/export_run_trace.py` |
| Tests | `tests/unit/test_kind_*.py`, `tests/unit/test_sregym_adapter.py`, `tests/e2e/test_kind_pipeline.py`, `tests/e2e/test_kind_live.py` (opt-in, real cluster), and more |
| Decisions | [ADR 0032](decisions/0032-a-real-kubernetes-target-behind-the-same-interface.md) (the Kubernetes target: typed `kubectl`, one identity per role, a pinned cluster, the typed-name approval), [ADR 0031](decisions/0031-an-external-benchmark-harness-release-tags-and-pod-failure-onsets.md) (the external harness) and [m5-integration-plan.md](m5-integration-plan.md) (the plan, written before the work) |

## 3. How the Kubernetes integration works

```mermaid
flowchart LR
  subgraph Mac["Owner's Mac"]
    RECAP["RECAP controller<br/>agents, verifier, gateway, oracle"]
    MCP["MCP server<br/>typed tools"]
    API["API + Mission Control"]
  end
  subgraph VM["Owner's AWS VM (m6a.2xlarge)"]
    KIND["Kind demo cluster<br/>frontend, backend, Prometheus"]
    SRE["SREGym's cluster<br/>hotel-reservation, 19 Deployments"]
    OLL["Ollama, qwen3:8b"]
  end
  RECAP -- "SSH tunnel: model requests" --> OLL
  MCP -- "reader identity: reads" --> KIND
  MCP -- "writer identity: one approved write" --> KIND
  RECAP -- "oracle identity; scenario identity at reset" --> KIND
  MCP -- "reader and writer identities" --> SRE
  RECAP -- "oracle identity" --> SRE
```

- **One interface.** The agents never import an environment. They reach the cluster only
  through MCP tools, and the gateway judges every call. The adapter translates real cluster
  data into RECAP's observation models: services, health, logs, metrics, releases, events,
  and the rollback dry run. Nothing outside the adapter is Kubernetes-specific. M5's changes
  there hold on every target: live-target checks, typed-name approvals, pod-event onsets, and
  memory kept per target kind.
- **Identities.** Each role has its own ServiceAccount token, minted on the VM and copied to
  the Mac (`ec2.sh creds`). In the MCP server, `recap-reader` serves the read tools and
  `recap-writer` may only patch the app's Deployments. In the controller, `recap-oracle` reads
  for the oracle and, on the Kind demo, `recap-scenario` injects faults. Each identity runs only the `kubectl` operations its role allows; the
  administrator's credential is refused.
- **Pinning.** Every reset first proves the cluster is the pinned one. Each kubeconfig's API
  server must be on 127.0.0.1 and its CA fingerprint must match the one recorded at setup.
  The versions must be within one minor of each other, and on Kind, Prometheus's clock must
  agree. A namespace lease keeps two runs off one namespace.
- **The write path.** Rollback and restart only. The capability is verified and its nonce
  committed before the cluster is touched, the target is checked again, then a server-side
  dry run, the write, a read-back and an audit row follow, all inside the 10 s tool limit. A
  rollback restores exactly the image the named revision ran; the read-back checks the
  digest too.
- **Reset (Kind).** The baseline images are restored and must be healthy and calm. Then the
  scenario's typed steps inject the fault, and RECAP waits for the alert measured from the
  cluster: the oracle's probe error rate, unready replicas and crash loops.

## 4. How the SREGym integration works

- SREGym 1.1 (commit `a90b43cc`, applications `afd5b947`) runs on the VM in its
  external-harness mode, set up by [infra/sregym/sregym.sh](../infra/sregym/sregym.sh) with
  pinned, checksummed tools. `sregym.sh deploy PROBLEM` lets SREGym deploy its app and
  inject the problem; `sregym.sh grant` creates RECAP's three identities
  ([recap-rbac.yaml](../infra/sregym/recap-rbac.yaml)). RECAP never uses SREGym's administrator
  credential or its agent tools (a kubectl passthrough and a shell, cluster-wide).
- RECAP's reset changes nothing: it takes the app's first revisions as the baseline and
  waits for the alert. A crash loop between two crashes still counts as one, so the alert does
  not depend on the instant it is measured.
- A release is an image tag, used only as a key into the Deployment's history. The proposal
  schema offers only the restore points the evidence names, and VP4 checks the answer.
- SREGym has no probe and no metrics. Health is status and readiness, and what is not
  measured is reported as not measured, never estimated. The verifier dates a bad release's
  onset from its own pods' failure events.
- Two verdicts, never merged. RECAP's oracle decides RECAP's RESOLVED, and SREGym's own
  grader (`sregym.sh grade`, which runs `run-oracle.py`) gives the benchmark's verdict, recorded
  in the batch by hand.

## 5. How approvals are enforced

```mermaid
sequenceDiagram
  participant M as Model (proposal)
  participant C as Controller
  participant V as Verifier (VP1-VP4)
  participant P as Policy (R1-R14)
  participant H as Person (console or dashboard)
  participant G as Gateway
  participant K as Cluster (writer identity)
  M->>C: execute_rollback geo to_version=sha-...
  C->>V: does the evidence support this exact fix?
  C->>P: reversible write: APPROVAL_REQUIRED (R11)
  C->>H: the exact proposal, its digest, the target
  H->>C: types the target service's name
  C->>G: single-use capability, bound to this action (R12)
  G->>K: dry run, write, read-back, audit
```

- **The proposal is checked before anyone is asked.** VP1 requires the cited evidence to
  exist, VP2 the target to match the verified diagnosis, VP3 the action to be named, and VP4
  a named restore point that is not the release the diagnosis blames.
- **A person decides on a live target, and must type the target service's name.** At the
  console ([recap/safety/approvals.py](../recap/safety/approvals.py)), an Enter alone asks
  again, text typed early is discarded, a bell rings, and the prompt gives up at the
  request's expiry. In the dashboard (`POST /approvals`), an approval without the exact name
  decides nothing. The decision log records how and when the answer came. It proves a
  terminal or a token holder and a timing, not a person.
- **The approval is bound and used once.** It names the proposal's digest. A capability
  signed for that one action is used once (its nonce is burnt even if the write then fails),
  and a "no" wins until the gateway uses it. Destructive actions such as deleting a namespace
  are always refused, even with a valid capability.

## 6. How the oracle verifies recovery

The controller probes the oracle after the write until the target is healthy or its
stabilization window ends (180 s on Kind, 240 s on SREGym). The oracle reads the cluster with
its own identity and is never an MCP tool:

- every allowlisted service's rollout is complete, with no surge pods and no old pods still
  serving;
- its replicas are ready;
- on Kind, the oracle's own HTTP probe, two rounds of requests a few seconds apart, finds the
  error rate below the threshold;
- where there is no probe (SREGym), every container has run at least a minute with no exit in
  the last five. Without a readiness probe, a crash-looping container is Running and Ready for
  the seconds each start lasts.

A successful write is never counted as a recovery by itself; only the oracle's verdict is.

## 7. Real-model runs and outcomes

All runs used `qwen3:8b` (Q4_K_M, the same weights digest throughout) and prompts-v4.7. The
model ran on the owner's VM (one run, on the Mac), at $0 per request. Each record says who
approved what.

| Target | Runs | Outcomes | Published |
|---|---|---|---|
| Kind demo (W4, W6) | 7 | 3 RESOLVED, oracle verified (a bad release; the red-team run with a planted instruction in the logs; the acceptance run, started and approved in Mission Control); 2 rejected (an Enter alone, before the prompt named the service); 1 failed safely (a model request outlasted its limit); 1 cancelled (memory watchdog on the Mac) | [batch](../evals/reports/batches/2026-11-03-real-kubernetes-qwen3-8b-prompts-v4.7/README.md), [three exports](../evals/live/README.md) |
| SREGym `misconfig_app_hotel_res` (W5) | 5 | 1 RESOLVED, and SREGym's grader passed it; 1 blocked by VP4 (the model named the bad release itself; nothing changed, so SREGym's grader: fail); 1 approval expired; 2 failed safely (the wrong mechanism, refused by the verifier) | [batch](../evals/reports/batches/2026-11-03-real-kubernetes-sregym-qwen3-8b-prompts-v4.7/README.md), [export](../evals/live/2026-11-07-qwen3-8b-sregym-human-approved-prompts-v4.7/README.md) |

These are counts, not rates. The SREGym problem is not held out: RECAP was fixed on it
between runs. Each fix was approved by a person, so nothing here is comparable with SREGym's
leaderboard. One earlier attempt is in neither batch: `run-93b12c804e` (2026-11-02, the model
and the Kind cluster both on the Mac) never finished, because the Mac froze during its
investigation; its record still reads INVESTIGATING, and the move to the VM followed.

## 8. Runtime resources and spending

- **VM:** AWS EC2 m6a.2xlarge (8 vCPU, 32 GiB, no GPU), ap-south-1, Ubuntu 24.04, 150 GB gp3.
  It ran only while used; the owner's rule was to stop it whenever idle.
- **Model:** Ollama 0.34.4 on the VM's CPU, set up on the VM by hand (no script installs
  it): it listens on the VM's 127.0.0.1:11434 only, with cloud models off, and serves
  `qwen3:8b` with weights `sha256:a3de86cd1c13…`, the same weights as on the Mac. One model
  request took 75 to 300 s, and up to about 13 minutes when two specialists shared the server.
  A run took 15 to 30 minutes, most of it model time.
- **Clusters:** the Kind demo's node container used about 0.75 GiB. SREGym's four-node
  cluster and its 19 services fit beside the model in 32 GiB.
- **Spending:** AWS Cost Explorer showed about $5.40 of usage before credits from 2026-11-02
  to 2026-11-07: compute $3.55, disk $1.63, the rest under $0.20. It was read on 2026-11-07,
  before the VM session of the W6 acceptance run, so that session is not in it, and its data
  can lag by up to a day. The owner's $100 of AWS credits covered all of it, so the
  out-of-pocket cost was $0. No model request was paid for.

## 9. Tests and CI

Run locally for the 0.6.0 release candidate (2026-11-07), on the owner's Mac. CI has not run
on M5 yet: nothing is pushed, so the first CI run comes with the push.

| Check | Result |
|---|---|
| `python -m pytest` | **2264 passed, 7 skipped** (the six live-cluster acceptance tests and the optional OpenTelemetry test) in about 4 minutes; `-m e2e` selects 185 end-to-end cases |
| Live acceptance tests on the real Kind cluster (`RECAP_LIVE_KIND=1`) | all six passed in W3 (2026-11-02, scripted policy); not run since, and not part of the default run. Nine commits since W3 changed the Kubernetes adapter: the real-model runs exercised its reads and its rollback again (the W6 acceptance run on the Kind demo, the last SREGym run in its external-harness mode), but its restart has not run on a real cluster since W3 |
| Browser smoke test (`apps/dashboard/e2e/smoke.mjs`, by hand) | no result recorded for the release candidate; its last recorded result is 72 of 72 checks, for 0.5.0, before W6 changed most pages |
| `ruff check .`, `ruff format --check .`, `mypy` (its configured scope) | clean |
| Dashboard: `tsc --noEmit`, `npm test`, `npm run build` | clean; **627 tests passed**; the production build succeeded |
| Secret scan (tracked files and every blob in the history) | no secrets found |
| Recordings manifest | both committed recordings match their pins; the golden two-agent replay passes |

The tests M5 added cover: the Kubernetes adapter over a fake cluster (reads, the write path,
pinning, identities, refused writes, the lease, the oracle's stability rule), the SREGym
external-harness mode, the controller's live-target rules, typed-name approvals at the
console and through the API, the readiness check, the evaluation records' targets and
benchmark verdicts, and the W6 fault tests (a slow rollout, a cancelled run, a tool server that
cannot start or dies mid-run, concurrent runs and the namespace lease, injection text in
events, a run killed while waiting for approval), plus the optional metrics and the load test.

## 10. Known failures and limitations

- **Small samples.** 12 real-cluster runs, 4 of them verified recoveries: they show that the
  path works end to end, not how often a model gets it right.
- **Not a benchmark result.** One SREGym problem, not held out, run five times with RECAP fixed
  in between, and every fix approved by a person.
- **A small model on a CPU.** `qwen3:8b` sometimes picks the wrong mechanism (two SREGym
  runs), and a request can take minutes. The verifier refused every wrong claim, so those
  runs failed safely.
- **Kind, not production.** One small cluster per target, no production traffic.
- **Paths not run on a cluster since W3, or never.** The six live acceptance tests last ran in
  W3, before nine adapter commits; the real-model runs since used the reads and the rollback,
  but no restart has run on a real cluster since W3. The Kind demo's `kind-dependency-crash`
  scenario was tested offline only and has never run on a cluster, so how long its crash loop
  takes to show there (the kubelet's back-off against the 60 s progress deadline) is unknown.
- **SREGym's writer is limited by RBAC only.** Unlike the Kind demo, no admission policy
  checks the shape of its patches.
- **Clocks on SREGym go unchecked.** Without Prometheus there is no clock to compare with, so
  a skew between the Mac and the VM would go unseen.
- **Published runs cannot be opened in Trace Explorer from a fresh checkout.** The API reads
  runs from its data folder; the exports in `evals/live/` are files to read, not a run store.
- **Identity.** The console proves a terminal and a timing, the dashboard a token holder and
  a timing; neither proves which person decided.
- **The API's reads are open on loopback.** Live runs' data is served without a token on the
  owner's machine; token-protected reads are future hardening ([threat model](threat-model.md)).
- **The writer's token.** It is mode 600, but while it is valid any process of the owner's
  could patch the allowlisted Deployments without an approval: the capability is checked
  inside RECAP, and the cluster enforces only RBAC (on Kind, also the admission policy).
- **A killed run holds its namespace.** A run whose process was killed while it waited for
  approval leaves its approval request listed until it expires, although nothing can act on
  it, and its namespace lease: new runs on that target are refused until the lease expires (an
  hour after the run's reset, by default) or is released.
- **Smaller ones.** `POST /incidents/run` re-reads the scenario files on every request (about
  40 ms that block the server's event loop).

## 11. Reproduction

| What | Commands |
|---|---|
| The VM | `bash infra/aws/ec2.sh up`, then `sync`, `creds kind`, `tunnel` (see the script's header) |
| The VM's model server | Set up on the VM by hand; no script here installs it. Ollama 0.34.4 listens on the VM's 127.0.0.1:11434 only, with cloud models off, and serves `qwen3:8b` (weights `sha256:a3de86cd1c13…`, the same as on the Mac). `ec2.sh tunnel` carries it to the Mac's 127.0.0.1:11435, where the provider `ollama-vm` reaches it |
| A Kind run | `infra/kind/README.md`; then `bash infra/aws/ec2.sh hold env RECAP_TARGET=kind-demo RECAP_DATA_DIR=$HOME/recap-kind-runs .venv/bin/python scripts/demo.py --scenario kind-bad-release --provider ollama-vm --approve ask` |
| A Kind run from Mission Control (as the W6 acceptance run) | The VM, the cluster and the tunnel as above, and the dashboard built once (`cd apps/dashboard && npm install && npm run build`). Then `bash infra/aws/ec2.sh hold env RECAP_TARGET=kind-demo RECAP_DATA_DIR=$HOME/recap-kind-runs .venv/bin/uvicorn apps.api.main:app` (the VM is kept in use for 2 hours at most) and open http://127.0.0.1:8000/. Sign in with the token in `$HOME/recap-kind-runs/operator-token`; on Overview, **Check now** runs the cluster's preflight (nothing changes); in the Incident Lab choose `kind-bad-release` and the backend `ollama-vm` (approval is manual on a live target; the acceptance run switched **Experience memory** off, so it read no episodes) and **Start run**; when the approval dialog asks, type `backend` and approve |
| A SREGym run | [infra/sregym/README.md](../infra/sregym/README.md): `sregym.sh setup`, `deploy misconfig_app_hotel_res`, `ec2.sh creds sregym`, the run with `RECAP_TARGET=sregym-hotel`, then `sregym.sh grade` |
| The batches | `RECAP_TARGET=kind-demo .venv/bin/python scripts/live_eval.py --from-data --data-dir ~/recap-kind-runs --out OUT` (SREGym: `RECAP_TARGET=sregym-hotel`, plus `--benchmark` and `--benchmark-verdict`) |
| Offline tests | `.venv/bin/python -m pytest` (the real-cluster tests need `RECAP_LIVE_KIND=1` and a running cluster) |
| When done | `sregym.sh teardown`, `ec2.sh stop`; `ec2.sh down` deletes the VM and everything on it |

## 12. Next steps toward a final release

- A held-out set of SREGym problems, split by failure mechanism and committed before the first
  scored run ([benchmark-plan.md](benchmark-plan.md)), with RECAP frozen while they run.
- An admission policy on SREGym's cluster, as on the Kind demo, and a clock check without
  Prometheus.
- Opening published runs in Mission Control (an export loader), so a fresh checkout can show
  them.
- Backend work tracked for after M5: Redis-backed incident workers, Docker Compose
  packaging and larger load tests.
- A GPU, or a larger hosted open-weight model on a free plan, to measure how much the model
  itself limits the results.
