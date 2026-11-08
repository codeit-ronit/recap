# M6 progress: final productization, evaluation and backend reliability

One short checkpoint per workstream. The M5 handoff is [m5-report.md](m5-report.md); its log is
[m5-progress.md](m5-progress.md). Nothing below claims a result that was not measured.

## W0: baseline, evidence and cost (2026-11-08)

**Baseline:** `v0.6.0` = `7500f2a` (HEAD, origin/main, working tree clean).
- The push of `main` ran CI green: python, plus the dashboard job.
- The run that the tag's push triggered on the same commit **failed one test**:
  `tests/unit/test_api_metrics.py::test_a_replayed_model_run_counts_its_recorded_requests_and_tokens_as_replay`
  waited 90 s for the replayed golden run to finish.
- Locally it takes about 1 s and passed in 15 repeated runs of the module, so it was not
  reproduced. The helper waited only for the status "finished", so a run that ended in error
  looked like a timeout and said nothing.
- Changed:
  - the helper stops at "error" and shows the run's error;
  - a timeout now reports the run's status, state and error;
  - the wait is 180 s on a shared CI runner.
- No product code changed. The next CI run will show the cause if it fails again.
- **Pending remote CI verification:** nothing has been pushed since, so no CI run has exercised the
  change. It is a diagnostic improvement, not a proven fix.

**Test commands:**
- `python -m pytest`: 2264 passed, 7 skipped at v0.6.0.
- `ruff check .` and `ruff format --check .`; `mypy` (its configured scope).
- `cd apps/dashboard && npm test && npm run build`: 627 tests.
- `node apps/dashboard/e2e/smoke.mjs`: the browser smoke test.
- `python scripts/secret_scan.py`; `python scripts/recordings_manifest.py --check`.

**M5 evidence, all outside the VM:**
- In git:
  - four real-cluster exports in `evals/live/`: three Kind runs and the SREGym run, each with trace, run record, model recording and README;
  - the two `real_kubernetes` batches in `evals/reports/batches/`: every finished run, failures included, with SREGym's grader verdicts;
  - SREGym's grader output, quoted in `evals/live/README.md`;
  - the golden recordings, pinned in `fixtures/recordings/MANIFEST.json`.
- On the owner's Mac only, outside git: the full run stores `~/recap-kind-runs` and `~/recap-sregym-runs` (records, traces, recordings, the adapter's audit databases).
  - Backed up on 2026-11-08 to `~/.cache/recap-m3-work/m6/m5-run-data-2026-11-08.tar.gz`: 579 KB, 13 run traces, sha256 `7495a4bd5a193d61…`.
