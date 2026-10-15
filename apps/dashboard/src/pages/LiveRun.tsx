import { useEffect, useMemo, useState } from "react";
import { api, toApiError } from "../api";
import type { ApiError } from "../api";
import { ApprovalCard } from "../components/ApprovalCard";
import { Chip, Mono, StateChip, YesNoChip } from "../components/Badge";
import { Card, KeyValues, Notice, PageHeader, Segmented, StatTile, Toggle } from "../components/Card";
import { ErrorState, Loading } from "../components/EmptyState";
import { EventFeed } from "../components/EventFeed";
import { Icon } from "../components/Icon";
import { RunLinks } from "../components/RunPicker";
import { StateStepper } from "../components/StateStepper";
import { costLabel, fmtArgs, fmtInt, fmtMs, isTerminal, num, obj, str, stateTone, tsMs } from "../format";
import { navigate, usePolling, useRunEvents } from "../hooks";
import type { PageProps, RunEventsState } from "../hooks";
import type {
  ApprovalRequest,
  ExperienceEpisode,
  ProposalPayload,
  RunConfigInput,
  RunDetail,
  RunSummaryPayload,
  ScenarioInfo,
  SystemSummary,
  TraceEvent,
} from "../types";

const DEFAULT_CONFIG: RunConfigInput = {
  strategy: "adaptive",
  use_rag: true,
  use_memory: true,
  approval_mode: "manual",
  tool_latency_ms: 120,
};

const PARTITION_ORDER: Record<string, number> = { demo: 0, train: 1, test: 2 };

const TRANSPORT_LABEL: Record<RunEventsState["transport"], string> = {
  idle: "idle",
  sse: "SSE stream",
  polling: "polling /events",
  recorded: "recorded events",
};

function lastOf(events: TraceEvent[], category: string): TraceEvent | undefined {
  for (let i = events.length - 1; i >= 0; i--) if (events[i].category === category) return events[i];
  return undefined;
}

/** A scenario may pin some run settings (e.g. a strategy); pre-fill the form with them. */
function withOverrides(config: RunConfigInput, scenario: ScenarioInfo): RunConfigInput {
  const o = obj(scenario.run_overrides);
  const next = { ...config };
  if (o.strategy === "adaptive" || o.strategy === "single" || o.strategy === "parallel") next.strategy = o.strategy;
  if (typeof o.use_rag === "boolean") next.use_rag = o.use_rag;
  if (typeof o.use_memory === "boolean") next.use_memory = o.use_memory;
  if (o.approval_mode === "manual" || o.approval_mode === "auto") next.approval_mode = o.approval_mode;
  if (typeof o.tool_latency_ms === "number") next.tool_latency_ms = o.tool_latency_ms;
  return next;
}

/** Demo prerequisites (e.g. memory-caution needs a verified bad-deployment episode). */
function missingPrerequisites(scenario: ScenarioInfo, episodes: ExperienceEpisode[] | undefined): string[] {
  if (!episodes) return [];
  return scenario.requires_memory_of.filter(
    (needed) => !episodes.some((ep) => ep.scenario_id === needed && ep.outcome === "success" && ep.verified),
  );
}

// ======================================================================================
// Scenario picker
// ======================================================================================

