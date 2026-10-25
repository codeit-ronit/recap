import { useEffect, useMemo, useState } from "react";
import { api, toApiError } from "../api";
import type { ApiError } from "../api";
import { ApprovalCard } from "../components/ApprovalCard";
import { Chip, Mono, OriginChip, StateChip, YesNoChip } from "../components/Badge";
import { Card, KeyValues, Notice, PageHeader, Segmented, StatTile, Toggle } from "../components/Card";
import { ErrorState, Loading } from "../components/EmptyState";
import { EventFeed } from "../components/EventFeed";
import { Icon } from "../components/Icon";
import { RunLinks } from "../components/RunPicker";
import { StateStepper } from "../components/StateStepper";
import {
  arr,
  budgetUses,
  costLabel,
  disagreementCounts,
  disagreementLabel,
  fmtArgs,
  fmtInt,
  fmtMs,
  humanize,
  hybridReadiness,
  isTerminal,
  liveModelTokenUse,
  memoryAccessLabel,
  modelTokenUse,
  num,
  obj,
  plural,
  searchModeLabel,
  str,
  stateTone,
  tsMs,
} from "../format";
import { navigate, usePolling, useRunEvents } from "../hooks";
import type { PageProps, RunEventsState } from "../hooks";
import type {
  ApprovalRequest,
  ExperienceEpisode,
  ProposalPayload,
  ProviderInfo,
  RunConfig,
  RunConfigInput,
  RunDetail,
  RunRecord,
  RunSummaryPayload,
  ScenarioInfo,
  SystemSummary,
  TraceEvent,
  Usage,
  VerificationReport,
} from "../types";
import { knowledgeOverlays } from "../view/retrieval";
import {
  checkDetail,
  detailLabel,
  diagnosisCheck,
  isCheckedValue,
  reportLeads,
  ruleIdsOf,
  textAuthor,
  verifiedBeforeM3,
} from "../view/verification";
import "../styles/live-m3.css";

/** How runbooks are searched (RunConfig.retrieval, M3): keywords, or keywords + meaning. */
type SearchMode = NonNullable<RunConfig["retrieval"]>;

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

/**
 * Runbook search (RunConfig.retrieval, M3): keywords (BM25) or keywords + meaning (hybrid).
 * Sent only once the operator picks one; until then the server's default (GET /system) applies.
 */
