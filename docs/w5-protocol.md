# W5: the held-out SREGym evaluation protocol

> **Run on 2026-11-09 and stopped after 3 of the 5 problems** (a stop condition, then the
> owner's decision): the [results](#results) are at the end. The machine-readable manifest, now
> with its results, is [evals/frozen/w5-heldout-sregym.yaml](../evals/frozen/w5-heldout-sregym.yaml),
> and the session's record is in [evals/frozen/w5-evidence/](../evals/frozen/w5-evidence/README.md).

## What this evaluation is, and is not

RECAP's own agents, with a **real model** (`qwen3:8b`, self-hosted on the owner's VM), investigate
**five SREGym problems RECAP was never run on**, on SREGym's own Kubernetes cluster, and propose
fixes a person approves or rejects. RECAP's oracle and SREGym's grader each give a verdict, never
merged. It is a human-in-the-loop evaluation of generalization to unseen faults: not an
unassisted benchmark score, not comparable with a leaderboard that uses other intervention rules,
and five runs support counts, not rates. Scripted, replayed and real-model results stay apart.

## The held-out set

SREGym 1.1 (commit `a90b43cc…`) registers 101 problems; 30 name the hotel-reservation app, the
only app RECAP's `sregym-hotel` profile allows. Only the registry's **problem names** were read:
no problem definition, injected change, mitigation oracle or solution was opened. A problem is
held out when it:

1. is in the pinned registry and named for the hotel-reservation app;
2. was never used by RECAP (M5 used `misconfig_app_hotel_res` only: five runs and the adapter's
   fixes, so it is excluded and serves as the setup pilot);
3. is **writable**: its name points at a field of a workload's Deployment or pod template, the one
   object kind RECAP may change (a rollback to an earlier revision, a restart);
4. is **observable**: by Kubernetes semantics alone, the named fault keeps pods from starting,
   restarts them or keeps them unready, which is all this target measures (no metrics, no probes).

Exactly five meet all four, run in the registry's order:

| scenario | SREGym problem |
|---|---|
| `sregym-w5-1` | `duplicate_pvc_mounts_hotel_reservation` |
| `sregym-w5-2` | `liveness_probe_too_aggressive_hotel_reservation` |
| `sregym-w5-3` | `init_container_dependency_hang_hotel_reservation` |
| `sregym-w5-4` | `readiness_probe_misconfiguration_hotel_reservation` |
| `sregym-w5-5` | `sidecar_port_conflict_hotel_reservation` |

The other 25 names and why each was excluded are in the manifest. The scenario files are
**blind**: `family: unlabelled`, `expected: {}`, neutral ids and titles. Their answers are written
in only after the frozen evaluation, from SREGym's own definitions, and the runs are judged again
with them. None of these files' fields reaches an agent: agents see the measured alert, the
environment's self-description and their tools' results.

## What is frozen, and how it is enforced

| frozen | value | enforced by |
|---|---|---|
| RECAP's code, configs, prompts, runbooks, scenarios, SREGym integration, approval path, run and batch scripts | one SHA-256 over every tracked file under the frozen roots | `scripts/w5_freeze.py check` before every scored run; `tests/unit/test_w5_manifest.py` |
| installed Python packages | one SHA-256 over names and versions | `scripts/w5_freeze.py check` |
| code revision | the commit that adds the manifest | each record's code version (never `+dirty`) |
| model | `ollama-vm`, `qwen3:8b`, weights `sha256:a3de86cd…` (pinned), temperature 0, seed 7, 16k context, thinking off | the provider refuses other weights |
| prompts | `prompts-v4.7` | the code (frozen) |
| agents and routing | adaptive (rules RT0-RT7), up to 2 parallel specialists, 2 rounds, 2 mitigation attempts | configs (frozen) |
| verifier and safety | `evidence-rule-verifier-v1`; `policy-sregym-hotel-v2`; manual approval, 900 s expiry; rollback and restart only | code and configs (frozen); RBAC on the cluster |
| retrieval | BM25, k 4, the shipped runbooks plus the Kubernetes overlay; no embeddings | configs (frozen); the shell below |
| memory | a new, empty data folder (`~/recap-w5-heldout`); reads TRAIN and LIVE (both empty); held-out runs are never written to memory | consolidation refuses test-partition scenarios |
| budgets | 30 decisions, 30 tool calls, 3600 s, 120k estimated and 200k model tokens; context pack 3000, item 400 | configs (frozen) |
| SREGym | release 1.1, commit `a90b43cc…`, applications `afd5b947…`, registry SHA-256 `e7a7f518…`, `kind-kind` (4 nodes, Kubernetes 1.32.1), namespace `hotel-reservation` | `infra/sregym/sregym.sh` checks the commits and the cluster's CA |