function ScenarioCard({
  scenario,
  selected,
  onSelect,
  showTruth,
  missing,
}: {
  scenario: ScenarioInfo;
  selected: boolean;
  onSelect: () => void;
  showTruth: boolean;
  missing: string[];
}) {
  const overrides = Object.entries(obj(scenario.run_overrides));
  return (
    <button type="button" className={selected ? "scenario selected" : "scenario"} aria-pressed={selected} onClick={onSelect}>
      <span className="scenario-top">
        <span className="scenario-title">{scenario.title}</span>
        <Chip tone={scenario.partition === "demo" ? "accent" : "neutral"}>{scenario.partition}</Chip>
      </span>
      <code className="mono muted">{scenario.id}</code>
      {scenario.description ? <span className="scenario-desc">{scenario.description}</span> : null}
      {scenario.tags.length ? (
        <span className="chip-row">
          {scenario.tags.map((t) => (
            <Chip key={t}>{t}</Chip>
          ))}
        </span>
      ) : null}
      {overrides.length ? (
        <span className="scenario-note">
          <Icon name="info" size={13} /> scenario sets {overrides.map(([k, v]) => `${k}=${str(v)}`).join(", ")}
        </span>
      ) : null}
      {scenario.requires_memory_of.length ? (
        <span className={missing.length ? "scenario-note warn" : "scenario-note ok"}>
          <Icon name="memory" size={13} />
          {missing.length
            ? `needs a verified episode from ${missing.join(", ")} — run it first (memory on)`
            : `prerequisite episode from ${scenario.requires_memory_of.join(", ")} is in memory`}
        </span>
      ) : null}
      {showTruth ? (
        <span className="truth">
          <span className="truth-label">ground truth · hidden from agents</span>
          <span>
            family <code className="mono">{scenario.family}</code>
          </span>
          {Object.entries(obj(scenario.expected)).map(([k, v]) => (
            <span key={k}>
              {k} <code className="mono">{str(v)}</code>
            </span>
          ))}
        </span>
      ) : null}
    </button>
  );
}

function ConfigForm({
  config,
  onChange,
}: {
  config: RunConfigInput;
  onChange: (config: RunConfigInput) => void;
}) {
  const set = <K extends keyof RunConfigInput>(key: K, value: RunConfigInput[K]) => onChange({ ...config, [key]: value });
  const hints: Record<RunConfigInput["strategy"], string> = {
    adaptive: "The router picks single or parallel from observable signals and the remaining budget.",
    single: "Always one generalist investigator.",
    parallel: "Telemetry and infrastructure specialists run concurrently, then the verifier merges evidence.",
  };
  return (
    <div className="config-grid">
      <div className="field">
        <span className="field-label">Strategy</span>
        <Segmented
          label="Strategy"
          value={config.strategy}
          onChange={(v) => set("strategy", v)}
          options={[
            { value: "adaptive", label: "Adaptive" },
            { value: "single", label: "Single" },
            { value: "parallel", label: "Parallel" },
          ]}
        />
        <span className="field-hint">{hints[config.strategy]}</span>
      </div>
      <div className="field">
        <span className="field-label">Approval</span>
        <Segmented
          label="Approval mode"
          vertical
          value={config.approval_mode}
          onChange={(v) => set("approval_mode", v)}
          options={[
            { value: "manual", label: "Manual — you approve here" },
            { value: "auto", label: "Auto — simulation auto-approver" },
          ]}
        />
        <span className="field-hint">
          {config.approval_mode === "manual"
            ? "The run pauses at AWAITING_APPROVAL until you approve or reject the exact proposal below."
            : "The simulation auto-approver decides; it is refused outside a simulated environment."}
        </span>
      </div>
      <div className="field">
        <span className="field-label">Context</span>
        <Toggle checked={config.use_rag} onChange={(v) => set("use_rag", v)} label="Runbook retrieval (RAG)" hint="BM25 over fixtures/knowledge" />
        <Toggle checked={config.use_memory} onChange={(v) => set("use_memory", v)} label="Experience memory" hint="past episodes, checked against live evidence" />
      </div>
      <div className="field">
        <label className="field-label" htmlFor="latency">
          Simulated tool latency (ms)
        </label>
        <input
          id="latency"
          className="input"
          type="number"
          min={0}
          max={5000}
          step={10}
          value={config.tool_latency_ms ?? ""}
          onChange={(e) => {
            const value = Number.parseInt(e.target.value, 10);
            set("tool_latency_ms", Number.isFinite(value) ? Math.max(0, Math.min(5000, value)) : null);
          }}
        />
        <span className="field-hint">Higher latency makes parallel overlap easier to see on the timeline.</span>
      </div>
    </div>
  );
}

// ======================================================================================
// Run view
// ======================================================================================

