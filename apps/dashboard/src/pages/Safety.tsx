import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api";
import type { Page } from "../api";
import { ApprovalCard, runTargetOf } from "../components/ApprovalCard";
import { Chip, Mono } from "../components/Badge";
import { Card, Notice, PageHeader } from "../components/Card";
import { DataSourceBadge, summarySource } from "../components/DataSourceBadge";
import { FilterBar, applyFilters, filterOptions } from "../components/FilterBar";
import { Icon } from "../components/Icon";
import type { IconName } from "../components/Icon";
import { LogView } from "../components/LogView";
import { Pager, paginate } from "../components/Pager";
import { RunScope } from "../components/RunPicker";
import { EmptyView, PolledView } from "../components/StateViews";
import { StatusToken } from "../components/StatusToken";
import { DataTable } from "../components/Table";
import type * as dto from "../dto";
import { fmtDateTime, fmtInt, isTerminal, riskTone, verdictLabel, verdictTone } from "../format";
import { PAGE_LABELS, hrefFor, usePolling, useRunSelection } from "../hooks";
import type { PageProps, Polled } from "../hooks";
import type { ApprovalEvent, ApprovalRequest } from "../types";
import {
  BLOCK_LAYER_LABELS,
  CALL_SCOPES,
  PROPOSAL_RULES,
  SAFETY_LAYERS,
  SAFETY_STOP_OUTCOMES,
  SAFETY_TESTS,
  actionText,
  actorLabel,
  adapterChecks,
  approvalKind,
  authorWords,
  blockedActions,
  callScope,
  decisionAttestation,
  gatewayRows,
  injectionRows,
  lastFixDecision,
  latestSummary,
  layerStatus,
  permissionLifecycle,
  requestFacts,
  requestedRunId,
  runLayerOutcomes,
  shortHash,
  targetWords,
  traceDecisions,
  trustedRisk,
  unsafeCounts,
  verdictCounts,
  writeRows,
} from "../view/safety";
import type {
  BlockedRow,
  GatewayRow,
  InjectionRow,
  LayerOutcome,
  Lifecycle,
  OutcomeState,
  RequestFacts,
  StepState,
  TargetWords,
  TestedGuarantee,
  TraceDecision,
  UnsafeCounts,
  WriteRow,
} from "../view/safety";
import "../styles/safety.css";

/**
 * Safety & Approvals: the four layers between a model's suggestion and a change to the target
 * (the simulated shop, or a real Kubernetes cluster), the approvals waiting for a person, and
 * for one run what each layer did (policy decisions with rule ids, the evidence gate, the
 * permission lifecycle from the approval log with how each decision came, the signed write and
 * the target's own answer), what was stopped, and its prompt-injection flags. Then the approval
 * audit log across runs, runs that ended in a safety stop, and the rule reference.
 *
 * Digests are shown as a short prefix only; no token, approval id, signed capability or key
 * reaches the page.
 */

export const NOT_RECORDED = "Not recorded";

/** Rows per page in the policy decision table and the approval audit log. */
export const DECISIONS_PER_PAGE = 15;
export const LOG_PER_PAGE = 20;

/** How many recent approval-log entries the page asks for (the server allows up to 500). */
const LOG_LIMIT = 200;

/** How often a pending request's run is asked again while GET /runs/{run_id} fails (ms). */
const RUN_RETRY_MS = 5000;

/** A short "nothing here, and why" line for a card inside the run audit. */
function Quiet({ icon = "info", children }: { icon?: IconName; children: ReactNode }) {
  return (
    <div className="sf-quiet">
      <Icon name={icon} size={15} />
      <span>{children}</span>
    </div>
  );
}

// --- the four layers ------------------------------------------------------------------------------

export function SafetyLayers({ components }: { components: readonly dto.ComponentStatus[] | null | undefined }) {
  return (
    <ol className="sf-layers" aria-label="The four safety layers, in the order a fix meets them">
      {SAFETY_LAYERS.map((layer) => {
        const status = layerStatus(layer.componentId, components);
        return (
          <li key={layer.key} className="sf-layer">
            <div className="sf-layer-head">
              <span className="sf-layer-num" aria-hidden="true">
                {layer.step}
              </span>
              <h3 className="sf-layer-title">{layer.title}</h3>
            </div>
            <div className="sf-layer-status">
              <StatusToken tone={status.tone} size="sm" label={status.label} title={status.detail ?? "GET /system did not report this component"} />
              {status.implementation ? <code className="sf-layer-impl">{status.implementation}</code> : null}
            </div>
            <p className="sf-layer-plain">{layer.plain}</p>
            <div className="sf-layer-rules">{layer.rules}</div>
          </li>
        );
      })}
    </ol>
  );
}

function SafetyLayersCard({ system }: { system: Polled<dto.SystemView> }) {
  return (
    <Card
      title="How a fix is kept safe"
      subtitle="Four independent checks, in the order a proposed fix meets them. None takes model text as an instruction. Status from GET /system."
    >
      <PolledView polled={system} loadingLabel="Loading component status…" errorTitle="Could not load GET /system">
        {(data) => <SafetyLayers components={data.components} />}
      </PolledView>
    </Card>
  );
}

// --- approvals waiting for a person ------------------------------------------------------------------

/**
 * One request's card, given the target of the request's own run (GET /runs/{run_id}). The card
 * combines it with this server's (GET /system), as the server checks the decision: every server
 * on a data folder lists every request in it, so a run on a live cluster can ask a server
 * configured for the simulator, and Approve is one click only when both are simulated.
 */
function PendingCard({ request, onDecided }: { request: ApprovalRequest; onDecided: () => void }) {
  // A run's target never changes: asked until GET /runs/{run_id} answers, then kept.
  const run = usePolling(() => api.runView(request.run_id), (detail) => (detail ? null : RUN_RETRY_MS), [request.run_id]);
  return <ApprovalCard request={request} onDecided={onDecided} run={runTargetOf(run)} />;
}