A change under the frozen roots before the first scored run (for example after the pilot) is a
new freeze, with new hashes in a new commit, and the report says so. From the first scored run
until the last grade, nothing changes.

## Leakage protections

- **No answer reaches an agent.** Agents get `Incident.agent_view()` (no scenario id, no labels),
  the environment's description (tested free of problem names) and tool results. The scenario
  files hold no answer at all until after the evaluation.
- **No grader output reaches a run.** SREGym's grader runs only after RECAP's run has ended, and
  its output goes to a file, never to RECAP.
- **No task-specific hints.** The runbooks and the Kubernetes overlay are frozen and name no
  held-out problem (tested); no rule names a scenario (W4's leakage tests).
- **No memory between problems.** Held-out runs are never consolidated; the pilot has its own data
  folder.
- **No adaptation.** The freeze check runs before every scored run; nothing is tuned between them.
- **No hidden-answer inspection while troubleshooting.** If a problem misbehaves, the report says
  what was observed in the cluster, never what SREGym's definition says, until all five are done.
- **No selective exclusion.** Every attempted problem is a row: `not_deployed`, `no_alert`, or a
  run with its category and grade. None is replaced.
- **Human role.** The owner only approves or rejects the exact proposed action. Any hint beyond
  that marks the run `assisted`, reported apart from the frozen evaluation.

## The session, step by step

Only after the owner approves the cost request, with the owner present. `$` lines run on the Mac
from the repository; the owner runs the RECAP command (it asks for approval at the terminal).

```bash
# 0. Gate (every step below: stop on a failure)
$ .venv/bin/python scripts/aws_cost_meter.py open --cap 5
$ .venv/bin/python scripts/w5_freeze.py check
# 1. Setup (SREGym's cluster was deleted after M5: setup recreates it; the app's images may be pulled again)
$ bash infra/aws/ec2.sh up                      # starts the VM and its idle guard
$ bash infra/aws/ec2.sh sync
$ bash infra/aws/ec2.sh ssh 'cd ~/recap && bash infra/sregym/sregym.sh setup'
$ bash infra/aws/ec2.sh tunnel                  # in the background, for the whole session
# 2. Pilot (not counted): the M5 development problem, its own data folder
$ bash infra/aws/ec2.sh ssh 'cd ~/recap && bash infra/sregym/sregym.sh deploy misconfig_app_hotel_res'
$ bash infra/aws/ec2.sh creds sregym
owner$ bash infra/aws/ec2.sh hold env RECAP_TARGET=sregym-hotel RECAP_DATA_DIR=$HOME/recap-w5-pilot \
         .venv/bin/python scripts/demo.py --scenario sregym-misconfig-app-hotel-res --provider ollama-vm --approve ask
$ bash infra/aws/ec2.sh ssh 'cd ~/recap && bash infra/sregym/sregym.sh grade misconfig_app_hotel_res'
# 3. Each held-out problem n = 1..5, in order
$ .venv/bin/python scripts/w5_freeze.py check && .venv/bin/python scripts/aws_cost_meter.py status
$ bash infra/aws/ec2.sh ssh 'cd ~/recap && bash infra/sregym/sregym.sh deploy <problem n>'
$ bash infra/aws/ec2.sh creds sregym
owner$ bash infra/aws/ec2.sh hold env RECAP_TARGET=sregym-hotel RECAP_DATA_DIR=$HOME/recap-w5-heldout \
         .venv/bin/python scripts/demo.py --scenario sregym-w5-<n> --provider ollama-vm --approve ask
$ bash infra/aws/ec2.sh ssh 'cd ~/recap && bash infra/sregym/sregym.sh grade <problem n>' > grader-w5-<n>.txt
# 4. End
$ bash infra/aws/ec2.sh ssh 'cd ~/recap && bash infra/sregym/sregym.sh teardown'
$ bash infra/aws/ec2.sh stop
$ .venv/bin/python scripts/aws_cost_meter.py close
```

The shell for every RECAP command: `RECAP_TARGET=sregym-hotel`, the data folder above, and
`RECAP_PROVIDER`, `RECAP_ENABLE_EMBEDDINGS`, `RECAP_KNOWLEDGE_OVERLAYS` and `RECAP_REPLAY` unset.
Approving in Mission Control (a local API on the same data folder, the operator token) is the
same human approval and may be used instead of the terminal.

## What each run records

From the run's own record and trace (`evals/categorize.py`): scenario, code version, provider,
model and weights, prompt version, configuration hash; agents and router rules per round; model
requests and the tokens the server reported; model time, server time and time waited inside the
server; tool calls; rounds; claims supported, contradicted and insufficient; safety blocks and
unsafe actions; proposed and executed actions; the approval decision; wall-clock time and stage
times; RECAP's oracle verdict; the failure category. SREGym's grader verdict is added by hand
from its output (`--benchmark-verdict`), kept apart. The failure mechanism is known only after
the evaluation, from SREGym's definitions. AWS time and cost come from the cost meter and each
run's wall-clock time.

## The cost gate

Prices from the AWS Price List API (2026-11-09, ap-south-1): the VM (m6a.2xlarge) $0.2222 an hour
plus $0.005 for its public IPv4 address while it runs; its 150 GB gp3 disk $0.0912 per GB-month
(about $0.46 a day, running or stopped). `scripts/aws_cost_meter.py` estimates the window's
spend from the VM's own state (free describe calls), counting whatever AWS does not report up to
now. No new step starts past 70% of the approved cap ($3.50 of $5), or when one more problem
would cross it. AWS's billing data lags by hours and is never used as the limit.

## Stop conditions

Stop at once, report, and do not retry an expensive step when: the cost meter says stop; the
owner is away or an approval expires; the freeze check fails; credentials or secret values show
up somewhere unexpected; the environment cannot be isolated; the model fails twice for the same
infrastructure reason; benchmark integrity is in doubt; or continuing would mean changing frozen
behaviour.

## After the evaluation

1. Build the batch from the data folder, with SREGym's verdicts recorded apart:
   `RECAP_TARGET=sregym-hotel .venv/bin/python scripts/live_eval.py --from-data --data-dir ~/recap-w5-heldout --out <folder> --benchmark "SREGym 1.1 mitigation oracle (run-oracle.py), recorded by hand" --benchmark-verdict <run>=pass|fail ...`
2. Only then read SREGym's definitions of the five problems, write each scenario's answer
   (`family`, `expected`), set the manifest's status to `completed`, and rebuild the batch so its
   categories use the answers.
3. Publish the sanitized batch, the grader outputs and the run exports (traces, recordings), and
   list the runs in the published-runs manifest so Mission Control opens them offline.
4. Report every attempted problem, the resource use and cost, and the limits of five runs.

## Results

Run on 2026-11-09 with the owner present: a development pilot first (`misconfig_app_hotel_res`,
not counted: RESOLVED by an approved rollback, verified by RECAP's oracle, passed by SREGym's
grader), then the held-out problems in order.

| scenario | SREGym problem | RECAP's run | category (with the answers) | SREGym's grader | wall clock |
|---|---|---|---|---|---|
| `sregym-w5-1` | `duplicate_pvc_mounts_hotel_reservation` | `run-7bc5159b6d` FAILED: no supported root cause after two rounds | `wrong_mechanism`: memory pressure claimed on frontend, the right service | fail | 52 min |
| `sregym-w5-2` | `liveness_probe_too_aggressive_hotel_reservation` | `run-db560f6568` FAILED: every hypothesis contradicted | `missed_crucial_tool`: the fault is on `aux-service`, outside the allowlist | fail | 35 min |
| `sregym-w5-3` | `init_container_dependency_hang_hotel_reservation` | `run-62f9029f00` cancelled by the owner (Ctrl-C) in its second round | `cancelled` | fail | 28 min |
| `sregym-w5-4` | `readiness_probe_misconfiguration_hotel_reservation` | not attempted | | | |
| `sregym-w5-5` | `sidecar_port_conflict_hotel_reservation` | not attempted | | | |

- **Why it stopped after three.** The stop condition "the model fails for the same
  infrastructure reason twice" was met. CPU-only inference on the VM took 103 to 616 s per
  request (median 259 s over 32 requests), more when two specialists shared the one server; a
  specialist ran out of time in problem 1, and a request did not answer within 900 s in
  problem 2. The owner ended the evaluation after problem 3. Problems 4 and 5 were never
  deployed: their files stay blind and their definitions unread, so they remain held out.
- **What the answers showed.** None of the three faults could have been fixed by RECAP. Each
  changes a Deployment's spec but not its image, so the rollback (to an earlier revision with
  another image) has nothing to restore and a restart re-creates the same pods; problem 2's
  fault is on a workload SREGym adds outside the profile's allowlist, which RECAP cannot
  observe. The rule's "writable" test, applied to names alone, could not see this, a risk
  [ADR 0036](decisions/0036-a-blind-frozen-held-out-evaluation.md) named. These runs measure
  diagnosis and safety, not repair.