function ResultPanel({
  detail,
  summary,
  system,
  runId,
}: {
  detail: RunDetail | undefined;
  summary: RunSummaryPayload | undefined;
  system: SystemSummary | undefined;
  runId: string;
}) {
  const record = detail?.record;
  if (!record) {
    return detail?.error ? (
      <Notice tone="bad" icon="x" title="The run failed before writing a checkpoint">
        {detail.error}
      </Notice>
    ) : (
      <Loading label="Loading the final checkpoint…" />
    );
  }
  const best = record.verification?.best;
  const recovery = record.recovery;
  const providerKind = system && record.provider === system.provider.name ? system.provider.kind : undefined;
  const u = record.usage;
  // Where the run happened comes from its own record; the simulator is never assumed.
  const env = record.environment;
  const where = !env
    ? undefined
    : env.is_simulated
      ? "SIMULATED LOCAL ENVIRONMENT — no real system was changed"
      : `LIVE ENVIRONMENT (${env.kind})`;
  return (
    <Card title="Result" subtitle={where} tone={stateTone(record.state)}>
      <div className="result-head">
        <StateChip state={record.state} />
        <span>{record.outcome_note || "—"}</span>
      </div>
      <KeyValues
        rows={[
          [
            "Diagnosis",
            best ? (
              <span className="cell-stack">
                <span>{best.claim}</span>
                <Mono>
                  {best.mechanism}@{best.target_service ?? "?"}
                </Mono>
              </span>
            ) : (
              <span className="muted">no supported diagnosis</span>
            ),
          ],
          [
            "Oracle verdict",
            recovery ? (
              <span className="chip-row">
                <YesNoChip value={recovery.verified} yes="recovery verified" no="not recovered" />
                <span className="muted">
                  healthy before {recovery.healthy_before ? "yes" : "no"} → after {recovery.healthy_after ? "yes" : "no"} ·{" "}
                  <Mono>{recovery.oracle_name}</Mono>
                </span>
              </span>
            ) : (
              <span className="muted">not consulted — no mitigation was executed</span>
            ),
          ],
          [
            "Approval",
            record.approvals.length ? (
              <span className="cell-stack">
                {record.approvals.map((a) => (
                  <span key={a.approval_id} className="chip-row">
                    <Chip tone={a.decision === "approved" ? "ok" : "bad"}>{a.decision}</Chip>
                    <span>by {a.actor}</span>
                    <Mono nowrap>{a.approval_id}</Mono>
                    <span className="muted">{a.consumed_at ? "single use, consumed" : "not consumed"}</span>
                  </span>
                ))}
              </span>
            ) : (
              <span className="muted">none recorded</span>
            ),
          ],
          ["Episode written", record.episode_id ? <Mono>{record.episode_id}</Mono> : <span className="muted">not written</span>],
          [
            "Usage",
            <span>
              {fmtInt(u.steps)} step{u.steps === 1 ? "" : "s"} · {fmtInt(u.tool_calls)} tool call{u.tool_calls === 1 ? "" : "s"} ·{" "}
              {fmtInt(u.estimated_tokens)} tokens{" "}
              <Chip>estimated</Chip>
            </span>,
          ],
          ["Cost", costLabel(summary?.cost, providerKind)],
          [
            "Run config",
            <Mono>
              {fmtArgs({
                strategy: record.config.strategy,
                use_rag: record.config.use_rag,
                use_memory: record.config.use_memory,
                approval_mode: record.config.approval_mode,
                planner: record.config.planner,
                tool_latency_ms: record.config.tool_latency_ms,
              })}
            </Mono>,
          ],
          [
            "Unsafe actions",
            summary ? (
              <span>
                {fmtInt(num(summary.unsafe_attempts))} attempted · {fmtInt(num(summary.unsafe_blocked))} blocked ·{" "}
                <strong className={num(summary.unsafe_executed) ? "text-bad" : undefined}>{fmtInt(num(summary.unsafe_executed))} executed</strong>
              </span>
            ) : (
              "—"
            ),
          ],
        ]}
      />
      {record.error ? (
        <Notice tone="bad" icon="x" title="Run error">
          {record.error}
        </Notice>
      ) : null}
      <div className="result-links">
        <span className="muted">Inspect this run:</span>
        <RunLinks runId={runId} />
      </div>
    </Card>
  );
}