export function PendingList({ requests, onDecided }: { requests: ApprovalRequest[]; onDecided: () => void }) {
  if (!requests.length) {
    return (
      <div className="sf-pending-empty">
        <Icon name="key" size={16} />
        <span>
          Nothing is waiting for a person. Start a run with manual approval in the {PAGE_LABELS.live}: its exact proposal appears
          here and in the lab.
        </span>
        <a className="btn btn-small" href={hrefFor("live")}>
          <Icon name="play" size={13} /> Open the {PAGE_LABELS.live}
        </a>
      </div>
    );
  }
  return (
    <div className="stack">
      {requests.map((request) => (
        <div key={request.proposal_id} className="sf-pending">
          <div className="sf-pending-meta">
            <span>
              Run{" "}
              <a href={hrefFor("live", { run: request.run_id })}>
                <Mono>{request.run_id}</Mono>
              </a>
            </span>
            <span className="muted">requested {fmtDateTime(request.requested_at)}</span>
            <a href={hrefFor("safety", { run: request.run_id })}>Audit this run</a>
          </div>
          <PendingCard request={request} onDecided={onDecided} />
        </div>
      ))}
    </div>
  );
}

function PendingApprovals({ pending }: { pending: Polled<ApprovalRequest[]> }) {
  const count = pending.data?.length ?? 0;
  return (
    <Card
      title={`Waiting for a person${pending.data ? ` (${count})` : ""}`}
      subtitle="GET /approvals/pending: each request names one exact proposal, and approving it is never general consent. When the request's run or this server acts on a live cluster, approving takes the target service's name, typed. Sign in (top right) to decide."
      tone={count ? "warn" : undefined}
    >
      <PolledView polled={pending} loadingLabel="Loading pending approvals…" errorTitle="Could not load GET /approvals/pending">
        {(list) => <PendingList requests={list} onDecided={pending.refresh} />}
      </PolledView>
    </Card>
  );
}

// --- one run: data -------------------------------------------------------------------------------------

export interface RunSafetyData {
  detail: dto.RunDetail;
  evidence: dto.EvidenceView;
  traceAvailable: boolean;
  /** policy.decision entries, and whether the trace has more than were fetched. */
  policy: dto.TimelineEntry[];
  policyMore: boolean;
  summary: dto.TimelineEntry[];
  flags: dto.TimelineEntry[];
  requests: dto.TimelineEntry[];
  /** approval.decided entries: the run's own record of each decision (a fallback for the log's note). */
  decisions: dto.TimelineEntry[];
  writes: dto.TimelineEntry[];
  /** The approval log of every proposal the run made. */
  approvals: ApprovalEvent[];
}

/** Every proposal id a run made: its evidence view's, then its record's. */
export function proposalIds(detail: dto.RunDetail, evidence: dto.EvidenceView): string[] {
  const ids = [...evidence.proposals.map((p) => p.proposal_id), ...(detail.record?.proposals ?? []).map((p) => p.proposal_id)];
  return [...new Set(ids.filter(Boolean))];
}

async function loadRunSafety(runId: string): Promise<RunSafetyData> {
  const [detail, evidence, policy, summary, flags, requests, decisions, writes] = await Promise.all([
    api.runView(runId),
    api.evidenceView(runId),
    api.timeline(runId, { kind: "policy", limit: 2000 }),
    api.timeline(runId, { kind: "summary", limit: 20 }),
    api.timeline(runId, { kind: "injection_flag", limit: 500 }),
    api.timeline(runId, { kind: "approval_requested", limit: 200 }),
    api.timeline(runId, { kind: "approval_decided", limit: 200 }),
    api.timeline(runId, { kind: "write_executed", limit: 200 }),
  ]);
  const approvals = (await Promise.all(proposalIds(detail, evidence).map((id) => api.approvalEvents(id)))).flat();
  return {
    detail,
    evidence,
    traceAvailable: policy.trace_available,
    policy: policy.entries,
    policyMore: policy.has_more,
    summary: summary.entries,
    flags: flags.entries,
    requests: requests.entries,
    decisions: decisions.entries,
    writes: writes.entries,
    approvals,
  };
}

/** The run's target from its summary (or its record): how the page names it in sentences. */
export function runTargetWords(detail: dto.RunDetail): TargetWords {
  const summary = detail.summary;
  if (summary && typeof summary.environment_simulated === "boolean") {
    return targetWords({ kind: summary.environment_kind, is_simulated: summary.environment_simulated });
  }
  return targetWords(detail.record?.environment ?? null);
}

// --- one run: unsafe actions, layer by layer ------------------------------------------------------------

function count(value: number | null): string {
  return value === null ? NOT_RECORDED : fmtInt(value);
}

export function UnsafeSplit({ counts, live, where = targetWords(null) }: { counts: UnsafeCounts; live: boolean; where?: TargetWords }) {
  if (!counts.summarized) {
    return (
      <Notice tone="neutral" icon="clock" title={live ? "Counted when the run ends" : "Unsafe-action counts: not recorded"}>
        {live
          ? "RECAP counts unsafe attempts and executions in the run summary, which it writes when the run ends."
          : "This run has no run summary, so RECAP never counted its unsafe attempts and executions."}
      </Notice>
    );
  }
  const attemptedTone = counts.attempted === null ? "neutral" : counts.attempted > 0 ? "warn" : "ok";
  const executedTone = counts.executed === null ? "neutral" : counts.executed > 0 ? "bad" : "ok";
  return (
    <div className="sf-unsafe" role="group" aria-label="Unsafe actions: attempted versus executed">
      <div className={`sf-unsafe-tile tone-${attemptedTone}`}>
        <div className="sf-unsafe-label">Unsafe actions attempted</div>
        <div className="sf-unsafe-value">{count(counts.attempted)}</div>
        <div className="sf-unsafe-note">Proposals for a destructive, high-risk or unknown tool.</div>
        <div className="sf-unsafe-sub">
          {count(counts.blocked)} blocked before the environment · {count(counts.refusedByEnvironment)} refused by the environment
        </div>
      </div>
      <div className={`sf-unsafe-tile tone-${executedTone}`}>
        <div className="sf-unsafe-label">Unsafe actions executed</div>
        <div className="sf-unsafe-value">
          {count(counts.executed)}
          {counts.executed !== null ? (
            <StatusToken
              tone={executedTone}
              size="sm"
              label={counts.executed === 0 ? `none reached ${where.short}` : `reached ${where.short}`}
              icon={counts.executed === 0 ? "check" : "x"}
            />
          ) : null}
        </div>
        <div className="sf-unsafe-note">Sent to {where.long} and applied. Should always be 0.</div>
      </div>
      <div className="sf-unsafe-tile tone-neutral">
        <div className="sf-unsafe-label">Fixes sent</div>
        <div className="sf-unsafe-value">{count(counts.fixesSent)}</div>
        <div className="sf-unsafe-note">Writes of any kind that reached the environment after every check.</div>
      </div>
    </div>
  );
}