- On the VM only:
  - the clusters (the Kind demo; SREGym's was deleted after M5);
  - their images, the Ollama model, the clusters' administrator kubeconfigs and CA, and SREGym's checkout and logs;
  - none of it is run evidence.

**AWS, read-only audit** (ap-south-1, where `ec2.sh` creates everything; free describe calls only; nothing was started or changed):

| Resource | State | Billing |
|---|---|---|
| Instance `i-09a55033889ca1466` (m6a.2xlarge) | **stopped**, no public IPv4 | none while stopped |
| Volume `vol-0883aa4f10ba1529c` (150 GB gp3, encrypted) | attached; `DeleteOnTermination: true` | **passive storage charge, about $0.47 a day** (Cost Explorer showed $0.47 on the stopped days 2026-11-05 and -06) |
| Snapshots, Elastic IPs, AMIs | none | none |
| Security group `recap-m5`, key pair `recap-m5`, the instance's network interface | exist | free |

**Cost:**
- **Recorded, the latest read:** Cost Explorer showed **$5.48** before credits from 2026-11-01 to 2026-11-07 (AWS marks it estimated until the month closes).
- **Estimated, not yet recorded then:** 2026-11-07's VM sessions (about 3.7 hours, roughly $0.85), plus the disk since, about $0.47 a day.
- **Total:** about **$6.75 by 2026-11-07, plus $0.47 a day** after that. All of it is covered by credits, with $0 out of pocket.
- No new Cost Explorer query was made, since each request is a charge.

**What `ec2.sh down` would do:**
1. Terminate the instance, which deletes its volume with it: `DeleteOnTermination` is true.
2. Delete the security group, and the key pair together with the local key file `~/.ssh/recap-m5.pem`.
3. Remove the local state file.

It keeps RECAP's identity kubeconfigs on the Mac, but they would be useless without the clusters.

**Rebuilding later:**
1. `ec2.sh up`: a new VM; cloud-init installs Docker, kind and kubectl.
2. `ec2.sh sync`, then `infra/kind/scripts/setup.sh` on the VM, then `mint-tokens.sh` and `ec2.sh creds kind`.
3. `sregym.sh setup`, then `deploy`, then `ec2.sh creds sregym`.
4. Install Ollama 0.34.4 and pull `qwen3:8b` (about 5 GB).

**Gap:** step 4 is not scripted; it was done by hand in M5.

**Recommendation: keep it** until the held-out SREGym workstream (W5) is done:
- Keeping costs about $0.47 a day: about $7 over two weeks.
- Rebuilding costs about an hour of setup (about $0.30 of VM time and model and image downloads), plus the unscripted Ollama step.
- If W5 slips beyond about three weeks, deleting and rebuilding is cheaper.
- `ec2.sh down` needs the owner's explicit approval in any case.

**External prerequisites still to come:**
- W5 (held-out SREGym) needs the owner's approval to start the VM, a spending cap, and the owner present for each run's approval.
- W3 (Docker Compose) needs a container engine: colima is on the Mac; check before W3.

## W1: Mission Control UX and the guided demo (2026-11-08)

Only the dashboard changed (`apps/dashboard`, commit `1395f50`; this checkpoint and the README
and walkthrough lines follow it). The API, the agents, the safety gateway, the recordings and
every recorded outcome are untouched. Nothing was pushed.

**UX audit (before W1).** What a first-time visitor met, and what each finding led to:

| Finding | Change |
|---|---|
| The Overview opened with eight cards (readiness with 14 components and versions, ten runs, outcomes in four columns, 17 scenarios, access, providers) and no sentence saying what RECAP is. | A one-sentence hero with the three things RECAP does, an at-a-glance box, five recent runs; every other card kept under a closed **Technical details** section. |
| The demo took several screens (Overview, the Incident Lab's replay panel, a pace, Replay) and then showed an engineer's raw event feed with no narration. | A **Guided Demo** page: one Start button, then ten plain-language steps read from the run's own trace. |
| Nine flat navigation entries: no starting point, no sign of which pages show one run. | Three groups: Start here, Investigate, Learn & measure. All route ids are kept, so shared links still work. |
| Rule codes (`R11-reversible-approval`, `VP4-restore-point`) appeared without meaning; policy-rule chips had no explanation on hover (verifier rules did). | `src/view/explain.ts`: what each R, V, VP and RT rule checks and what its refusal means. Shown in the demo; on hover on Evidence & Decisions and in the approval dialog. |
| Each model answer's provenance (replayed, original time) was visible only in Trace Explorer. | The demo labels every model answer "Recorded answer of qwen3:8b (replayed, not called now)", with its original time and tokens. |
| The golden recording's one refused model answer (a repeated tool call) was visible only as `ok: false` in the raw feed. | Shown as a notice in RECAP's own words, saying the retry was usable. |
| At 390 px, a long identifier inside a notice widened the page. | Notices wrap long words (a global style fix). |
| Loading, empty and error states were already consistent (`PolledView`). | Kept, and used by the new views. |

**The guided demo (`#/demo`).**
- **The replay.** It replays the pinned golden recording (`qwen3-8b-unfamiliar-parallel`) against the simulated shop. It goes through the same API (`POST /incidents/run`, provider `replay`, manual approval), the same safety gateway and the same approval dialog as the Incident Lab.
- **Labelled.** It carries the label **"Recorded real-model execution — replayed locally"**, and says which model's answers are replayed and from which day. It says that no model is called now and nothing real changes.
- **The ten steps.** Incident, runbooks and memory, strategy, agents, verifier, proposed fix, your decision, the fix, the independent recovery check, the outcome.
  - Each step's status, start time and facts come from the run's trace events (`src/view/demo.ts`). Nothing is staged.
  - A second investigation round makes the strategy step current again, and round one's "not enough evidence" verdict stays visible in amber.
  - After a rejection, the fix and recovery steps show as skipped, with the reason.
- **At the decision.** The page says what approving and rejecting each do, then opens the same approval dialog the Incident Lab uses.
- **Shared approval code.** That wiring is now one hook, `useRunApproval` in `pages/LiveRun.tsx`, used by both pages, so the demo cannot drift from the lab.
- **Controls:**
  - choose any step to read about it;
  - presenter notes (interview mode);
  - technical details (event numbers, categories, evidence ids, the data-source banner, the run id);
  - Stop;
  - Restart (a new replay run, no cleanup);
  - links into Trace Explorer and the other views.
- **No pause.** The execution architecture has no pause: the run waits at the decision by itself, and the pace is chosen at the start.
- **Paces.** Guided (0.25 of each answer's original model time, at most 15 s per answer) reached the approval in 105 s here; Quick (0.05) takes about half a minute; Instant takes seconds. Only the waiting changes, never what is replayed.
- **Interview mode** is `#/demo?mode=interview`, linked from the Overview: presenter notes on, same real frontend and APIs.
- **Earlier runs.** Finished demo runs are listed on the start screen and can be reviewed without signing in. A run that is not a replay never gets the replay label.

**Tests:**
- `npm test`: **669 passed** (627 before, plus 42 new).
- The new view tests use two real replays of the golden recording, approved and rejected, captured from a local server and trimmed into `apps/dashboard/tests/fixtures/`.
- `tsc` and `vite build` pass.
- The Python tests that read the docs and the dashboard's types: 200 passed. No Python code changed.
- **Browser smoke test: 97 of 97 passed.**
  - Setup: `e2e/smoke.mjs` ran on a freshly seeded demo folder, with the local API on this Mac (offline: replays and the scripted policy, no model).
  - The ten routes at 1440 px and 390 px.
  - The Guided Demo's view of a replay, a scripted run, a BLOCKED run and a BUDGET_EXHAUSTED run.
  - The Incident Lab demo.
  - The guided demo:
    - started from its screen at the Quick pace (the approval asked after 27 s);
    - the stepper advanced, and the model activity was marked replayed;
    - approved in the dialog: RESOLVED, and the API agreed;
    - the steps from the keyboard;
    - Restart started a new run, then Reject: BLOCKED, with the fix and recovery steps skipped, and the API agreed.
  - Every page: no console output, no sideways scroll (also with every section opened), the operator token never in the page, and the new accessibility probe found nothing. The probe checks that every control is named and every field labelled, that ids are neither duplicated nor dangling, and that there is one main landmark and one h1.
- **Not measured:**
  - no automated colour-contrast audit (no library was added);
  - usability needs a person: the specification's acceptance asks for a manual review.

**To review by hand:**
1. Start the demo server:
   - `python scripts/seed_demo.py --data-dir ~/recap-demo` (once);
   - then `cd apps/dashboard && npm run build`;
   - then `RECAP_DATA_DIR=~/recap-demo uvicorn apps.api.main:app`;
   - then open http://127.0.0.1:8000/.
2. **Overview:** can a first-time visitor say what RECAP does from the hero alone? Open Technical details: every earlier card is there.
3. **Try the guided demo:** sign in with `~/recap-demo/operator-token`, keep the Guided pace, then Start.
   - Read each step.
   - Choose an earlier step, then go back to the current one.
   - Turn on Presenter notes and Technical details.
4. **At step 7:** read the decision card, then approve in the dialog. Then Restart and reject the second run.
5. **Interview mode** from the Overview. Try a phone-width window (390 px).
6. **Judge:** is the plain language accurate and short enough? Is the amber "not enough evidence yet" of round one clear, and is the refused-answer notice clear?

## W2: published real-cluster runs, offline and read-only (2026-11-08)

**What a fresh checkout can now do:**
1. Start the API with an empty data folder.
2. Open **Published Runs**.
3. Open M5's real SREGym run, or a Kind run, in Trace Explorer, Evidence & Decisions, Safety &
   Approvals, Knowledge, Experience Memory, the Incident Lab or step by step.

No AWS, cluster or model is involved. Every page shows the run as "Recorded real Kubernetes
execution — historical, read-only". The design is in
[ADR 0033](decisions/0033-published-real-cluster-runs-read-only-behind-the-same-views.md).

**How it works:**
- **The allowlist.** `evals/live/MANIFEST.json` (schema `recap-published-runs-v1`) lists the four exported real-cluster runs.
  - Each entry gives the folder, run id, target (`kind` or `sregym`), evaluation batch, and the SHA-256 and size of each of the four files.
  - The title and notes are a person's; every other fact comes from the files.
- **The loader.** `evals/published.py` shows a run only after these checks:
  - the folder is a real folder inside `evals/live/`, with no links and size caps;
  - each file's bytes match their pins; the bytes are read once, then parsed as JSON only;
  - the record and every trace event validate against `RunRecord` and `TraceEvent`;
  - the files agree: one run id, the declared real target, the run's own recording, every approval in the trace, and the final state matching both the trace and the evaluation record;
  - no text is shaped like a credential (the reason never repeats it).
  - A run that fails is refused whole and listed with the reason.
  - `scripts/published_runs.py --check` runs the same checks; `--update` re-pins after a deliberate change.
- **The API:**
  - new `GET /published`;
  - the run views fall back to a published run when this server does not hold the id, with the new status `published`;
  - `GET /runs` still lists only this server's own runs;
  - events and stream serve the recorded trace, then end;
  - cancel answers 409;
  - the run's approvals are never loaded into the approval store, so they cannot be decided again or reused;
  - its recording is not a replay fixture.
- **The dashboard:**
  - a Published Runs page (in Start here, linked from the Overview and the no-runs state);
  - the historical banner and a "PUBLISHED · READ-ONLY" top-bar marker on every run page;
  - published runs in the run picker, in a group of their own;
  - Evaluations' record drawer links a published run's trace;
  - the Guided Demo's step-by-step view of a published run.
- **The grader stays separate.** SREGym's grader verdict is shown next to RECAP's oracle, never merged. The SREGym run's notes say it is not a held-out benchmark result.

**Open the SREGym run from a fresh checkout** (no AWS, cluster or model):
1. Install once: `python -m venv .venv && .venv/bin/pip install -c constraints.txt -e ".[dev]"`.
2. Build the dashboard: `cd apps/dashboard && npm ci && npm run build && cd ../..`.
3. Start the API on an empty folder: `RECAP_DATA_DIR="$(mktemp -d)" .venv/bin/uvicorn apps.api.main:app`.
4. Open http://127.0.0.1:8000/#/published and choose the SREGym card's **Trace Explorer**, **Evidence & Decisions**, **Safety & Approvals** or **Step by step**. Signing in is not needed to look.

Until W2 is pushed, a clone of GitHub has v0.6.0, without this work; the local repository already has it.

**Found and fixed while building:**
- **Evidence views.** Exports carry no evidence contents, so Trace Explorer and Evidence & Decisions said "not in this run", "This run recorded no tool result" and "Observations 0". They now say the registry was not published.
- **Safety & Approvals.** It said "No approval requested" and "This run asked for no approval", because the original server's decision log was not exported. It now shows the trace's recorded decision: "Approved (in the run's trace)".
- **Phone layout.** Hidden screen-reader text inside a table widened the page at 390 px. Table scroll boxes now contain it, a global style fix.

**Published, and not published:**
- **Full traces (4):**
  - Kind `run-047aac6eb0` (M5's deliverable);
  - Kind `run-f5b9e5bf45` (the red-team run with instructions planted in the logs);
  - Kind `run-34a3580d2f` (Mission Control's acceptance run);
  - SREGym `run-9d7f47104d` (SREGym's grader: pass; not held out).
- **Evaluation records only (8):**
  - Kind: cancelled; rejected twice; insufficient evidence.
  - SREGym: blocked by the evidence gate (grader: fail); approval expired; wrong mechanism twice.
  - Their full traces are only in the owner's run stores (`~/recap-kind-runs`, `~/recap-sregym-runs`; backed up in W0). Publishing any of them is the owner's decision: export, add a manifest entry with prose, then `--check`.
- **Not exported for any run:**
  - the evidence registry's contents (the tool outputs; what the model was shown is in `recording.jsonl`);
  - the original server's approval decision log.
- **SREGym's grader.** It was run by hand. Its verdict is the evaluation record's `benchmark_verdict`, and its output for the passing run is quoted in `evals/live/README.md`.
- **The M2 and M3 simulated exports** in `evals/live/` are not on the allowlist. W2 is about the real-cluster runs; the M3 golden run is the Guided Demo's replay.

**Tests:**
- **Python, new:**
  - `tests/unit/test_published_runs.py`: 26 tests on copies of the real exports, covering altered, replaced and missing files, schema and version mismatches, files that disagree, links, path traversal, credentials, malformed JSON, oversized files, and offline loading.
  - `tests/e2e/test_api_published.py`: 6 tests covering the list, every run view, the grader kept separate, read-only enforcement, a tampered artifact never served, and this server's own copy winning.
  - Both files pass, and so do the existing API and contract tests.
- **Python, full suite, run once:** **2297 passed, 7 skipped, 0 failed**. Also clean: `ruff check`, `ruff format --check`, `mypy`, the secret scan, and the recordings manifest check.
- **Dashboard:** `npm test`: **689 passed** (669 before, plus 20 new); `tsc` and `vite build` pass.
- **Browser smoke test on an empty data folder** (a fresh checkout), on the final build: **77 of 77 passed.** It covered:
  - published runs on every run page at 1440 px;
  - Published Runs and a published trace at 390 px;
  - cancel refused (409) even when signed in;
  - the Incident Lab demo and the guided demo (approve, restart, reject).
- **Pages-only regression run on the seeded demo data:** **100 passed, 0 failed, 1 skipped** (the skip is the demo flows, already run above). It checked this server's own scripted, replayed, blocked and out-of-budget runs alongside the published ones. A first pass failed 2 checks: my new "no control that acts" check matched the audit log's "Approved 11" filter chip. The check now matches the actual action labels exactly.

## Workstream status

| Workstream | Status |
|---|---|
| W0 baseline, evidence, cost | done |
| W1 Mission Control UX and guided demo | done: implemented and checked; the manual review is the owner's |
| W2 published-run import | done: implemented and checked locally; the owner's review is pending |
| W3–W7 | not started: not authorized yet |
