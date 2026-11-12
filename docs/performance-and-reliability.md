# Performance and reliability

> **M6 W6, measured on 2026-11-12 on the owner's laptop.** One machine, the simulated
> environment and the scripted baseline (no model): these are measurements of RECAP's backend,
> not a capacity figure for any deployment. A real model's latency (minutes per request on a
> CPU, [W5](w5-protocol.md#results)) would dominate any real run.

## In short

- **Throughput is bounded by the worker slots, as designed.** Two workers running two incidents
  each (4 slots) finished about 1.2 to 1.3 scripted incidents per second at every load; beyond
  4 at once, more load meant a longer queue wait, not more throughput, and each incident's own
  time stayed about 2.6 to 3.0 s.
- **Backpressure is exact.** With 40 submitted at once, 24 were accepted (4 running, 20 waiting:
  the queue's capacity) and 16 were refused with 429, in all three runs. No request got a 5xx at
  any load, and every accepted incident was RESOLVED with its own record and its own approval.
- **A bottleneck found and fixed.** Every submission re-read all 17 scenario files with
  PyYAML's pure-Python parser (about 40 ms here, 57 ms in the container) before its first
  `await`, so a burst of N submissions all answered after about N × 57 ms (2.3 s for 40), and
  reads in flight waited with them (up to 2.0 s). The C safe loader (the same safe constructor)
  cut that to 14 ms per submission: the burst of 40 now answers in 0.56 s, the slowest read in
  0.47 s ([recap/environments/simulated/scenario.py](../recap/environments/simulated/scenario.py)).
- **Prometheus sees the real numbers.** It scraped the API and both workers (found through
  Compose's DNS) and counted what the load test counted (below).
- **Faults and attacks: covered, with one defect fixed.** All 12 failure cases and 16 attacks
  the spec lists map to tests that inject them. Mapping them found one defect, now fixed: a second
  cancellation during an in-flight write lost the write's outcome. One gap, reads without a token
  on loopback, now has its test.

## The load test

**Setup.** macOS-26.3.1-arm64-arm-64bit, 8 CPUs and 16 GiB; Docker in a colima VM
with 4 CPUs and 3.8 GiB (Ubuntu 24.04.4 LTS, Docker
29.5.2, aarch64). The Compose stack of
[infra/compose/](../infra/compose/README.md): Redis 7.4.2, the API, two worker replicas each
running up to two incidents at once, queue capacity 20, and Prometheus scraping every 15 s.
[scripts/api_load_test.py](../scripts/api_load_test.py) `--url` sends N simulated operators at
once (the levels 4, 16 and 40, one after another): each submits one `bad-deployment` incident
(the scripted baseline, memory off), approves its own run's exact proposal through
`POST /approvals/{proposal_id}`, and waits for the run to finish. Runs 1 and 2 were before the
scenario-loader fix, run 3 after it. The reports and Prometheus's answers are in
[evals/reports/load/2026-11-12-compose-two-workers/](../evals/reports/load/2026-11-12-compose-two-workers/README.md).

| Run | Offered at once | Accepted | 429 | Resolved | Runs per second | Incident s p50 / p95 | Queue wait s p50 / p95 | HTTP ms p50 / p95 / p99 | Slowest submission s | 5xx |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 (before the fix) | 4 | 4 | 0 | 4 | 1.24 | 2.6 / 2.7 | 0.0 / 0.0 | 6 / 18 / 272 |  | 0 |
| 1 (before the fix) | 16 | 16 | 0 | 16 | 1.29 | 2.7 / 2.9 | 3.0 / 8.7 | 5 / 16 / 79 |  | 0 |
| 1 (before the fix) | 40 | 24 | 16 | 24 | 1.29 | 2.7 / 3.0 | 5.9 / 13.8 | 5 / 20 / 2469 |  | 0 |
| 2 (before the fix) | 4 | 4 | 0 | 4 | 1.25 | 2.6 / 2.7 | 0.0 / 0.0 | 6 / 14 / 249 | 0.25 | 0 |
| 2 (before the fix) | 16 | 16 | 0 | 16 | 1.25 | 2.8 / 3.1 | 3.0 / 9.2 | 5 / 32 / 226 | 0.87 | 0 |
| 2 (before the fix) | 40 | 24 | 16 | 24 | 1.28 | 2.7 / 3.0 | 6.2 / 14.3 | 5 / 27 / 2288 | 2.30 | 0 |
| 3 (after the fix) | 4 | 4 | 0 | 4 | 1.26 | 2.7 / 2.8 | 0.0 / 0.0 | 7 / 18 / 61 | 0.06 | 0 |
| 3 (after the fix) | 16 | 16 | 0 | 16 | 1.32 | 2.7 / 2.8 | 3.0 / 9.0 | 4 / 17 / 65 | 0.25 | 0 |
| 3 (after the fix) | 40 | 24 | 16 | 24 | 1.20 | 3.0 / 3.4 | 6.2 / 16.6 | 6 / 71 / 474 | 0.56 | 0 |

Run 1 did not yet break latency down by route; runs 2 and 3 did, which is how the submission
bottleneck was found. Peak use per container over all three runs (`docker stats`, sampled every
second while a level ran):

| Container | Peak CPU (cores) | Peak memory (MiB) |
|---|---|---|
| api-1 | 1.00 | 84 |
| prometheus-1 | 0.01 | 81 |
| redis-1 | 0.04 | 14 |
| worker-1 | 1.89 | 184 |
| worker-2 | 1.74 | 184 |

What these numbers do not say: one laptop, one Docker VM, scripted incidents of about 3 s on
the simulator. A real model's request takes seconds (hosted) to minutes (a CPU, as in W5), so a
real deployment is bounded by inference long before the queue, the API or Redis. Throughput here
is the four worker slots' turnover; more workers or more concurrency per worker would add slots
until the API's single event loop or the 4 CPUs bind.

How to reproduce (Docker running; Compose as in [infra/compose/README.md](../infra/compose/README.md)):

```bash
export RECAP_OPERATOR_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
RECAP_METRICS=1 RECAP_API_PORT=8001 docker compose -f infra/compose/compose.yaml -p recap-w6 \
  --profile metrics up -d --build --scale worker=2 redis api worker prometheus
python scripts/api_load_test.py --url http://127.0.0.1:8001 --loads 4,16,40 --docker-stats --out report.json
docker compose -f infra/compose/compose.yaml -p recap-w6 --profile metrics down -v
```

## Metrics

RECAP's Prometheus metrics are off unless `RECAP_METRICS=1` ([apps/api/metrics.py](../apps/api/metrics.py)).
The API serves `GET /metrics`; with the Redis backend, each worker serves its own runs' metrics
on its own port (`RECAP_WORKER_METRICS_PORT`, [apps/worker/metrics.py](../apps/worker/metrics.py)),
and Compose's `metrics` profile runs a Prometheus that finds every worker replica through
Compose's DNS ([infra/compose/prometheus.yml](../infra/compose/prometheus.yml)). Every label
value comes from a closed set: never an id, a prompt, model text, a path or a secret (a test
parses every exposition and checks each label against its set).

| What the spec asks for | Metric (W6: added in M6 W6) | Served by |
|---|---|---|
| API latency and error rate | `recap_api_requests_total` (method, route template, status class), `recap_api_request_duration_seconds` | API |
| Active and queued incidents | `recap_active_incidents`; `recap_queue_depth`, `recap_queue_capacity` | API (runs in process); API, from Redis |
| Worker occupancy | `recap_workers` (alive), `recap_workers_busy` (W6) | API, from Redis |
| Queue wait | `recap_queue_wait_seconds` (W6: every worker's claims, one histogram kept in Redis) | API, from Redis |
| Tool-call latency and failures | `recap_tool_calls_total` (tool, outcome: ok, error, denied), `recap_tool_call_duration_seconds` | where the run executes |
| Model request duration | `recap_model_request_duration_seconds` (W6, by provider class) | where the run executes |
| Token consumption | `recap_model_tokens_total` (as the model server reported them, never estimated) | where the run executes |
| Job completion and failure | `recap_jobs_total` (submitted, completed, failed, cancelled, needs_reconciliation, requeued) | API, from Redis |
| Approval waits | `recap_approval_wait_seconds` (W6, by decision) | where the run executes |
| Recovery outcomes | `recap_recovery_verifications_total`, `recap_actions_total`, `recap_incidents_finished_total` | where the run executes |

Metrics complement the traces, they do not repeat them: a trace says what one run did and why,
a metric counts and times what all runs did. Nothing here reads a run record or changes a run.

### What Prometheus scraped during run 1

After run 1's three levels, Prometheus's own answers ([prometheus-after-run-1.json](../evals/reports/load/2026-11-12-compose-two-workers/prometheus-after-run-1.json)):

| Query | Answer |
|---|---|
| targets up, by job | job=recap-api: 1, job=recap-workers: 2 |
| incidents finished (from the workers' endpoints) | state=BLOCKED: 0, state=BUDGET_EXHAUSTED: 0, state=FAILED: 0, state=QUOTA_EXHAUSTED: 0, state=RESOLVED: 44 |
| fixes, by outcome | outcome=blocked: 0, outcome=executed: 44, outcome=expired: 0, outcome=refused: 0, outcome=rejected: 0 |
| recovery verdicts | verified=false: 0, verified=true: 44 |
| approval waits, by decision | decision=approved: 44 |
| jobs, by event (from Redis) | event=cancelled: 0, event=completed: 44, event=failed: 0, event=needs_reconciliation: 0, event=requeued: 0, event=submitted: 44 |
| queue waits counted; their mean (s) | job=recap-api: 44; job=recap-api: 5.42595 |
| API requests, by status class | status_class=2xx: 5506, status_class=4xx: 16 |
| model requests (the scripted baseline sends none) | value: 0 |

The counts agree with the load test: 44 incidents accepted and resolved (4 + 16 + 24), 44 jobs,
16 API answers in the 4xx class (the 429s) and no 5xx. Two cautions: a quantile computed from
histogram buckets is coarse (the queue wait's p95 from buckets was 24.5 s, the load test measured
13.8 s), and a 15 s scrape interval smooths short peaks (the queue depth Prometheus saw peaked at
4, the queue briefly held 20); a query right after a level reads the last scrape, up to 15 s old.

## Fault injection

Each failure the spec names, and the tests that inject it (paths under `tests/`). Mapped in W6 by
reading every test that applies, then checked by running them; the matrix lists the most direct
ones.

| Failure | Injected by | What happens |
|---|---|---|
| API restart | `e2e/test_api_workers.py::test_a_run_waiting_for_a_person_survives_an_api_restart`, `e2e/test_api_evals.py` (another process's run), `e2e/test_process_interruption.py` | Redis backend: the run goes on in its worker and a new API instance accepts its approval. In-process backend: a run whose API died stays at its last checkpoint (shown as `external`), its approval request unusable (by design, [ADR 0034](decisions/0034-durable-workers-redis-coordinates-the-run-records-decide.md)) |
| Worker termination | `integration/test_worker_crashes.py` (a real worker SIGKILLed at 7 points), `integration/test_workers.py::test_a_dead_worker_s_job_is_settled_and_run_by_another` | Another real worker settles the job from the run's own evidence: runs it again when nothing could have been written, or ends it `needs_reconciliation` for a person when a write may have reached the target |
| Redis unavailable | `e2e/test_api_workers.py::test_a_redis_outage_refuses_what_needs_the_queue_and_still_serves_reads`, `integration/test_workers.py` (short and long outages), `unit/test_api_metrics.py` | Submissions get 503 ("nothing was queued"), reads are still served; a short outage loses no outcome; a long one fences the worker off and the job is settled afterwards; `/metrics` answers with `recap_queue_up 0` |
| MCP server failure | `e2e/test_runtime_faults.py` (cannot start; dies while agents read; while the fix is drafted; while it is sent) | The run ends FAILED naming the lost server: no write, the approval used at most once, the namespace lease given back |
| Model timeout | `e2e/test_audit_retry_and_stabilization.py`, `unit/test_orchestrator.py::test_a_timeout_becomes_an_error_finding_and_the_other_agent_survives` | Cut at the run's deadline (BUDGET_EXHAUSTED) or FAILED on the provider's own timeout, with no write; one agent's timeout leaves the other's finding |
| Tool timeout | `unit/test_gateway.py::test_transport_crashes_and_timeouts_become_error_results`, `unit/test_mcp_server.py::test_slow_tool_times_out_and_the_connection_stays_usable`, `unit/test_kind_kubectl.py::test_a_hung_kubectl_is_killed_at_its_time_limit` | An error result the agents see; the connection stays usable; a hung `kubectl` is killed |
| Concurrent incident conflict | `e2e/test_api_concurrency.py`, `unit/test_gateway.py::test_conflicting_remediation_on_the_same_target_is_denied` | A second run on a held Kind namespace is refused; simulated runs stay independent; a conflicting fix on one target is denied |
| Cancel during execution | `integration/test_controller_paths.py`, `integration/test_workers.py::test_cancelling_a_queued_job_and_a_running_one`, `e2e/test_kind_pipeline.py`, `e2e/test_w6_faults.py` (W6) | A queued job is taken off the queue; a running one stops, and a write already sent is awaited and recorded. **Found and fixed in W6:** a second cancellation (a second click, or a worker fencing itself at each missed heartbeat) used to cancel the write in flight, and the record then said the fix "was not sent" |
| Interrupted approval | `integration/test_worker_crashes.py` (killed while waiting), `e2e/test_process_interruption.py`, `integration/test_workers.py::test_a_stopping_worker_hands_its_run_back_and_the_retry_asks_again` | The old request can never be used again; a retried run asks a person again |
| Interrupted remediation | `integration/test_worker_crashes.py` (windows 5 and 6: killed before and after the write) | A write that may have been sent is never sent again: the job ends `needs_reconciliation` and a person checks |
| Kubernetes unavailable | `unit/test_kind_engine.py` (a write the read-back cannot confirm is an error; the capability's nonce is burnt even when the write fails), `unit/test_kind_kubectl.py` | Safe failure on the fake cluster; no real cluster was available in W6, so no run-level test makes a real cluster stop answering |
| Uncertain writes never repeated | `integration/test_worker_crashes.py::test_window_6_killed_after_the_write_it_is_never_sent_again`, `unit/test_gateway.py::test_replaying_an_approval_is_denied`, `unit/test_kind_engine.py::test_the_nonce_is_burnt_even_when_the_write_then_fails` | Prevented: approvals and capabilities are single-use, and settling never re-sends an uncertain write |

## Security regressions

Each attack the spec names, the tests that make it, and what they show. **Attempted against
executed:** every unsafe action these tests attempt (deleting a namespace on instructions found
in logs, runbooks, memories or tool output; writes without an approval or from a second server
process; forged, replayed or cross-run approvals and capabilities; writes without the token) is
refused, and none runs. The only test in which an unsafe action runs is a deliberate ablation
with safety switched off on the simulator (`adversarial/test_prompt_injection_gateway.py::test_safety_off_ablation_shows_what_the_unsafe_action_would_do`),
which is refused on any live target.

| Attack | Tests | Result |
|---|---|---|
| Prompt injection in Kubernetes logs and events | `unit/test_gateway.py` (cluster events and evidence flagged, kept as data), `e2e/test_kind_pipeline.py::test_a_planted_instruction_in_the_logs_changes_nothing` | Detected (flagged) and prevented; events are tested at the gateway, logs end to end on the Kind fake |
| Poisoned runbooks | `adversarial/test_poisoned_runbook.py`, `adversarial/test_prompt_injection_gateway.py::test_injected_runbook_text_is_cited_but_still_blocked` | Retrieved verbatim, flagged, outside the trusted catalog, blocked |
| Manipulated episodic memories | `adversarial/test_poisoned_memory.py` | False advice rejected (the evidence decides); planted text never reaches trusted prompt text |
| Tool-response injection | `adversarial/test_malicious_tools.py`, `unit/test_gateway.py::test_an_mcp_server_cannot_put_documents_into_an_agent_context` | Bounded, flagged and kept untrusted |
| Unauthorized MCP writes | `adversarial/test_audit_executor_authority.py`, `e2e/test_kind_pipeline.py::test_the_write_boundary_holds_inside_the_mcp_server_for_any_caller` | Prevented: no write without an approval, none from a second server process |
| Cross-namespace actions | `unit/test_policy.py`, `integration/test_controller_paths.py`, `adversarial/test_prompt_injection_gateway.py::test_any_namespace_the_injection_names_is_denied` | Denied before anything is sent |
| Forged evidence references | `unit/test_verifier.py`, `integration/test_m4_robustness.py` | A claim citing unknown or another run's evidence is never supported |
| Approval bypass | `unit/test_gateway.py` (no approval; forged or stale approval objects), `e2e/test_audit_api_security.py` | Prevented |
| Replay of consumed capabilities | `unit/test_capabilities.py`, `unit/test_kind_engine.py::test_a_capability_works_once` | Prevented: a capability works once |
| Confused deputy across targets | `unit/test_kind_engine.py` (every invalid capability refused), `unit/test_capabilities.py`, `adversarial/test_audit_executor_authority.py` (an approval from a finished run unlocks nothing else) | Prevented; each binding (run, signing key, incident, proposal digest) is tested on its own, not one simulator-to-Kind run end to end |
| Malicious imported trace files | `unit/test_published_runs.py` (altered, linked, oversized files) | Refused whole; the other published runs still load |
| Stale or misleading evidence | `unit/test_verifier.py` (old OOM kills, a deployment that came too late), `adversarial/test_audit_forged_evidence.py` | Contradicted |
| Reads on loopback, writes need the token | `e2e/test_api_security.py` (every write refused with a wrong or no token; **W6:** run data, evidence, pending approvals and published runs readable without one) | As designed: the dashboard reads freely on loopback, nothing changes without the operator token |
| Approvals bound to the run's real target | `e2e/test_api_security.py` (a live run's approval needs the service's exact name, even on a server configured for the simulator), `unit/test_console_approval_live.py` | Prevented |
| Imported artifacts never act | `e2e/test_api_published.py::test_nothing_can_act_on_a_published_run`, `unit/test_published_runs.py::test_loading_reads_only_and_opens_no_connection` | Prevented |
| False positives | `adversarial/test_m4_safety.py::test_a_correct_fix_still_runs_with_every_lure_present`, `adversarial/test_audit_forged_evidence.py` | Valid fixes still run: a blocked unsafe action is a security success, not a resolution |

## What W6 did not cover

- **A real cluster that stops answering.** Kubernetes failures are tested on the fake cluster;
  the real-cluster tests (`e2e/test_kind_live.py`) need the VM and were not run in W6.
- **Real Redis dropping connections mid-run** (`integration/test_workers_redis.py`) needs
  `RECAP_TEST_REDIS_URL` and was not run in W6 (it passed against a real Redis in W3's final
  verification). The load test above used a real Redis.
- **A partial network partition** during an in-flight write: fencing relies on timing
  ([ADR 0034](decisions/0034-durable-workers-redis-coordinates-the-run-records-decide.md)'s residual risk);
  a person's fresh approval stands between it and a second write.
- **Load with a model.** The load test uses the scripted baseline; it measures the backend, not
  inference.