const OUTCOME_WORDS: Readonly<Record<OutcomeState, { label: string; icon: IconName }>> = {
  passed: { label: "Passed", icon: "check" },
  stopped: { label: "Stopped the fix", icon: "x" },
  waiting: { label: "Waiting", icon: "clock" },
  not_reached: { label: "Not reached", icon: "minus" },
  warning: { label: "Look closer", icon: "alert" },
};

export function LayerOutcomes({ outcomes }: { outcomes: readonly LayerOutcome[] }) {
  return (
    <ol className="sf-outcomes" aria-label="What each safety layer did in this run">
      {outcomes.map((item) => {
        const layer = SAFETY_LAYERS.find((l) => l.key === item.key);
        const words = OUTCOME_WORDS[item.state];
        return (
          <li key={item.key} className={`sf-outcome tone-${item.tone}`}>
            <div className="sf-outcome-layer">
              <span className="sf-layer-num" aria-hidden="true">
                {layer?.step ?? "?"}
              </span>
              {layer?.title ?? item.key}
            </div>
            <StatusToken tone={item.tone} icon={words.icon} size="sm" label={words.label} />
            <div className="sf-outcome-headline">{item.headline}</div>
            {item.lines.map((line, i) => (
              <div key={i} className="sf-outcome-line">
                {line}
              </div>
            ))}
          </li>
        );
      })}
    </ol>
  );
}

// --- one run: permission lifecycle ----------------------------------------------------------------------

const STEP_VIEW: Readonly<Record<StepState, { icon: IconName; words: string }>> = {
  done: { icon: "check", words: "done" },
  failed: { icon: "x", words: "stopped" },
  waiting: { icon: "clock", words: "waiting" },
  skipped: { icon: "minus", words: "did not happen" },
};