function LiveFacts({ events }: { events: TraceEvent[] }) {
  const count = (category: string) => events.filter((e) => e.category === category).length;
  const modes = events.filter((e) => e.category === "routing.decision").map((e) => str(e.payload.mode, "?"));
  const elapsed = events.length > 1 ? tsMs(events[events.length - 1].ts) - tsMs(events[0].ts) : 0;
  const injections = count("safety.injection_flag");
  const denials = events.filter((e) => e.category === "policy.decision" && e.payload.verdict === "DENY").length;
  return (
    <div className="stat-grid compact">
      <StatTile label="Elapsed" value={fmtMs(elapsed)} tone="neutral" />
      <StatTile label="Tool calls" value={fmtInt(count("tool.call") + count("mitigation.executed"))} tone="neutral" />
      <StatTile label="Routing" value={modes.length ? modes.join(" → ") : "—"} tone="accent" />
      <StatTile label="Injection flags" value={fmtInt(injections)} tone={injections ? "warn" : "neutral"} />
      <StatTile label="Policy denials" value={fmtInt(denials)} tone={denials ? "bad" : "neutral"} />
    </div>
  );
}

/** POST /runs/{id}/cancel (operator only). A pending approval request expires with the run. */
function CancelRunButton({ runId }: { runId: string }) {
  const [state, setState] = useState<"idle" | "sending" | "sent">("idle");
  const [error, setError] = useState<string | null>(null);
  async function cancel() {
    setState("sending");
    setError(null);
    try {
      await api.cancelRun(runId);
      setState("sent");
    } catch (err) {
      setError(toApiError(err).message);
      setState("idle");
    }
  }
  return (
    <>
      <button
        type="button"
        className="btn btn-reject"
        disabled={state !== "idle"}
        title="Stops the run; nothing is executed afterwards"
        onClick={() => void cancel()}
      >
        <Icon name="stop" size={14} /> {state === "sent" ? "Cancelling…" : "Cancel run"}
      </button>
      {error ? (
        <span className="small operator-warn" role="alert">
          {error}
        </span>
      ) : null}
    </>
  );
}