- **Safety held.** No run proposed a fix, so no approval was requested and nothing was written
  to the cluster. The verifier refused every unsupported claim (memory pressure without a memory
  metric, dependency failures the logs contradicted), and every run handed the incident to a
  person.
- **The freeze held.** Every record carries code `042e0db212d1`, configuration
  `34fab689410b24cf`, `prompts-v4.7` and the pinned weights; the freeze check passed before each
  problem; no memory episode was written.
- **How the session differed from the steps above.** The `$` steps ran from one script the owner
  started ([session.sh](../evals/frozen/w5-evidence/session.sh)), in the same order, plus the
  owner's Enter before each problem, a check of the pinned weights through the tunnel, a rolling
  shutdown backstop on the VM, and the deploy's and the grader's output to files only. The idle
  guard stopped the VM before problems 1 and 2 while it waited; the script started it again and
  rebuilt SREGym's cluster each time (teardown, setup). SREGym's harness port-forwards its own
  MCP server on every interface (port 9954; the security group admits SSH only): the script moved
  it to the VM's loopback after each deploy. Problem 3's first grade was interrupted with its
  run and was taken again 2.5 minutes later.
- **Before and after the answers.** A batch built before any answer was written put the runs in
  `insufficient_evidence` (2) and `cancelled` (1); the published batch, built after, uses the
  answers: [2026-11-09-w5-heldout-sregym-qwen3-8b](../evals/reports/batches/2026-11-09-w5-heldout-sregym-qwen3-8b/README.md).
  The runs are in [evals/live/](../evals/live/README.md).
- **Cost.** About $1.40 of VM time by AWS's own billing (compute $1.34 and the public address
  $0.03 on 2026-11-09), the setup and the pilot included, against the $5 cap. The cost meter had
  estimated $1.10 from 4.24 running hours: it missed a 1.8-hour session (the VM started for
  problem 1 and stopped by its idle guard during the break) that began and ended between two of
  its readings; the VM is stopped and SREGym's cluster deleted,
  the disk kept.