export function LifecycleView({
  lifecycle,
  proposal,
  request,
  risk,
  decision = null,
  traced = null,
}: {
  lifecycle: Lifecycle;
  proposal: dto.ProposalView | undefined;
  request: RequestFacts | undefined;
  risk: string | null;
  /** The policy's last decision on this proposal's tool (explains a fix that never reached approval). */
  decision?: GatewayRow | null;
  /** The run's own record of the decision (its trace), for a proposal the approval log does not hold. */
  traced?: TraceDecision | null;
}) {
  const title = proposal ? actionText(proposal.action, proposal.target, proposal.params) : `proposal ${lifecycle.proposalId}`;
  const decided = lifecycle.steps.find((step) => step.key === "decided");
  const attestation = decided && lifecycle.status !== "expired" ? decisionAttestation(decided.actor, decided.note) : null;
  const late = lifecycle.late.length;
  return (
    <section className={`sf-lifecycle tone-${lifecycle.tone}`} aria-label={`${title}: ${lifecycle.label}`}>
      <div className="sf-lifecycle-head">
        <h3 className="sf-lifecycle-title">
          <Mono>{title}</Mono>
        </h3>
        <StatusToken tone={lifecycle.tone} label={lifecycle.label} />
      </div>
      <div className="sf-lifecycle-meta">
        <span>
          proposal <Mono>{lifecycle.proposalId}</Mono>
        </span>
        {proposal ? (
          <span title="The first characters of the SHA-256 that binds the approval to this exact action, target and parameters">
            digest <Mono>{shortHash(proposal.digest)}</Mono>
          </span>
        ) : null}
        <span className="chip-row">
          <Chip tone={riskTone(risk ?? "unknown")} mono title="The registry's risk tier, as the policy recorded it">
            trusted risk: {risk ?? "not recorded"}
          </Chip>
          {proposal ? (
            <Chip mono title="What the proposer declared; the policy never trusts it">
              declared: {proposal.declared_risk || "none"}
            </Chip>
          ) : null}
          {proposal ? (
            <Chip tone={proposal.source === "redteam" ? "bad" : "neutral"} mono title="Where the proposal came from">
              source: {proposal.source}
            </Chip>
          ) : null}
        </span>
        {request?.channel ? <span className="muted">asked via {request.channel}</span> : null}
      </div>
      {lifecycle.status === "not_requested" && decision && decision.verdict === "DENY" ? (
        <div className="sf-lifecycle-why">
          <VerdictToken verdict={decision.verdict} />
          <span>
            The policy denied this fix{decision.deciding ? <> (<Mono>{decision.deciding}</Mono>)</> : null} before any approval could
            be requested: <span className="reason">{decision.reasons.join("; ") || NOT_RECORDED}</span>
          </span>
        </div>
      ) : null}
      {lifecycle.status === "not_requested" && traced ? (
        <div className="sf-lifecycle-why sf-traced">
          <Icon name="info" size={14} />
          <span>
            This server's approval log holds no entry for this proposal, but the run's trace records a decision:{" "}
            <strong>{traced.decision || NOT_RECORDED}</strong>
            {traced.actor ? <> by {actorLabel(traced.actor).label}</> : null}
            {traced.note || traced.reason ? <span className="reason"> ({traced.note || traced.reason})</span> : null}.
          </span>
        </div>
      ) : null}
      <ol className="sf-steps">
        {lifecycle.steps.map((step) => {
          const view = STEP_VIEW[step.state];
          return (
            <li key={step.key} className={`sf-step sf-step-${step.state}`}>
              <span className="sf-step-node" aria-hidden="true">
                <Icon name={view.icon} size={13} />
              </span>
              <div className="sf-step-body">
                <div className="sf-step-title">
                  {step.title}
                  <span className="sr-only">: {view.words}</span>
                </div>
                <div className="sf-step-text">{step.text}</div>
                {step.at ? <div className="sf-step-at">{fmtDateTime(step.at)}</div> : null}
                {step.key === "requested" && request?.expiresAt ? (
                  <div className="sf-step-at">expires {fmtDateTime(request.expiresAt)}</div>
                ) : null}
                {step.note ? (
                  <div className="sf-step-note">
                    <span className="sf-step-note-label">decision log:</span> {step.note}
                  </div>
                ) : null}
              </div>
            </li>
          );
        })}
      </ol>
      {attestation ? (
        <div className="sf-attest" role="note" aria-label="What the decision log proves">
          <Icon name={attestation.channel === "console" ? "key" : "users"} size={14} />
          <span>
            {attestation.shows} <strong>{attestation.limit}</strong>
          </span>
        </div>
      ) : null}
      {lifecycle.status === "expired" ? (
        <div className="sf-expired">
          <StatusToken tone="warn" icon="clock" size="sm" label="Expired: the fix was not applied" />
          <span>
            Nobody decided before the request expired{request?.expiresAt ? ` (${fmtDateTime(request.expiresAt)})` : ""}, so the gateway never received an
            approval and this fix was never sent.
            {late
              ? ` ${late === 1 ? "An answer" : `${late} answers`} reached the decision log after the expiry and ${late === 1 ? "was" : "were"} refused: a late answer changes nothing.`
              : ""}
          </span>
        </div>
      ) : null}
      {lifecycle.refused.length ? (
        <div className="sf-refused">
          <StatusToken tone="warn" icon="alert" size="sm" label={`${lifecycle.refused.length} refused ${lifecycle.refused.length === 1 ? "attempt" : "attempts"}`} />
          <ul className="sf-refused-list">
            {lifecycle.refused.map((event) => (
              <li key={event.event_id}>
                <span className="muted small">{fmtDateTime(event.at)}</span> {actorLabel(event.actor).label}
                {lifecycle.late.includes(event) ? <span className="sf-late"> (a late answer, after the expiry)</span> : null}:{" "}
                <span className="reason">{event.detail || NOT_RECORDED}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {proposal?.rationale ? (
        <details className="sf-rationale">
          <summary>Rationale, as written by {authorWords(proposal.author)}</summary>
          <LogView
            text={proposal.rationale}
            label={`Rationale of ${lifecycle.proposalId}`}
            untrusted={`written by ${authorWords(proposal.author)}`}
            maxHeight={180}
            initialLines={20}
          />
        </details>
      ) : null}
    </section>
  );
}

export function Lifecycles({
  lifecycles,
  proposals,
  requests,
  gateway,
  decisions = new Map(),
}: {
  lifecycles: readonly Lifecycle[];
  proposals: readonly dto.ProposalView[];
  requests: ReadonlyMap<string, RequestFacts>;
  gateway: readonly GatewayRow[];
  /** The trace's approval.decided events, by proposal id. */
  decisions?: ReadonlyMap<string, TraceDecision>;
}) {
  if (!lifecycles.length) return <Quiet icon="key">This run proposed no fix, so nothing needed a decision.</Quiet>;
  return (
    <div className="sf-lifecycles">
      {lifecycles.map((lifecycle) => {
        const proposal = proposals.find((p) => p.proposal_id === lifecycle.proposalId);
        return (
          <LifecycleView
            key={lifecycle.proposalId}
            lifecycle={lifecycle}
            proposal={proposal}
            request={requests.get(lifecycle.proposalId)}
            risk={proposal ? trustedRisk(gateway, proposal.action) : null}
            decision={proposal ? lastFixDecision(gateway, proposal.action) : null}
            traced={decisions.get(lifecycle.proposalId) ?? null}
          />
        );
      })}
    </div>
  );
}

// --- one run: the target's own answer to each write ----------------------------------------------------

export function TargetChecks({ writes, where, refused }: { writes: readonly WriteRow[]; where: TargetWords; refused: number | null }) {
  const checks = adapterChecks(where);
  return (
    <div className="stack">
      {writes.length ? (
        <ul className="sf-writes">
          {writes.map((row) => (
            <li key={row.seq} className="sf-write">
              <div className="sf-write-head">
                <StatusToken
                  tone={row.ok ? "ok" : "bad"}
                  icon={row.ok ? "check" : "x"}
                  size="sm"
                  label={row.ok ? `Applied by ${where.short}` : `Refused or failed at ${where.short}`}
                />
                <Mono>{row.what}</Mono>
                {row.params ? <span className="muted small mono">({row.params})</span> : null}
              </div>
              <div className="sf-write-meta muted small">
                {fmtDateTime(row.ts)} ·{" "}
                {row.digest === NOT_RECORDED ? (
                  "its digest was not recorded"
                ) : (
                  <>
                    bound to digest <Mono>{row.digest}</Mono>
                  </>
                )}
              </div>
              {row.ok ? (
                row.effects.length ? (
                  <ul className="sf-write-report" aria-label="What the target reported">
                    {row.effects.map((effect, i) => (
                      <li key={i}>{effect}</li>
                    ))}
                  </ul>
                ) : (
                  <span className="muted small">The target reported no change details.</span>
                )
              ) : (
                <span className="reason">{row.error || NOT_RECORDED}</span>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <Quiet icon="shield">No write was sent to {where.short} in this run, so it had nothing to check.</Quiet>
      )}
      {refused !== null && refused > 0 ? (
        <div className="muted small">
          {refused} unsafe {refused === 1 ? "write was" : "writes were"} refused by {where.short} itself (the run summary's count).
        </div>
      ) : null}
      {checks ? (
        <p className="sf-adapter-checks">
          <span className="sf-adapter-label">What {where.long} checks before it applies a write (from its adapter's code, not recorded per run):</span>{" "}
          {checks}
        </p>
      ) : null}
    </div>
  );
}

// --- one run: what was stopped ------------------------------------------------------------------------

export function BlockedTable({ rows }: { rows: readonly BlockedRow[] }) {
  return (
    <DataTable
      className="sf-blocked"
      rows={[...rows]}
      rowKey={(row) => row.key}
      empty="Nothing was stopped in this run: no denial, no refused proposal, no rejected or expired approval, no failed write."
      columns={[
        {
          key: "layer",
          header: "Stopped by",
          render: (row) => <StatusToken tone="bad" icon="shield" size="sm" label={BLOCK_LAYER_LABELS[row.layer]} />,
        },
        { key: "what", header: "Action", render: (row) => <Mono>{row.what}</Mono> },
        { key: "result", header: "Result", render: (row) => <strong className="sf-result">{row.result}</strong> },
        { key: "rule", header: "Rule", render: (row) => (row.rule ? <Mono nowrap>{row.rule}</Mono> : <span className="muted">none</span>) },
        { key: "reason", header: "RECAP's reason", render: (row) => <span className="reason">{row.reason}</span> },
        {
          key: "at",
          header: "When",
          nowrap: true,
          render: (row) => (row.at ? fmtDateTime(row.at) : <span className="muted">not recorded</span>),
        },
      ]}
    />
  );
}

// --- one run: every policy decision -------------------------------------------------------------------

function RuleChips({ rules }: { rules: readonly string[] }) {
  return (
    <span className="chip-row">
      {rules.map((id, i) => (
        <Chip key={`${id}-${i}`} mono tone={i === rules.length - 1 ? "accent" : "neutral"} title={i === rules.length - 1 ? "deciding rule" : "evaluated, passed"}>
          {id}
        </Chip>
      ))}
    </span>
  );
}

function VerdictToken({ verdict }: { verdict: string }) {
  const tone = verdictTone(verdict);
  const icon: IconName = tone === "ok" ? "check" : tone === "bad" ? "x" : tone === "warn" ? "key" : "minus";
  return <StatusToken tone={tone} icon={icon} size="sm" label={verdictLabel(verdict || null)} />;
}

const READERS = {
  scope: (row: GatewayRow) => callScope(row),
  verdict: (row: GatewayRow) => row.verdict,
};

export function PolicyDecisions({ rows, more }: { rows: readonly GatewayRow[]; more: boolean }) {
  // Proposed fixes first: they are where the policy says "approval required" or "deny".
  const [values, setValues] = useState<Record<string, string | null>>(() => ({
    scope: rows.some((row) => row.fix) ? "fix" : null,
    verdict: null,
  }));
  const [offset, setOffset] = useState(0);
  const shown = applyFilters(rows, values, READERS);
  const page = paginate(shown, offset, DECISIONS_PER_PAGE);
  const counts = verdictCounts(rows);
  return (
    <>
      <div className="card-pad sf-decisions-bar">
        <FilterBar
          label="Filter policy decisions"
          filters={[
            { key: "scope", label: "Calls", options: filterOptions(rows, READERS.scope, (v) => CALL_SCOPES[v as keyof typeof CALL_SCOPES] ?? v) },
            { key: "verdict", label: "Verdict", options: filterOptions(rows, READERS.verdict, (v) => verdictLabel(v)) },
          ]}
          values={values}
          onChange={(key, value) => {
            setValues((prev) => ({ ...prev, [key]: value }));
            setOffset(0);
          }}
          onClear={() => {
            setValues({ scope: null, verdict: null });
            setOffset(0);
          }}
          summary={`${fmtInt(shown.length)} of ${fmtInt(rows.length)} decisions · ${fmtInt(counts.ALLOW)} allowed, ${fmtInt(counts.APPROVAL_REQUIRED)} approval required, ${fmtInt(counts.DENY)} denied`}
        />
        {more ? <div className="muted small">The trace has more decisions than the first 2,000 shown here.</div> : null}
      </div>
      <DataTable
        className="sf-decisions"
        rows={page}
        rowKey={(row) => String(row.seq)}
        rowClassName={(row) => (row.verdict === "DENY" ? "row-bad" : undefined)}
        empty={
          rows.length
            ? "No decision matches these filters. Clear them to see every call the gateway judged."
            : "No policy decision is recorded for this run yet."
        }
        expand={(row) => (
          <div className="stack">
            <div>
              <h4 className="mini-title">Rules evaluated, in order (the last one decided)</h4>
              {row.rules.length ? <RuleChips rules={row.rules} /> : <span className="muted">not recorded</span>}
            </div>
            <div className="muted small">Recorded {fmtDateTime(row.ts)} · trace event #{row.seq}</div>
          </div>
        )}
        columns={[
          { key: "seq", header: "#", numeric: true, render: (row) => <span className="muted">{row.seq}</span> },
          { key: "verdict", header: "Verdict", render: (row) => <VerdictToken verdict={row.verdict} /> },
          { key: "tool", header: "Tool", render: (row) => <Mono>{row.tool}</Mono> },
          {
            key: "caller",
            header: "Caller",
            render: (row) => (row.caller ? <Mono>{row.caller}</Mono> : <span className="not-recorded">{NOT_RECORDED}</span>),
          },
          {
            key: "risk",
            header: "Trusted risk",
            render: (row) =>
              row.risk ? (
                <Chip tone={riskTone(row.risk)} mono>
                  {row.risk}
                </Chip>
              ) : (
                <span className="muted">not recorded</span>
              ),
          },
          {
            key: "rule",
            header: "Deciding rule",
            render: (row) => (row.deciding ? <Mono nowrap>{row.deciding}</Mono> : <span className="muted">not recorded</span>),
          },
          { key: "reasons", header: "RECAP's reason", render: (row) => <span className="reason">{row.reasons.join("; ") || NOT_RECORDED}</span> },
        ]}
      />
      {shown.length > DECISIONS_PER_PAGE ? (
        <div className="card-pad">
          <Pager offset={offset} limit={DECISIONS_PER_PAGE} total={shown.length} onChange={setOffset} noun="decisions" label="Policy decision pages" />
        </div>
      ) : null}
    </>
  );
}

// --- one run: prompt-injection flags --------------------------------------------------------------------

export function InjectionFlags({ rows, runId }: { rows: readonly InjectionRow[]; runId: string }) {
  if (!rows.length) {
    return (
      <Quiet icon="shield">
        No instruction-like text was flagged in this run's tool output. The check is a short pattern list, not a guarantee: the
        layers above hold either way.
      </Quiet>
    );
  }
  return (
    <div className="stack">
      {rows.map((row) => (
        <div className="sf-flag" key={row.seq}>
          <div className="sf-flag-head">
            <StatusToken tone="warn" icon="alert" size="sm" label="Injection suspected" />
            {row.evidenceId ? (
              <a href={hrefFor("evidence", { run: runId, ev: row.evidenceId })} title="Open this evidence in Evidence & Decisions">
                <Mono>{row.evidenceId}</Mono>
              </a>
            ) : (
              <span className="not-recorded">evidence id not recorded</span>
            )}
            {row.tool ? (
              <span className="muted small">
                from <Mono>{row.tool}</Mono>
              </span>
            ) : null}
            {row.agent ? (
              <span className="muted small">
                read by <Mono>{row.agent}</Mono>
              </span>
            ) : null}
          </div>
          <div className="chip-row">
            <span className="muted small">patterns</span>
            {row.patterns.length ? row.patterns.map((p) => <Chip key={p} mono tone="warn">{p}</Chip>) : <span className="muted small">not recorded</span>}
          </div>
          {row.excerpt ? (
            <LogView text={row.excerpt} label={`Flagged excerpt of ${row.evidenceId || "the tool output"}`} untrusted="tool output" maxHeight={160} initialLines={12} />
          ) : (
            <span className="muted small">The flagged excerpt was not recorded.</span>
          )}
        </div>
      ))}
    </div>
  );
}

// --- approval log tables -----------------------------------------------------------------------------

export function ApprovalLogTable({
  events,
  empty,
  linkRuns,
}: {
  events: readonly ApprovalEvent[];
  empty: string;
  /** Link a "requested" entry to the run it names (the cross-run log). */
  linkRuns: boolean;
}) {
  return (
    <DataTable
      className="sf-log"
      rows={[...events]}
      rowKey={(event) => String(event.event_id)}
      empty={empty}
      columns={[
        { key: "at", header: "When", nowrap: true, render: (event) => fmtDateTime(event.at) },
        {
          key: "kind",
          header: "Entry",
          render: (event) => {
            const kind = approvalKind(event.kind);
            return <StatusToken tone={kind.tone} icon={kind.icon} size="sm" label={kind.label} title={kind.meaning} />;
          },
        },
        {
          key: "proposal",
          header: "Proposal",
          render: (event) => {
            const run = linkRuns ? requestedRunId(event) : null;
            return (
              <span className="cell-stack">
                <Mono nowrap>{event.proposal_id}</Mono>
                {run ? (
                  <a className="small nowrap" href={hrefFor("safety", { run })} title="Audit the run that asked">
                    audit {run}
                  </a>
                ) : null}
              </span>
            );
          },
        },
        {
          key: "who",
          header: "Who",
          render: (event) => {
            const actor = actorLabel(event.actor);
            return (
              <span className="cell-stack">
                <span>{actor.label}</span>
                {event.actor && event.actor !== "recap" ? <Mono>{event.actor}</Mono> : null}
              </span>
            );
          },
        },
        {
          key: "digest",
          header: "Digest",
          nowrap: true,
          render: (event) => <Mono>{shortHash(event.proposal_digest)}</Mono>,
        },
        {
          key: "detail",
          header: "Detail (RECAP's words)",
          render: (event) => (event.detail ? <span className="reason">{event.detail}</span> : <span className="muted">none</span>),
        },
      ]}
    />
  );
}

export function ApprovalAuditLogView({ events }: { events: readonly ApprovalEvent[] }) {
  const [kind, setKind] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const shown = kind ? events.filter((event) => event.kind === kind) : [...events];
  const page = paginate(shown, offset, LOG_PER_PAGE);
  return (
    <>
      <div className="card-pad sf-decisions-bar">
        <FilterBar
          label="Filter the approval log"
          filters={[{ key: "kind", label: "Entry", options: filterOptions(events, (event) => event.kind, (value) => approvalKind(value).label) }]}
          values={{ kind }}
          onChange={(_, value) => {
            setKind(value);
            setOffset(0);
          }}
          onClear={() => {
            setKind(null);
            setOffset(0);
          }}
          summary={`${fmtInt(shown.length)} of the newest ${fmtInt(events.length)} entries`}
        />
      </div>
      <ApprovalLogTable events={page} empty="No entry matches this filter." linkRuns />
      {shown.length > LOG_PER_PAGE ? (
        <div className="card-pad">
          <Pager offset={offset} limit={LOG_PER_PAGE} total={shown.length} onChange={setOffset} noun="entries" label="Approval log pages" />
        </div>
      ) : null}
    </>
  );
}

function ApprovalAuditLog({ log }: { log: Polled<ApprovalEvent[]> }) {
  return (
    <Card
      title="Approval audit log"
      subtitle={`GET /approvals/events: the newest ${LOG_LIMIT} entries across all runs. Append-only: RECAP only adds entries and the database refuses edits and deletions (a guard against bugs, not against someone who can edit the file).`}
      flush
    >
      <PolledView
        polled={log}
        loadingLabel="Loading the approval log…"
        errorTitle="Could not load GET /approvals/events"
        emptyTitle="No approval requested yet"
        emptyIcon="key"
        empty={(events) => (events.length ? null : "A run asks for an approval when it proposes a reversible fix; none has on this server yet.")}
      >
        {(events) => <ApprovalAuditLogView events={events} />}
      </PolledView>
    </Card>
  );
}

// --- runs that ended in a safety stop, and what the tests check -------------------------------------------

export function SafetyStopList({ page }: { page: Page<dto.RunSummary> }) {
  const runs = page.items.filter((run) => Object.prototype.hasOwnProperty.call(SAFETY_STOP_OUTCOMES, run.outcome));
  if (!runs.length) {
    return (
      <Quiet icon="shield">
        No run on this server has ended in a safety stop yet. A red-team scenario in the {PAGE_LABELS.live} (a planner that obeys
        an injected log line) shows the policy stopping a destructive fix.
      </Quiet>
    );
  }
  return (
    <ul className="sf-stops">
      {runs.map((run) => (
        <li key={run.run_id} className="sf-stop">
          <div className="sf-stop-head">
            <a href={hrefFor("safety", { run: run.run_id })} title="Audit this run">
              <Mono>{run.scenario_id}</Mono>
            </a>
            <StatusToken tone="bad" icon="shield" size="sm" label={SAFETY_STOP_OUTCOMES[run.outcome]} />
          </div>
          <DataSourceBadge {...summarySource(run)} size="sm" label={`Data source of ${run.run_id}`} />
          <div className="muted small">
            {fmtDateTime(run.created_at)} · <Mono>{run.run_id}</Mono>
          </div>
        </li>
      ))}
      {page.total !== null && page.total > page.items.length ? (
        <li className="muted small">
          Showing the newest {fmtInt(page.items.length)} of {fmtInt(page.total)} blocked runs.
        </li>
      ) : null}
    </ul>
  );
}

function TestList({ items }: { items: readonly TestedGuarantee[] }) {
  return (
    <ul className="sf-tests">
      {items.map((item) => (
        <li key={`${item.file}::${item.test}`}>
          <span>{item.shows}</span>
          <Mono>{`${item.file}::${item.test}`}</Mono>
        </li>
      ))}
    </ul>
  );
}

export function TestedGuarantees() {
  const every = SAFETY_TESTS.filter((item) => !item.liveTarget);
  const live = SAFETY_TESTS.filter((item) => item.liveTarget);
  return (
    <div className="sf-test-groups">
      <section>
        <h3 className="mini-title">Every target</h3>
        <TestList items={every} />
      </section>
      <section>
        <h3 className="mini-title">Live targets (M5) · checked offline, on a fake cluster or a target marked live</h3>
        <TestList items={live} />
      </section>
    </div>
  );
}

/** Static documentation of the policy rule ids that appear in decisions (not metrics). */
const POLICY_RULES: [string, string][] = [
  ["R1-tool-name-format", "The tool name must be a plain lowercase identifier (blocks look-alike names)."],
  ["R2-known-tool", "The tool must be in RECAP's trusted registry; tools the MCP server advertises are not trusted."],
  ["R3-safety-scope", "Safety may be switched off only in a simulated environment (R0 marks that simulation-only bypass)."],
  ["R4-args-valid", "Strict argument validation: exact types, ranges, no unknown arguments."],
  ["R5-namespace-allowlist", "The environment and any namespace argument must be allowlisted."],
  ["R6-service-allowlist", "Service arguments must be allowlisted services."],
  ["R7-worker-read-only", "Only the controller may use non-read tools; investigators are read-only."],
  ["R8-read-allow", "A plain read-only call is allowed automatically."],
  ["R9-destructive-deny", "Destructive actions are always denied, whatever any model or document says."],
  ["R10-high-risk-deny", "High-risk actions are denied in V1."],
  ["R14-remediation-catalog", "A fix must use a tool from the trusted remediation catalog; a read tool cannot be proposed as a fix."],
  ["R11-reversible-approval", "A reversible write needs a proposal that matches the request, and then an exact approval."],
  ["R12-approval-binding", "The stored approval must match this exact, unexpired, unused proposal (digest-bound, single use)."],
  ["R-agent-tool-allowlist", "A worker may only call the tools listed for its role in configs/agents.yaml (the gateway)."],
  ["R13-conflicting-remediation", "A conflicting fix is refused while another is in progress (the gateway)."],
];

export function PolicyRuleReference() {
  return (
    <div className="stack">
      <details className="sf-reference">
        <summary>Policy rule reference ({POLICY_RULES.length} rule ids)</summary>
        <p className="muted small">
          Documentation, not metrics. Rules run in this order (R14 runs after the risk tiers, before R11) and the first DENY wins, so the
          last id a decision lists is the rule that produced its verdict. Verdicts depend only on trusted inputs: the registry's risk
          tier, validated arguments, the caller, the environment and the stored approval.
        </p>
        <div className="table-wrap">
          <table className="table compact rules">
            <caption className="sr-only">Policy rule ids and what each means</caption>
            <tbody>
              {POLICY_RULES.map(([id, meaning]) => (
                <tr key={id}>
                  <td>
                    <Mono>{id}</Mono>
                  </td>
                  <td>{meaning}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
      <details className="sf-reference">
        <summary>Evidence gate rule reference ({PROPOSAL_RULES.length} rule ids)</summary>
        <p className="muted small">
          Documentation, not metrics. After the policy's verdict and before any approval is requested, the verifier checks every proposed fix
          against all four. A check lists all four ids; a refusal's problems say which failed, and that rule is shown with the refused fix.
        </p>
        <div className="table-wrap">
          <table className="table compact rules">
            <caption className="sr-only">Evidence gate rule ids and what each means</caption>
            <tbody>
              {PROPOSAL_RULES.map((rule) => (
                <tr key={rule.id}>
                  <td>
                    <Mono>{rule.id}</Mono>
                  </td>
                  <td>{rule.meaning}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}

// --- one run: the whole audit ------------------------------------------------------------------------------

/** The run's data source from its summary, with the record's red-team flag (the summary has none). */
function RunSource({ detail }: { detail: dto.RunDetail }) {
  const record = detail.record;
  if (detail.summary) {
    const source = summarySource(detail.summary);
    return (
      <DataSourceBadge
        provider={{ ...source.provider, red_team: record?.provider_info?.red_team === true }}
        environment={source.environment}
        label="This run's data source"
      />
    );
  }
  return <DataSourceBadge provider={record?.provider_info ?? null} environment={record?.environment ?? null} label="This run's data source" />;
}

export function RunSafetyView({ data, runId, live }: { data: RunSafetyData; runId: string; live: boolean }) {
  const record = data.detail.record;
  const gateway = gatewayRows(data.policy);
  const counts = unsafeCounts(latestSummary(data.summary));
  const proposals = data.evidence.proposals;
  const decisions = traceDecisions(data.requests, data.decisions ?? []);
  const lifecycles = proposalIds(data.detail, data.evidence).map((id) => permissionLifecycle(id, data.approvals, live, decisions.get(id) ?? null));
  const outcomes = runLayerOutcomes({ gateway, checks: data.evidence.proposal_checks, lifecycles, writes: data.writes, counts, live, decisions });
  const blocked = blockedActions({ actions: record?.actions ?? [], gateway, checks: data.evidence.proposal_checks, proposals, lifecycles });
  const injections = injectionRows(data.evidence.injection_flags, data.flags);
  const requests = requestFacts(data.requests);
  const writes = writeRows(data.writes);
  const where = runTargetWords(data.detail);
  const safetyOff = record?.config.safety_enabled === false;

  return (
    <div className="stack">
      <div className="sf-run-source">
        <RunSource detail={data.detail} />
        {record ? (
          <span className="muted small">
            approval mode <Mono>{record.config.approval_mode}</Mono>
            {record.config.approval_mode === "auto" ? " (the simulation-only auto-approver decides, no person)" : " (a person decides)"}
          </span>
        ) : null}
      </div>
      {!record ? (
        <EmptyView title="No checkpoint yet" reason="The run has just started; its first safety decisions appear within a few seconds." icon="clock" />
      ) : (
        <>
          {safetyOff ? (
            <Notice tone="bad" icon="alert" title="Safety was switched off for this run (simulation-only ablation)">
              The policy allowed every call with rule R0 and recorded the verdict that would have applied. This is allowed only
              against the simulated shop (a real cluster refuses such a run before it is touched), to show what an unsafe action would do.
            </Notice>
          ) : null}
          {data.traceAvailable ? null : (
            <Notice tone="warn" icon="alert" title="Trace not available">
              This run's stored events are missing: its policy decisions, flags and writes are not recorded. Proposals and approvals
              below come from its record and the approval log.
            </Notice>
          )}
          <Card title="This run, layer by layer" subtitle="What each layer did with this run's fix, from its trace and the approval log. Attempted is not executed.">
            <UnsafeSplit counts={counts} live={live} where={where} />
            <LayerOutcomes outcomes={outcomes} />
          </Card>
          <Card
            title={`Permission lifecycle${lifecycles.length ? ` (${lifecycles.length})` : ""}`}
            subtitle="From the append-only approval log: requested, then decided, then used once, unless an expiry, a rejection or a revocation stopped it. How each decision came is in the log's own words."
          >
            <Lifecycles lifecycles={lifecycles} proposals={proposals} requests={requests} gateway={gateway} decisions={decisions} />
          </Card>
          <Card
            title={`The target's own checks${writes.length ? ` (${writes.length})` : ""}`}
            subtitle="The gateway's approval is not the last lock: the target checks the signed capability itself. Each write RECAP sent, and what the target answered."
          >
            <TargetChecks writes={writes} where={where} refused={counts.refusedByEnvironment} />
          </Card>
          <Card
            title={`What RECAP stopped${blocked.length ? ` (${blocked.length})` : ""}`}
            subtitle="Blocked actions and the layer that stopped each. Reasons are RECAP's own text; one may repeat an id or a target a proposal named."
            flush
          >
            <BlockedTable rows={blocked} />
          </Card>
          <Card
            title={`Policy decisions${gateway.length ? ` (${gateway.length})` : ""}`}
            subtitle="Every call the gateway judged, with the rule ids it evaluated (the last one decided). Proposed fixes are shown first; expand a row for every rule."
            flush
          >
            <PolicyDecisions rows={gateway} more={data.policyMore} />
          </Card>
          <Card
            title={`Prompt-injection flags${injections.length ? ` (${injections.length})` : ""}`}
            subtitle="Instruction-like text in tool output is flagged and kept as untrusted data. A flag never decides anything; the layers above do."
          >
            <InjectionFlags rows={injections} runId={runId} />
          </Card>
          <Card title="Approval log for this run" subtitle="GET /approvals/{proposal_id}/events for each proposal this run made, oldest first." flush>
            <ApprovalLogTable
              events={[...data.approvals].sort((a, b) => a.event_id - b.event_id)}
              empty={
                decisions.size
                  ? "This server's approval log holds no entry for this run: it was decided elsewhere. Its trace records the request and the decision (above)."
                  : "This run asked for no approval."
              }
              linkRuns={false}
            />
          </Card>
        </>
      )}
    </div>
  );
}

function RunSafety({ runId, running }: { runId: string; running: boolean }) {
  const data = usePolling(() => loadRunSafety(runId), (d) => (running || d?.detail.status === "running" ? 3000 : null), [runId]);
  const { refresh } = data;
  // When the run ends, load its final decisions and summary once more.
  const wasRunning = useRef(running);
  useEffect(() => {
    if (wasRunning.current && !running) refresh();
    wasRunning.current = running;
  }, [running, refresh]);
  const live = running || data.data?.detail.status === "running";
  return (
    <PolledView polled={data} loadingLabel="Loading this run's safety record…" errorTitle="Could not load this run's safety record">
      {(d) => <RunSafetyView data={d} runId={runId} live={!!live && !(d.detail.summary?.terminal ?? false)} />}
    </PolledView>
  );
}

// --- the page ----------------------------------------------------------------------------------------------

export function SafetyPage({ route }: PageProps) {
  const selection = useRunSelection(route);
  const system = usePolling(() => api.systemView(), 30000, []);
  const pending = usePolling(() => api.pendingApprovals(), 2000, []);
  const log = usePolling(() => api.recentApprovalEvents(LOG_LIMIT), 5000, []);
  const stops = usePolling(() => api.runsPage({ state: "BLOCKED", limit: 10 }), 15000, []);
  return (
    <>
      <PageHeader
        title="Safety & Approvals"
        description="Four independent checks stand between a model's suggestion and a change to the target: the simulated shop, or a real Kind cluster the owner runs (never production). None of them depends on the model behaving well, and an unsafe action that is attempted is never the same as one that is executed."
      />
      <PendingApprovals pending={pending} />
      <SafetyLayersCard system={system} />
      <div className="section-head">
        <h2 className="section-title">Audit a run</h2>
        <p className="section-lead">What each layer decided for one run, what it stopped, and who approved what.</p>
      </div>
      <RunScope selection={selection} page="safety">
        {(runId, summary) => <RunSafety runId={runId} running={!!summary && !isTerminal(summary.state)} />}
      </RunScope>
      <div className="section-head">
        <h2 className="section-title">Across all runs</h2>
        <p className="section-lead">The approval log, runs a layer stopped, and what the test suite checks.</p>
      </div>
      <ApprovalAuditLog log={log} />
      <div className="grid-2">
        <Card title="Runs that ended in a safety stop" subtitle="GET /runs?state=BLOCKED: a layer or a person stopped the fix. Open one to audit it.">
          <PolledView polled={stops} loadingLabel="Loading blocked runs…" errorTitle="Could not load GET /runs">
            {(page) => <SafetyStopList page={page} />}
          </PolledView>
        </Card>
        <Card
          title="What the test suite checks"
          subtitle="Documentation, not a live result: tests that run in CI with every change (python -m pytest), never against a real cluster."
        >
          <TestedGuarantees />
        </Card>
      </div>
      <Card title="Reference" subtitle="What each rule id in a policy decision or an evidence check means.">
        <PolicyRuleReference />
      </Card>
    </>
  );
}
