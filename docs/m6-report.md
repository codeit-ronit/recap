# M6 report: RECAP's final milestone (version 0.7.0)

> 2026-11-12. M6 turned RECAP from a working prototype into a finished portfolio product. This is
> the summary: what was delivered, how it was verified, what it cost and what is left. The log,
> workstream by workstream, is [m6-progress.md](m6-progress.md). Nothing ran in production; every
> number below is a count from a command that was run, not a rate.

## What M6 delivered

| Workstream | What it delivered | Commits |
|---|---|---|
| W0 baseline | The 0.6.0 release verified, M5's run data preserved, the AWS resources audited, a CI wait made diagnosable | `7e6917e` |
| W1 Mission Control | A simpler Overview, the **Guided Demo** (a recorded real-model run, replayed locally and labelled so, stopping for your approval), grouped navigation, plain-language rule explanations | `1395f50`, `99e002e` |
| W2 published runs | The real Kind and SREGym runs open offline and read-only, each checked against pinned SHA-256s ([ADR 0033](decisions/0033-published-real-cluster-runs-read-only-behind-the-same-views.md)) | `2589651`, `ff4d678` |
| W3 workers and Docker | Incidents queued in Redis and run by crash-safe worker processes that never re-send an uncertain write ([ADR 0034](decisions/0034-durable-workers-redis-coordinates-the-run-records-decide.md)); Docker Compose, verified with real containers | `b332a95`, `1a9d1fd`, `f0794f9`, `e46e06f` |
| W4 AI evaluation | One-switch comparisons with provenance, and retrieval, memory, routing, budgets, the verifier and safety each measured on its own ([ADR 0035](decisions/0035-one-switch-comparisons-separate-component-datasets-nothing-pooled.md)) | `8e26832`, `46ea1d5`, `9acf7ea` |
| W5 held-out SREGym | A frozen, blind evaluation on problems RECAP had never seen, reported in full ([ADR 0036](decisions/0036-a-blind-frozen-held-out-evaluation.md), [w5-protocol.md](w5-protocol.md#results)) | `042e0db`, `0395662`, `b03bf33` |
| W6 hardening | Metrics from the API and every worker, a load test of the Compose stack, fault and attack coverage mapped, two defects fixed ([performance-and-reliability.md](performance-and-reliability.md)) | `144eecf` |
| W7 productization | The README rewritten around what has been demonstrated, an interview kit, refreshed docs and screenshots, two dashboard copy fixes, version 0.7.0 | this commit |

## The definition of done, checked

- **AI systems engineering.** A multi-agent, model-agnostic agent platform whose parts were
  measured one switch at a time (W4, the scripted policy on the simulator, 8 paired runs per
  comparison): runbook retrieval is what turns a diagnosis into a recovery (without it 4 of 8,
  with it 8 of 8); adaptive routing kept every recovery with 2.9 fewer tool calls per run than
  always-single; memory judged all 79 remembered episodes correctly and changed no outcome;
  halving the tool budget cost one hard incident. The real-model pilot of W4 was planned, not run.
- **Genuine reliability automation.** Real Kubernetes remediation with a person's approval and an
  independent oracle (M5: 12 runs, 4 verified recoveries, one passed by SREGym's grader); a
  documented held-out evaluation (W5: 3 of 5 frozen problems run, none solved, none fixable with
  RECAP's two actions; no unsafe action, every unsupported claim refused).
- **Backend engineering maturity.** Durable processing (W3: a real worker killed at seven points,
  every job settled correctly), Docker packaging, Prometheus metrics from the API and each worker,
  fault and attack coverage (W6: 12 failure cases and 16 attacks mapped to the tests that inject
  them), load measurements (W6: about 1.3 incidents a second with four worker slots, exact
  backpressure, no server error), and the regression suite below.
- **A finished product experience.** Mission Control's Guided Demo and published real-cluster runs
  work offline from a fresh checkout; the README answers what RECAP does, why, how to try it and
  what has been shown; [interview.md](interview.md) holds a tested demonstration script.

## Final verification

The spec's twenty checks, run on 2026-11-12 on the release commit.

| # | Check | Result |
|---|---|---|
| 1 | M0 to M5 still work, golden recordings included | The full suite (below) passes, including the offline replays of the committed real runs; the scripted V1 evaluation reproduced the committed report exactly (32 of 32 cases, same states and outcomes); both recordings match their pins |
| 2 | Lint, types, tests | `ruff check` and `ruff format --check` clean (437 files); `mypy` on its configured scope clean (59 files); `pytest` **2485 passed, 15 skipped, 0 failed** (the skips need a live Kind cluster, a real Redis or the OpenTelemetry SDK). CI itself has not run on M6's commits: nothing is pushed yet |
| 3 | Dashboard build and browser checks | `npm test` **710 of 710**; the strict TypeScript build; the browser smoke test **133 of 133** (every page at 1440 and 390 px, the published runs, the Guided Demo's approve, restart and reject, the replay-and-approve flow), run twice on 0.7.0 |
| 4 | The guided demo from a fresh checkout | Passed: the release's files exported from git's index into an empty folder (`git checkout-index`), a new virtual environment from `constraints.txt`, `scripts/seed_demo.py` (11 runs), `npm ci` and the build, the server on 0.7.0 serving all 7 published runs (none rejected), and the browser smoke test **133 of 133**, the Guided Demo and the published runs included; about 3.5 minutes with this machine's package caches |
| 5 | Published runs without AWS | `scripts/published_runs.py --check`: all 7 load (3 Kind, 4 SREGym, 3 of them W5's held-out runs); the smoke test opened each |
| 6 | Worker restart and recovery | `tests/integration/test_worker_crashes.py` (a real worker SIGKILLed at seven points) and `test_workers.py`, in the suite; a real Redis and real containers in W3 |
| 7 | No duplicate unsafe write | Single-use approvals and capabilities, and a write that may have landed never sent again (`test_window_6_...`), in the suite; W6 fixed the one gap it found (a second cancellation during a write) |
| 8 | Compose start and stop | W3 (34 of 34 checks) and three W6 sessions with real containers on 2026-11-12, each brought up and removed |
| 9 | Prometheus with real measurements | W6: the API and both workers scraped, the counts matching the load test's ([performance-and-reliability.md](performance-and-reliability.md#what-prometheus-scraped-during-run-1)) |
| 10 | Reproducible load data | Three runs of the same levels, within a few percent where nothing changed ([the reports](../evals/reports/load/2026-11-12-compose-two-workers/README.md)) |
| 11 | Security regressions | The adversarial and API security tests, in the suite; every unsafe action they attempt is refused ([the matrix](performance-and-reliability.md#security-regressions)) |
| 12 | Honest AI ablations | W4's comparisons each change one switch, checked before the runs and from their records; scripted, replayed and real-model results never pooled |
| 13 | The held-out evaluation | Frozen code, a pinned model, blind scenario files, every attempted problem reported ([w5-protocol.md](w5-protocol.md#results)) |
| 14 | Real clusters told apart from replay and simulation | Batches of one mode; published runs marked historical and read-only; the Guided Demo labelled as a replay |
| 15 | Docs match the code | Every claim in the W7 docs checked against code, tests or evidence; every relative link in the repository resolves |
| 16 | No secret or credential | `scripts/secret_scan.py` over the files and the whole git history: none |
| 17 | No paid inference | None: every model request was local, self-hosted or on a free plan; the paid adapter is disabled |
| 18 | Cloud costs | Below |
| 19 | Clean-up documented | Below |
| 20 | A reproducible release candidate | Version 0.7.0 in the package, the API and Mission Control; check 4; the tag waits for the owner |

## Costs and resources

- **Model requests:** $0. No paid API was called in any milestone.
- **AWS** (the owner's credits, $0 out of pocket): $5.48 of usage recorded by AWS through
  2026-11-07 plus about $0.85 estimated for that day's last sessions (M5); about $1.40 for W5's
  VM time by AWS's own billing (RECAP's cost meter had estimated $1.10: it missed one session);
  and the stopped VM's 150 GB disk, about $0.46 a day. In all, AWS recorded $10.54 of usage up
  to 2026-11-13 (its figures lag about a day), every cent covered by the credits.
- **The VM** (`i-09a55033889ca1466`, ap-south-1) is **stopped**; SREGym's cluster on it was deleted
  after W5. Keeping it costs the disk only; `bash infra/aws/ec2.sh down` deletes the VM, its disk
  and its security group (the owner's decision).
- **This laptop:** colima's `recap` VM is stopped; W6's measurement stack and its volumes were
  removed; no RECAP server is left running.

## Known limitations

- Small samples everywhere: the real-model results show the path works and how it fails, not how
  often a model is right; there is no benchmark score.
- RECAP fixes a Deployment only by rolling it back to an earlier image or restarting it; the
  held-out SREGym problems needed other edits.
- A CPU-only 8B model is slow (one to ten minutes a request on the owner's VM).
- One shared operator token; container images verified on linux/arm64 only.

## What is left for the owner

1. Push M6's commits: `git push origin main` (as codeit-ronit).
2. Tag and release: `git tag -a v0.7.0 -m "RECAP 0.7.0: M6"`, `git push origin v0.7.0`, then the
   GitHub release from the 0.7.0 section of [CHANGELOG.md](../CHANGELOG.md) (the owner runs
   `gh release create`; it publishes).
3. Decide whether to keep the stopped VM (disk only) or delete it.
4. Optionally, the two held-out problems never attempted (`sregym-w5-4`, `-5`) on faster
   hardware, under a new freeze.