function SearchField({
  config,
  onChange,
  system,
}: {
  config: RunConfigInput;
  onChange: (config: RunConfigInput) => void;
  system: SystemSummary | undefined;
}) {
  const serverDefault = system?.retrieval_mode; // reported by servers since M3
  const chosen = config.retrieval;
  const shown = chosen ?? serverDefault;
  const readiness = hybridReadiness(system);
  const useDefault = () => {
    const next = { ...config };
    delete next.retrieval;
    onChange(next);
  };
  return (
    <div className="field">
      <span className="field-label">Runbook search</span>
      <Segmented<SearchMode | "">
        label="Runbook search"
        value={shown ?? ""}
        onChange={(v) => onChange({ ...config, retrieval: v || undefined })}
        options={[
          { value: "bm25", label: "Keywords (BM25)" },
          { value: "hybrid", label: "Keywords + meaning (hybrid)" },
        ]}
      />
      <span className="field-hint">
        {shown === "hybrid"
          ? "Adds a meaning ranking from nomic-embed-text on local Ollama to BM25. Needs Ollama running with nomic-embed-text and the stored runbook vectors (scripts/build_vector_index.py); without them the run fails at its first search."
          : shown === "bm25"
            ? "Keyword ranking only: offline and deterministic, no model needed."
            : "GET /system has not reported the server's default search."}
      </span>
      {shown === "hybrid" && readiness !== "unknown" ? (
        <span className="field-hint">
          {readiness === "ready" ? (
            `GET /system: stored vectors cover every runbook chunk${system?.embedder ? ` (${system.embedder})` : ""}.`
          ) : (
            <span className="text-warn">GET /system: the stored runbook vectors are missing or incomplete, so a hybrid run would fail now.</span>
          )}
        </span>
      ) : null}
      <span className="field-hint">
        {chosen
          ? `Sent with the run as retrieval: ${chosen}.`
          : `Not chosen: ${serverDefault ? `the server's default (${searchModeLabel(serverDefault)})` : "the server's default"} applies and nothing is sent.`}
        {config.use_rag ? "" : " RAG is off, so no runbook search runs."}
      </span>
      {chosen ? (
        <button type="button" className="btn btn-ghost btn-small" onClick={useDefault}>
          Use the server default
        </button>
      ) : null}
    </div>
  );
}

function ConfigForm({
  config,
  onChange,
  system,
}: {
  config: RunConfigInput;
  onChange: (config: RunConfigInput) => void;
  system: SystemSummary | undefined;
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
        <Toggle checked={config.use_rag} onChange={(v) => set("use_rag", v)} label="Runbook retrieval (RAG)" hint="runbooks in fixtures/knowledge, cited by chunk id" />
        <Toggle checked={config.use_memory} onChange={(v) => set("use_memory", v)} label="Experience memory" hint="past episodes, checked against live evidence" />
      </div>
      <SearchField config={config} onChange={onChange} system={system} />
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

/** How the run searched the runbooks (RunRecord.retrieval, M3), and any extra runbook folders. */
function SearchFacts({ record }: { record: RunRecord }) {
  if (!record.config.use_rag) return <span className="muted">RAG off: no runbook search ran</span>;
  const info = record.retrieval;
  if (!info) return <span className="muted">not recorded</span>;
  const requests = num(info.embedding_requests);
  const tokens = num(info.embedding_input_tokens);
  const replayed = num(info.replayed_requests);
  const searches = num(info.agent_searches);
  // RECAP_KNOWLEDGE_OVERLAYS: extra runbook folders (e.g. a red-team overlay) were indexed.
  const overlays = arr<string>(obj(record.versions).knowledge_overlays).filter((o) => typeof o === "string");
  return (
    <span className="cell-stack">
      <span className="chip-row">
        <Chip tone={info.mode === "hybrid" ? "info" : "neutral"}>{searchModeLabel(info.mode, true)}</Chip>
        <span className="muted">{record.config.retrieval ? "chosen for this run" : "server default"}</span>
      </span>
      {info.mode === "hybrid" ? (
        <span className="muted small">
          embedder <Mono>{info.embedder ?? "not recorded"}</Mono>
          {requests !== null ? ` · ${plural(requests, "embedding request")}` : ""}
          {tokens !== null ? ` · ${fmtInt(tokens)} input tokens reported by the server` : ""}
          {replayed ? ` · ${fmtInt(replayed)} replayed from the recording` : ""}
        </span>
      ) : null}
      <span className="muted small">
        {searches !== null ? `${plural(searches, "search", "searches")} by the agents (search_runbooks)` : "agent searches not recorded"}
      </span>
      {overlays.length ? (
        <span className="chip-row">
          <Chip tone="warn">
            <Icon name="alert" size={12} /> extra runbook folders indexed
          </Chip>
          {overlays.map((o) => (
            <Mono key={o}>{o}</Mono>
          ))}
        </span>
      ) : null}
    </span>
  );
}

/**
 * The final verification's disagreements between agents (M3): what conflicted, and the rule
 * that settled it; and RECAP's leads for a next round. A run verified before M3 recorded none
 * (the API serves the missing list as []): "not recorded".
 */
function DisagreementFacts({ report, findings }: { report: VerificationReport | null; findings: RunRecord["findings"] }) {
  if (!report) return <span className="muted">no verification ran</span>;
  const counts = verifiedBeforeM3(report, findings) ? null : disagreementCounts(report.disagreements);
  if (!counts) return <span className="muted">not recorded</span>;
  const leads = reportLeads(report);
  return (
    <span className="cell-stack">
      <span>
        {disagreementLabel(counts)} <span className="muted">(final verification)</span>
      </span>
      {arr<NonNullable<VerificationReport["disagreements"]>[number]>(report.disagreements).map((d, i) => {
        const claims = arr<string>(d.claims);
        const agents = arr<string>(d.agents);
        const rules = ruleIdsOf(d.rule_id);
        return (
          <span key={i} className="live-conflict">
            <span className="chip-row">
              <Chip tone={d.resolved ? "ok" : "warn"}>{d.resolved ? (rules.length ? "settled by" : "settled") : "open"}</Chip>
              {d.resolved
                ? rules.map((id) => (
                    <Chip key={id} mono>
                      {id}
                    </Chip>
                  ))
                : null}
              <span className="muted small">
                {humanize(str(d.kind, "disagreement"))}
                {agents.length ? ` · ${agents.join(", ")}` : ""}
              </span>
            </span>
            {claims.length ? <span className="small">{claims.join(" vs ")}</span> : null}
            {d.resolution ? <span className="muted small">{d.resolution}</span> : null}
          </span>
        );
      })}
      {leads.length ? (
        <span className="live-conflict">
          <span className="live-label">RECAP's leads for the next round (its own text)</span>
          {leads.map((lead) => (
            <span key={lead} className="small">
              {lead}
            </span>
          ))}
        </span>
      ) : null}
    </span>
  );
}

/**
 * The diagnosis: RECAP's checked facts (mechanism, service, and the release or dependency the
 * merger keyed it by), then the claim sentence and confidence note quoted as the agent's own
 * untrusted words. The verifier never reads the sentence: only its mechanism, service and detail.
 */
function DiagnosisFacts({ report, provider }: { report: VerificationReport; provider: Partial<ProviderInfo> | null | undefined }) {
  const best = report.best;
  if (!best) return <span className="muted">no supported diagnosis</span>;
  const author = textAuthor(provider);
  const check = diagnosisCheck(report);
  const detail = check ? checkDetail(check) : "";
  return (
    <span className="cell-stack">
      <span className="chip-row">
        <Mono>
          {best.mechanism}@{best.target_service ?? "?"}
        </Mono>
        {detail && isCheckedValue(best.mechanism, detail) ? <span className="muted">{detailLabel(best.mechanism, detail)}</span> : null}
      </span>
      {detail && !isCheckedValue(best.mechanism, detail) ? (
        <span className="live-untrusted">
          <span className="live-untrusted-label">judged as · untrusted {author.who} text</span>
          <q className="live-quote">{detail}</q>
        </span>
      ) : null}
      {best.claim ? (
        <span className="live-untrusted">
          <span className="live-untrusted-label">claim · untrusted {author.who} text</span>
          <q className="live-quote">{best.claim}</q>
        </span>
      ) : null}
      {best.confidence_note ? (
        <span className="live-untrusted">
          <span className="live-untrusted-label">confidence note · {author.words}, not a score</span>
          <q className="live-quote">{best.confidence_note}</q>
        </span>
      ) : null}
    </span>
  );
}

/** The budget as the run's run.summary recorded it: each counter against its limit, real counts only. */
function BudgetFacts({
  budget,
  usage,
  provider,
}: {
  budget: unknown;
  usage: Usage;
  provider: Partial<ProviderInfo> | null | undefined;
}) {
  const uses = budgetUses(budget).filter((b) => b.key !== "model_tokens");
  if (!uses.length) return <span className="muted">not recorded</span>;
  const model = modelTokenUse(budget, usage, provider);
  return (
    <span className="cell-stack">
      <span>{uses.map((b) => b.text).join(" · ")}</span>
      <span>
        model tokens <strong className={model.tone === "warn" ? "text-warn" : undefined}>{model.value}</strong>
        {model.note ? <span className="muted"> · {model.note}</span> : null}
      </span>
    </span>
  );
}

function ResultPanel({
  detail,
  summary,
  system,
  runId,
  memorySkip,
}: {
  detail: RunDetail | undefined;
  summary: RunSummaryPayload | undefined;
  system: SystemSummary | undefined;
  runId: string;
  /** Why the run wrote no episode (its last memory.skip reason, RECAP's own text). */
  memorySkip: string;
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
  const recovery = record.recovery;
  // Where the decisions came from: the run's own record (runs before M2: the live system's).
  const provider = record.provider_info ?? (system && record.provider === system.provider.name ? system.provider : null);
  const u = record.usage;
  const reported = u.actual_input_tokens !== null || u.actual_output_tokens !== null;
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
            record.verification ? (
              <DiagnosisFacts report={record.verification} provider={provider} />
            ) : (
              <span className="muted">no supported diagnosis</span>
            ),
          ],
          ["Disagreements", <DisagreementFacts report={record.verification} findings={record.findings} />],
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
          [
            "Decisions by",
            <span className="chip-row">
              <OriginChip provider={provider} />
              <Mono>{provider?.name ?? record.provider}</Mono>
              {provider?.mode ? <span className="muted">{provider.mode} mode</span> : null}
              {provider?.prompt_version ? <span className="muted">· {provider.prompt_version}</span> : null}
              {provider?.replay_of ? (
                <span className="muted">· replay of {str(provider.replay_of.original_provider)} {str(provider.replay_of.model)}</span>
              ) : null}
            </span>,
          ],
          ["Runbook search", <SearchFacts record={record} />],
          ["Memory partitions", <span>{memoryAccessLabel(record.config, !!record.episode_id)}</span>],
          [
            "Episode written",
            record.episode_id ? (
              <Mono>{record.episode_id}</Mono>
            ) : (
              <span className="muted">not written{memorySkip ? ` — ${memorySkip}` : ""}</span>
            ),
          ],
          [
            "Usage",
            <span>
              {fmtInt(u.steps)} step{u.steps === 1 ? "" : "s"} · {fmtInt(u.tool_calls)} tool call{u.tool_calls === 1 ? "" : "s"} ·{" "}
              {fmtInt(u.estimated_tokens)} tokens{" "}
              <Chip>estimated</Chip>
              {u.model_requests ? (
                <>
                  {" "}· {fmtInt(u.model_requests)} model request{u.model_requests === 1 ? "" : "s"}
                  {reported ? (
                    <>
                      {" "}· in {fmtInt(u.actual_input_tokens)} / out {fmtInt(u.actual_output_tokens)} tokens <Chip tone="ok">reported by the model server</Chip>
                    </>
                  ) : null}
                </>
              ) : null}
            </span>,
          ],
          ["Budget", <BudgetFacts budget={summary?.budget} usage={u} provider={provider} />],
          ["Cost", costLabel(summary?.cost, provider)],
          [
            "Run config",
            <Mono>
              {fmtArgs({
                strategy: record.config.strategy,
                use_rag: record.config.use_rag,
                retrieval: record.config.retrieval,
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

function LiveFacts({
  events,
  provider,
  modelTokenLimit,
}: {
  events: TraceEvent[];
  provider: Partial<ProviderInfo> | undefined;
  modelTokenLimit: number | null;
}) {
  const count = (category: string) => events.filter((e) => e.category === category).length;
  const modes = events.filter((e) => e.category === "routing.decision").map((e) => str(e.payload.mode, "?"));
  const elapsed = events.length > 1 ? tsMs(events[events.length - 1].ts) - tsMs(events[0].ts) : 0;
  const injections = count("safety.injection_flag");
  const denials = events.filter((e) => e.category === "policy.decision" && e.payload.verdict === "DENY").length;
  // M3: how the run searches, the latest merge's disagreements, and reported model tokens.
  const search = events.find((e) => e.category === "retrieval.docs");
  const ragOff = events.some((e) => e.category === "retrieval.skip" && e.payload.stage === "initial");
  const merge = lastOf(events, "verification.merge");
  const conflicts = merge ? disagreementCounts(merge.payload.disagreements) : null;
  const tokens = liveModelTokenUse(events, provider, modelTokenLimit);
  return (
    <div className="stat-grid compact">
      <StatTile label="Elapsed" value={fmtMs(elapsed)} tone="neutral" />
      <StatTile label="Tool calls" value={fmtInt(count("tool.call") + count("mitigation.executed"))} tone="neutral" />
      <StatTile label="Routing" value={modes.length ? modes.join(" → ") : "—"} tone="accent" />
      <StatTile label="Injection flags" value={fmtInt(injections)} tone={injections ? "warn" : "neutral"} />
      <StatTile label="Policy denials" value={fmtInt(denials)} tone={denials ? "bad" : "neutral"} />
      <StatTile label="Runbook search" value={search ? searchModeLabel(search.payload.mode) : ragOff ? "off" : "—"} tone="info" />
      <StatTile
        label="Disagreements"
        value={conflicts ? (conflicts.total ? `${conflicts.total} · ${conflicts.open} open` : "none") : "—"}
        note={merge ? "latest merge" : undefined}
        tone={conflicts?.open ? "warn" : "neutral"}
      />
      <StatTile label="Model tokens" value={tokens.value} note={tokens.note} tone={tokens.tone} />
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
        title="Stops the run at its next step. A fix already being sent cannot be called back: the run then records its outcome, unverified."
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
  // The first event (CREATED) names the provider: the run-origin badge needs nothing else.
  const runProvider = events.length ? (obj(events[0].payload.provider) as Partial<ProviderInfo>) : undefined;
  // The run's model-token limit, from the incident's budget (recorded since M3).
  const modelTokenLimit = num(obj(incident.budget).max_model_tokens);

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
            {runProvider?.name ? <OriginChip provider={runProvider} /> : null}
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
          <EventFeed events={events} live={live} height={560} overlays={knowledgeOverlays(detail.data?.record?.versions)} />
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
            <ResultPanel
              detail={detail.data}
              summary={summary}
              system={system}
              runId={runId}
              memorySkip={str(lastOf(events, "memory.skip")?.payload.reason)}
            />
          ) : (
            <Card title="So far" subtitle="Counted from the trace">
              <LiveFacts events={events} provider={runProvider} modelTokenLimit={modelTokenLimit} />
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
      // `retrieval` is in the body only when the operator chose a search (see SearchField).
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
          <ConfigForm config={config} onChange={setConfig} system={system.data} />
          <div className="run-bar">
            <div className="muted">
              {scenario ? (
                <>
                  Scenario <Mono>{scenario.id}</Mono> · {config.strategy} · RAG {config.use_rag ? "on" : "off"} · search{" "}
                  {config.retrieval ? searchModeLabel(config.retrieval) : "server default"} · memory {config.use_memory ? "on" : "off"} ·{" "}
                  {config.approval_mode} approval · {config.tool_latency_ms ?? "default"} ms
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
