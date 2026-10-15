import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { ApprovalCard } from "../components/ApprovalCard";
import { Chip, Mono, VerdictChip } from "../components/Badge";
import { Card, Notice, PageHeader, StatTile, Toggle } from "../components/Card";
import { EmptyState, ErrorState, Loading } from "../components/EmptyState";
import { Icon } from "../components/Icon";
import { RunScope } from "../components/RunPicker";
import { DataTable } from "../components/Table";
import {
  actionResultTone,
  arr,
  fmtArgs,
  fmtDateTime,
  fmtInt,
  fmtMs,
  isTerminal,
  num,
  riskTone,
  str,
  tsMs,
} from "../format";
import { hrefFor, usePolling, useRunSelection } from "../hooks";
import type { PageProps } from "../hooks";
import type { InjectionPayload, PolicyPayload, ProposalPayload, RunSummaryPayload, TraceEvent } from "../types";

/** Static documentation of the policy rule ids that appear in decisions (not metrics). */
const POLICY_RULES: [string, string][] = [
  ["R1-tool-name-format", "The tool name must be a plain lowercase identifier (blocks look-alike names)."],
  ["R2-known-tool", "The tool must be in RECAP's trusted registry; tools the MCP server advertises are not trusted."],
  ["R3-safety-scope / R0", "Safety may be switched off only in a simulated environment; R0 marks that simulation-only bypass."],
  ["R4-args-valid", "Strict argument validation: exact types, ranges, no unknown arguments."],
  ["R5-namespace-allowlist", "The environment and any namespace argument must be allowlisted."],
  ["R6-service-allowlist", "Service arguments must be allowlisted services."],
  ["R7-worker-read-only", "Only the controller may use non-read tools; investigators are read-only."],
  ["R-agent-tool-allowlist", "A worker may only call the tools listed for its role in configs/agents.yaml (enforced by the gateway)."],
  ["R8-read-allow", "Read-only tools are allowed automatically."],
  ["R9-destructive-deny", "Destructive tools are always denied, whatever any model or document says."],
  ["R10-high-risk-deny", "High-risk tools are denied in V1."],
  ["R11-reversible-approval", "A reversible write needs a proposal and an exact human approval."],
  ["R12-approval-binding", "The approval must match this exact, unexpired, unused proposal (digest-bound, single use)."],
  ["R13-conflicting-remediation", "A conflicting remediation is refused while another is in progress."],
  ["R14-remediation-catalog", "A mitigation must use a tool from the trusted remediation catalog; a read tool cannot be proposed as a fix."],
];

function lastRule(ruleIds: string[]): string | null {
  return ruleIds.length ? ruleIds[ruleIds.length - 1] : null;
}

function RuleIds({ ids }: { ids: string[] }) {
  const deciding = lastRule(ids);
  return (
    <span className="chip-row">
      {ids.map((id, i) => (
        <Chip key={`${id}-${i}`} mono tone={id === deciding && i === ids.length - 1 ? "accent" : "neutral"} title={i === ids.length - 1 ? "deciding rule" : "evaluated"}>
          {id}
        </Chip>
      ))}
    </span>
  );
}