function RunPanel({ runId, system, onNewRun }: { runId: string; system: SystemSummary | undefined; onNewRun: () => void }) {
  const stream = useRunEvents(runId);
  const events = stream.events;
  const states = useMemo(
    () => events.filter((e) => e.category === "state.transition").map((e) => str(e.payload.to)).filter(Boolean),
    [events],
  );
  const current = states.length ? states[states.length - 1] : null;
  const finished = stream.finished || isTerminal(current);
  // A run that another process is running is shown as recorded; nothing here follows it.
  const live = !finished && stream.transport !== "recorded";

  // Approval: the run waits while a request has no decision after it.
  const requested = lastOf(events, "approval.requested");
  const decided = lastOf(events, "approval.decided");
  const awaiting =
    live && (current === "AWAITING_APPROVAL" || (!!requested && (!decided || decided.seq < requested.seq)));
  const pending = usePolling(() => api.pendingApprovals(), awaiting ? 1500 : null, [runId]);
  const request = pending.data?.find((r) => r.run_id === runId) ?? null;
  const [lastRequest, setLastRequest] = useState<ApprovalRequest | null>(null);
  const [decidedHere, setDecidedHere] = useState<Set<string>>(() => new Set());
  useEffect(() => {
    if (request) setLastRequest(request);
  }, [request?.proposal_id]); // eslint-disable-line react-hooks/exhaustive-deps
  const shown = request ?? lastRequest;
  // approval_mode=auto: the simulation auto-approver decides; offering buttons would only race it.
  const autoChannel = /auto/i.test(str(requested?.payload.channel));
  // Keep the card (and the server's response) visible after deciding here, until the run ends.
  const showApproval =
    !!shown && !finished && !autoChannel && (awaiting || decidedHere.has(shown.proposal_id));

  const trustedRisk = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i].category !== "mitigation.proposed") continue;
      const p = events[i].payload as ProposalPayload;
      if (p.proposal?.proposal_id === shown?.proposal_id) return str(p.trusted_risk) || undefined;
    }
    return undefined;
  }, [events, shown?.proposal_id]);

  // The final checkpoint is saved right after run.summary; re-poll briefly until it lands.
  const detail = usePolling(finished ? () => api.run(runId) : null, (d) => (d?.status === "running" ? 800 : null), [runId, finished]);
  const summary = lastOf(events, "run.summary")?.payload as RunSummaryPayload | undefined;
  const incident = events.length ? obj(events[0].payload.incident) : {};

  return (
    <div className="run-panel" id="run-panel">
      <Card
        title={
          <span className="run-title">
            Run <Mono>{runId}</Mono>
            {incident.scenario_id ? <span className="muted"> · {str(incident.scenario_id)}</span> : null}
          </span>
        }
        subtitle={
          <span className="chip-row">
            {current ? <StateChip state={current} /> : <Chip>starting</Chip>}
            <Chip tone={stream.transport === "sse" ? "accent" : "neutral"} mono>
              {TRANSPORT_LABEL[stream.transport]}
            </Chip>
            {finished ? <Chip>finished</Chip> : live ? <Chip tone="accent">running</Chip> : null}
          </span>
        }
        actions={
          <span className="chip-row">
            {live ? <CancelRunButton runId={runId} /> : null}
            <button type="button" className="btn" onClick={onNewRun}>
              <Icon name="play" size={14} /> New run
            </button>
          </span>
        }
      >
        <StateStepper states={states} running={live} />
        {stream.transport === "recorded" ? (
          <div className="muted small spaced">
            Started by another process — showing recorded events; live updates unavailable here.
          </div>
        ) : null}
        {stream.error ? (
          <div className="spaced">
            <ErrorState error={stream.error} title="Live updates interrupted" />
          </div>
        ) : null}
      </Card>

      <div className="run-grid">
        <Card title="Live event feed" subtitle="Real trace events from the controller, agents and gateway (newest at the bottom)" flush>
          <EventFeed events={events} live={live} height={560} />
        </Card>
        <div className="run-side">
          {showApproval && shown ? (
            <Card tone="warn" title="Approval" subtitle={`proposal ${shown.proposal_id}`}>
              <ApprovalCard
                key={shown.proposal_id}
                request={shown}
                trustedRisk={trustedRisk}
                onDecided={() => {
                  setDecidedHere((prev) => new Set(prev).add(shown.proposal_id));
                  pending.refresh();
                }}
              />
            </Card>
          ) : null}
          {awaiting && (autoChannel || !shown) ? (
            <Notice
              tone="warn"
              icon="key"
              title={autoChannel ? "The simulation auto-approver is deciding…" : "Waiting for the approval request…"}
            >
              {autoChannel
                ? "approval_mode = auto, which the controller allows only against a simulated environment."
                : "Polling GET /approvals/pending for this run."}
            </Notice>
          ) : null}
          {finished ? (
            <ResultPanel detail={detail.data} summary={summary} system={system} runId={runId} />
          ) : (
            <Card title="So far" subtitle="Counted from the trace">
              <LiveFacts events={events} />
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}

// ======================================================================================
// Page
// ======================================================================================

export function LiveRunPage({ route, system }: PageProps) {
  const runId = route.params.get("run");
  const scenarios = usePolling(() => api.scenarios(), null, []);
  const memory = usePolling(() => api.memory(), 15000, []);
  const [selected, setSelected] = useState<string | null>(null);
  const [config, setConfig] = useState<RunConfigInput>(DEFAULT_CONFIG);
  const [showTruth, setShowTruth] = useState(false);
  const [showOthers, setShowOthers] = useState(false);
  const [setupOpen, setSetupOpen] = useState(!runId);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<ApiError | null>(null);

  const sorted = useMemo(
    () =>
      [...(scenarios.data ?? [])].sort(
        (a, b) => (PARTITION_ORDER[a.partition] ?? 9) - (PARTITION_ORDER[b.partition] ?? 9),
      ),
    [scenarios.data],
  );
  const demo = sorted.filter((s) => s.partition === "demo");
  const others = sorted.filter((s) => s.partition !== "demo");
  const scenario = sorted.find((s) => s.id === selected) ?? null;

  const choose = (s: ScenarioInfo) => {
    setSelected(s.id);
    setConfig((c) => withOverrides(c, s));
  };

  // Default: the first demo scenario.
  useEffect(() => {
    if (!selected && sorted.length) choose(sorted[0]);
  }, [sorted]); // eslint-disable-line react-hooks/exhaustive-deps

  // A deep link to a run opens with the setup collapsed.
  useEffect(() => {
    if (runId) setSetupOpen(false);
  }, [runId]);

  async function start() {
    if (!scenario) return;
    setStarting(true);
    setStartError(null);
    try {
      const response = await api.startRun({ scenario_id: scenario.id, config });
      setSetupOpen(false);
      navigate("live", { run: response.run_id });
      window.setTimeout(() => document.getElementById("run-panel")?.scrollIntoView({ behavior: "smooth", block: "start" }), 80);
    } catch (err) {
      setStartError(toApiError(err));
    } finally {
      setStarting(false);
    }
  }

  const visibleScenarios = showOthers ? [...demo, ...others] : demo.length ? demo : sorted;

  return (
    <>
      <PageHeader
        title="Live Incident Run"
        description="Inject a scenario's fault into the simulator and watch RECAP investigate, propose, wait for your approval, mitigate and verify — streamed live from the backend."
      />

      {setupOpen ? (
        <Card
          title="1 · Choose a scenario"
          subtitle="From GET /scenarios (demo scenarios first)"
          actions={<Toggle checked={showTruth} onChange={setShowTruth} label="Show ground truth" hint="hidden from agents" />}
        >
          {scenarios.error && !scenarios.data ? (
            <ErrorState error={scenarios.error} />
          ) : !scenarios.data ? (
            <Loading label="Loading scenarios…" />
          ) : sorted.length === 0 ? (
            <Notice tone="warn" title="The environment reports no scenarios">
              Check configs/environment.yaml and fixtures/scenarios.
            </Notice>
          ) : (
            <>
              <div className="scenario-grid">
                {visibleScenarios.map((s) => (
                  <ScenarioCard
                    key={s.id}
                    scenario={s}
                    selected={s.id === selected}
                    onSelect={() => choose(s)}
                    showTruth={showTruth}
                    missing={missingPrerequisites(s, memory.data)}
                  />
                ))}
              </div>
              {others.length && demo.length ? (
                <button type="button" className="btn btn-ghost btn-small spaced" onClick={() => setShowOthers((v) => !v)}>
                  <Icon name={showOthers ? "chevronDown" : "chevronRight"} size={14} />
                  {showOthers ? "Hide" : "Show"} {others.length} evaluation scenarios (train/test partitions)
                </button>
              ) : null}
            </>
          )}
        </Card>
      ) : null}

      {setupOpen ? (
        <Card title="2 · Configure and run" subtitle="Sent as RunConfig in POST /incidents/run (safety cannot be disabled from the API)">
          <ConfigForm config={config} onChange={setConfig} />
          <div className="run-bar">
            <div className="muted">
              {scenario ? (
                <>
                  Scenario <Mono>{scenario.id}</Mono> · {config.strategy} · RAG {config.use_rag ? "on" : "off"} · memory{" "}
                  {config.use_memory ? "on" : "off"} · {config.approval_mode} approval · {config.tool_latency_ms ?? "default"} ms
                </>
              ) : (
                "Select a scenario."
              )}
            </div>
            <div className="chip-row">
              {runId ? (
                <button type="button" className="btn btn-ghost" onClick={() => setSetupOpen(false)}>
                  Back to current run
                </button>
              ) : null}
              <button type="button" className="btn btn-primary btn-large" disabled={!scenario || starting} onClick={() => void start()}>
                <Icon name="play" size={15} /> {starting ? "Starting…" : "Run incident"}
              </button>
            </div>
          </div>
          {startError ? (
            <div className="spaced">
              <ErrorState error={startError} title="Could not start the run" />
            </div>
          ) : null}
        </Card>
      ) : (
        <div className="setup-collapsed">
          <span className="muted">
            Viewing run <Mono>{runId ?? "—"}</Mono>. Its exact configuration is in the result panel and on the Evidence page.
          </span>
          <button
            type="button"
            className="btn"
            onClick={() => {
              setSetupOpen(true);
              window.scrollTo({ top: 0, behavior: "smooth" });
            }}
          >
            <Icon name="play" size={14} /> Configure a new run
          </button>
        </div>
      )}

      {runId ? (
        <RunPanel
          key={runId}
          runId={runId}
          system={system.data}
          onNewRun={() => {
            setSetupOpen(true);
            window.scrollTo({ top: 0, behavior: "smooth" });
          }}
        />
      ) : null}
    </>
  );
}