function PendingApprovals() {
  const pending = usePolling(() => api.pendingApprovals(), 2000, []);
  const list = pending.data ?? [];
  return (
    <Card
      title={`Pending approvals${pending.data ? ` (${list.length})` : ""}`}
      subtitle="GET /approvals/pending — each request names one exact proposal; approving it is never general consent"
      tone={list.length ? "warn" : undefined}
    >
      {pending.error && !pending.data ? (
        <ErrorState error={pending.error} />
      ) : !pending.data ? (
        <Loading />
      ) : list.length === 0 ? (
        <EmptyState title="Nothing is waiting for a human" icon="key">
          Start a run with manual approval on Live Incident Run; its proposal will appear here and on the live page.
        </EmptyState>
      ) : (
        <div className="stack">
          {list.map((request) => (
            <div key={request.proposal_id}>
              <div className="muted small spaced-bottom">
                run <a href={hrefFor("live", { run: request.run_id })}><Mono>{request.run_id}</Mono></a> · requested{" "}
                {fmtDateTime(request.requested_at)}
              </div>
              <ApprovalCard request={request} onDecided={() => pending.refresh()} />
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function SafetyView({ runId, running }: { runId: string; running: boolean }) {
  const detail = usePolling(() => api.run(runId), (d) => (d?.status === "running" || running ? 2500 : null), [runId]);
  const events = usePolling(
    () => api.events(runId),
    (list) => (list?.some((e) => e.category === "run.summary") || !running ? null : 2500),
    [runId],
  );
  const { refresh: refreshEvents } = events;
  useEffect(() => {
    if (running) refreshEvents();
  }, [running, refreshEvents]);
  const [showReads, setShowReads] = useState(false);
  const trace = useMemo(() => events.data ?? [], [events.data]);

  // Trusted risk of each proposal, from the mitigation.proposed event (registry tier).
  const trustedRisk = useMemo(() => {
    const map = new Map<string, string>();
    for (const e of trace) {
      if (e.category !== "mitigation.proposed") continue;
      const p = e.payload as ProposalPayload;
      if (p.proposal?.proposal_id) map.set(p.proposal.proposal_id, str(p.trusted_risk, "unknown"));
    }
    return map;
  }, [trace]);

  if (detail.error && !detail.data) return <ErrorState error={detail.error} />;
  if (!detail.data) return <Loading />;
  const record = detail.data.record;
  if (!record) return <EmptyState title="No checkpoint yet" icon="clock" />;

  const t0 = trace.length ? tsMs(trace[0].ts) : 0;
  const gateway = trace.filter((e) => e.category === "policy.decision");
  const shownGateway = showReads
    ? gateway
    : gateway.filter((e) => !(e.payload.verdict === "ALLOW" && str((e.payload as PolicyPayload).caller ?? e.agent) !== "controller"));
  const injections = trace.filter((e) => e.category === "safety.injection_flag");
  const summary = trace.find((e) => e.category === "run.summary")?.payload as RunSummaryPayload | undefined;
  const denied = record.safety_decisions.filter((d) => d.verdict === "DENY").length + gateway.filter((e) => e.payload.verdict === "DENY" && str((e.payload as PolicyPayload).caller ?? e.agent) !== "controller").length;

  return (
    <>
      <div className="stat-grid">
        <StatTile label="Gateway decisions" value={fmtInt(gateway.length)} note="policy.decision events (reads included)" />
        <StatTile label="Denied" value={fmtInt(denied)} tone={denied ? "bad" : "neutral"} />
        <StatTile label="Approvals" value={fmtInt(record.approvals.length)} tone={record.approvals.length ? "ok" : "neutral"} />
        <StatTile label="Injection flags" value={fmtInt(injections.length)} tone={injections.length ? "warn" : "neutral"} />
        <StatTile
          label="Unsafe actions"
          value={summary ? `${fmtInt(num(summary.unsafe_executed))} executed` : "—"}
          tone={summary && num(summary.unsafe_executed) ? "bad" : summary ? "ok" : "neutral"}
          note={summary ? `${fmtInt(num(summary.unsafe_attempts))} attempted · ${fmtInt(num(summary.unsafe_blocked))} blocked` : "run not finished"}
        />
      </div>

      <Card title="Mitigation policy decisions" subtitle="record.safety_decisions — trusted policy verdicts on proposed and executed mitigations" flush>
        <DataTable
          rows={record.safety_decisions}
          rowKey={(_, i) => `sd${i}`}
          rowClassName={(d) => (d.verdict === "DENY" ? "row-bad" : undefined)}
          empty="No mitigation reached the policy in this run."
          columns={[
            { key: "verdict", header: "Verdict", render: (d) => <VerdictChip verdict={d.verdict} /> },
            { key: "tool", header: "Tool", render: (d) => <Mono>{d.tool_name}</Mono> },
            { key: "risk", header: "Trusted risk", render: (d) => <Chip tone={riskTone(d.risk ?? "unknown")} mono>{d.risk ?? "unknown"}</Chip> },
            { key: "caller", header: "Caller", render: (d) => <Mono>{d.caller ?? "—"}</Mono> },
            { key: "rules", header: "Rule ids (last = deciding)", render: (d) => <RuleIds ids={d.rule_ids} /> },
            { key: "reasons", header: "Reasons", render: (d) => <span className="reason">{d.reasons.join("; ") || "—"}</span> },
            { key: "version", header: "Policy", nowrap: true, render: (d) => <Mono>{d.policy_version || "—"}</Mono> },
          ]}
        />
      </Card>

      <Card
        title="Gateway decisions in the trace"
        subtitle="Every tool call is judged by the gateway before it reaches the MCP server"
        actions={<Toggle checked={showReads} onChange={setShowReads} label="Include routine read ALLOWs" />}
        flush
      >
        <DataTable
          rows={shownGateway}
          rowKey={(e) => String(e.seq)}
          rowClassName={(e) => (e.payload.verdict === "DENY" ? "row-bad" : undefined)}
          empty={gateway.length ? "Only routine read ALLOWs — toggle them on to see them." : "No policy decisions recorded yet."}
          columns={[
            { key: "t", header: "At", numeric: true, nowrap: true, render: (e) => `+${fmtMs(tsMs(e.ts) - t0)}` },
            { key: "verdict", header: "Verdict", render: (e) => <VerdictChip verdict={str(e.payload.verdict)} /> },
            { key: "tool", header: "Tool", render: (e) => <Mono>{str(e.payload.tool, "?")}</Mono> },
            { key: "caller", header: "Caller", render: (e) => <Mono>{str((e.payload as PolicyPayload).caller ?? e.agent, "controller")}</Mono> },
            { key: "rule", header: "Deciding rule", render: (e) => <Mono>{lastRule(arr<string>(e.payload.rule_ids)) ?? "—"}</Mono> },
            { key: "reasons", header: "Reasons", render: (e) => <span className="reason">{arr<string>(e.payload.reasons).join("; ") || "—"}</span> },
          ]}
        />
      </Card>

      <div className="grid-2">
        <Card title="Proposals" subtitle="record.proposals — the declared risk is NOT trusted; the registry tier is" flush>
          <DataTable
            rows={record.proposals}
            rowKey={(p) => p.proposal_id}
            empty="No mitigation was proposed."
            expand={(p) => (
              <div className="stack">
                <div>
                  <span className="muted">Rationale:</span> {p.rationale || "—"}
                </div>
                <div className="chip-row">
                  <span className="muted">Cited evidence:</span>
                  {p.evidence_ids.length ? p.evidence_ids.map((id) => <a key={id} className="chip tone-neutral mono" href={hrefFor("evidence", { run: runId, ev: id })}>{id}</a>) : <span className="muted">none</span>}
                </div>
                <div className="chip-row">
                  <span className="muted">Citations:</span>
                  {p.citations.length ? p.citations.map((c) => <Chip key={c} mono>{c}</Chip>) : <span className="muted">none</span>}
                </div>
              </div>
            )}
            columns={[
              {
                key: "action",
                header: "Action",
                render: (p) => <Mono>{`${p.action} on ${p.target}${Object.keys(p.params).length ? ` (${fmtArgs(p.params)})` : ""}`}</Mono>,
              },
              { key: "source", header: "Source", render: (p) => <Chip tone={p.source === "redteam" ? "bad" : "accent"} mono>{p.source}</Chip> },
              {
                key: "risk",
                header: "Risk",
                render: (p) => {
                  const trusted = trustedRisk.get(p.proposal_id) ?? "unknown";
                  return (
                    <span className="chip-row">
                      <Chip tone={riskTone(trusted)} mono title="trusted registry tier">trusted: {trusted}</Chip>
                      <Chip mono title="declared by the proposer">declared: {p.risk_level}</Chip>
                    </span>
                  );
                },
              },
            ]}
          />
        </Card>

        <Card title="Approvals" subtitle="record.approvals — bound to one proposal digest, expiring, single use" flush>
          <DataTable
            rows={record.approvals}
            rowKey={(a) => a.approval_id}
            rowClassName={(a) => (a.decision === "rejected" ? "row-bad" : undefined)}
            empty="No approval was recorded for this run."
            columns={[
              { key: "decision", header: "Decision", render: (a) => <Chip tone={a.decision === "approved" ? "ok" : "bad"}>{a.decision}</Chip> },
              { key: "actor", header: "Actor", render: (a) => a.actor },
              { key: "id", header: "Approval", nowrap: true, render: (a) => <Mono>{a.approval_id}</Mono> },
              { key: "digest", header: "Digest", render: (a) => <Mono title={a.proposal_digest}>{a.proposal_digest.slice(0, 12)}…</Mono> },
              {
                key: "consumed",
                header: "Use",
                render: (a) =>
                  a.consumed_at ? (
                    <Chip tone="ok" title={a.consumed_at}>
                      <Icon name="lock" size={12} /> single use, consumed {fmtDateTime(a.consumed_at)}
                    </Chip>
                  ) : (
                    <Chip>not consumed · expires {fmtDateTime(a.expires_at)}</Chip>
                  ),
              },
            ]}
          />
        </Card>
      </div>

      <Card title="Mitigation attempts" subtitle="record.actions — denied, rejected and expired actions are shown in red" flush>
        <DataTable
          rows={record.actions}
          rowKey={(_, i) => `a${i}`}
          rowClassName={(a) => (["denied", "rejected", "expired", "error"].includes(a.result) ? "row-bad" : undefined)}
          empty="No mitigation was attempted."
          columns={[
            { key: "action", header: "Action", render: (a) => <Mono>{`${a.action} on ${a.target}${Object.keys(a.params).length ? ` (${fmtArgs(a.params)})` : ""}`}</Mono> },
            { key: "result", header: "Result", render: (a) => <Chip tone={actionResultTone(a.result)}>{a.result}</Chip> },
            { key: "note", header: "Note", render: (a) => <span className="reason">{a.note || "—"}</span> },
            { key: "at", header: "At", render: (a) => fmtDateTime(a.at) },
          ]}
        />
      </Card>

      <Card title="Prompt-injection flags" subtitle="safety.injection_flag events — suspicious text is labeled and kept as untrusted data; the gateway still decides">
        {injections.length === 0 ? (
          <span className="muted">No injection-like text was flagged in this run.</span>
        ) : (
          <div className="stack">
            {injections.map((e: TraceEvent) => {
              const p = e.payload as InjectionPayload;
              return (
                <div className="injection" key={e.seq}>
                  <div className="chip-row">
                    <Chip tone="warn">
                      <Icon name="alert" size={12} /> injection suspected
                    </Chip>
                    <a href={hrefFor("evidence", { run: runId, ev: str(p.evidence_id) })}>
                      <Mono>{str(p.evidence_id, "?")}</Mono>
                    </a>
                    {p.tool ? <Mono>{p.tool}</Mono> : null}
                    {arr<string>(p.patterns).map((pat) => (
                      <Chip key={pat} mono>
                        {pat}
                      </Chip>
                    ))}
                  </div>
                  {p.excerpt ? <pre className="raw excerpt">{p.excerpt}</pre> : null}
                </div>
              );
            })}
          </div>
        )}
      </Card>
    </>
  );
}

export function SafetyPage({ route }: PageProps) {
  const selection = useRunSelection(route);
  return (
    <>
      <PageHeader
        title="Safety & Approvals"
        description="Every tool call passes a trusted policy at the gateway. Reads are allowed, reversible writes need an exact approval, destructive actions are always denied — no matter what a model, log or runbook says."
      />
      <PendingApprovals />
      <div className="section-head">
        <h2 className="section-title">Audit a run</h2>
        <span className="muted">Verdicts, proposals, approvals and injection flags recorded for one run</span>
      </div>
      <RunScope selection={selection} page="safety">
        {(runId, summary) => <SafetyView runId={runId} running={!!summary && !isTerminal(summary.state)} />}
      </RunScope>
      <Card title="Policy rule reference" subtitle="Documentation, not metrics: what each rule id in a decision means. Rules run in order; the last id listed is the one that produced the verdict.">
        <Notice tone="info" icon="shield" title="Trusted inputs only">
          Verdicts depend on the registry risk tier, validated arguments, the caller, the environment and the stored approval —
          never on model text, a proposal's declared risk, tool descriptions or MCP server annotations.
        </Notice>
        <table className="table compact rules">
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
      </Card>
    </>
  );
}
