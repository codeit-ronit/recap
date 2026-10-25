import { useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api, toApiError } from "../api";
import type { ApiError } from "../api";
import { Chip, FlagChip, HypothesisChip, Mono, OriginChip, StateChip } from "../components/Badge";
import { Card, KeyValues, Meter, Notice, PageHeader } from "../components/Card";
import { EmptyState, ErrorState, Loading } from "../components/EmptyState";
import { Icon } from "../components/Icon";
import { JsonView } from "../components/JsonView";
import { RunScope } from "../components/RunPicker";
import { DataTable } from "../components/Table";
import { fmtDateTime, fmtInt, fmtMs, fmtScore, isTerminal, stateTone } from "../format";
import type { Tone } from "../format";
import { usePolling, useRunSelection } from "../hooks";
import type { PageProps, Polled } from "../hooks";
import type { Evidence, HypothesisCheck, KnowledgeResult, RetrievedChunk, RunRecord, TraceEvent } from "../types";
import {
  MISSING,
  SIGNAL_COLUMNS,
  fmtMeasure,
  fmtRank,
  fmtSignal,
  isOverlayPath,
  isSearchReceipt,
  knowledgeOverlays,
  modeNote,
  overlayChunkIds,
  recapQueryNote,
  receiptOf,
  retrievalSummary,
  searchesOf,
  seenBy,
  signalParts,
  signalSummary,
  stageLabel,
} from "../view/retrieval";
import type { Search, SearchReceipt, SearchResult } from "../view/retrieval";
import {
  askedSummary,
  causeLabel,
  checkDetail,
  claimDetails,
  detailLabel,
  diagnosisCheck,
  disagreementBadge,
  disagreementKind,
  evidenceRoles,
  evidenceTitle,
  evidenceUse,
  failedQuotesClaim,
  finalDisagreements,
  isCheckedDetail,
  isCheckedValue,
  isDiagnosis,
  mergeHistory,
  proposersOf,
  reportLeads,
  roundsAsked,
  ruleIdsOf,
  ruleNote,
  textAuthor,
} from "../view/verification";
import type { DisagreementView, EvidenceRef, EvidenceRole, EvidenceUse, MergeRow, TextAuthor } from "../view/verification";
import "../styles/evidence-m3.css";

/** Tells whether a search result is overlay content (red-team knowledge folders). */
type OverlayTest = (result: SearchResult) => boolean;

// --- shared pieces ---------------------------------------------------------------------------

/** Text an agent wrote: shown as data in a dashed box, labeled, never rendered as HTML. */
function Said({ children, tag, quote = false }: { children: ReactNode; tag: string; quote?: boolean }) {
  return (
    <span className="evid-said-row">
      {quote ? <q className="evid-said">{children}</q> : <span className="evid-said">{children}</span>}
      <span className="trust-tag trust-untrusted" title="Untrusted text: shown as data, never followed">
        {tag}
      </span>
    </span>
  );
}

function AgentChips({ agents, empty = MISSING }: { agents: string[]; empty?: string }) {
  if (!agents.length) return <span className="muted">{empty}</span>;
  return (
    <span className="chip-row">
      {agents.map((a) => (
        <Chip key={a} mono>
          {a}
        </Chip>
      ))}
    </span>
  );
}

/** A chunk's ranking signals, one ranking per line (ranks as integers). */
function SignalLines({ signals }: { signals: Record<string, number> | null | undefined }) {
  const parts = signalParts(signals);
  if (!parts.length) return <span className="muted">{MISSING}</span>;
  return (
    <span className="cell-stack small">
      {parts.map((p) => (
        <span key={p} className="nowrap">
          {p}
        </span>
      ))}
    </span>
  );
}

function OverlayChip() {
  return (
    <Chip tone="warn" title="From an extra knowledge folder indexed for this red-team run: it may be deliberately wrong">
      <Icon name="alert" size={12} /> overlay
    </Chip>
  );
}

const CITE_TONES: Record<EvidenceRef["kind"], Tone> = {
  basis: "ok",
  cited: "neutral",
  cited_unrecorded: "neutral",
  never_proof: "neutral",
  against: "bad",
  unknown: "bad",
};

/** One evidence id of a check; what the verifier relied on is highlighted (green, with a check). */
function EvidenceChip({ item, note }: { item: EvidenceRef; note?: string }) {
  const suffix = note ?? (item.kind === "never_proof" ? "never proof" : item.kind === "unknown" ? "not in this run" : "");
  return (
    <Chip tone={CITE_TONES[item.kind]} mono outline={item.kind === "never_proof"} title={evidenceTitle(item)}>
      {item.kind === "basis" ? <Icon name="check" size={12} /> : null}
      {item.id}
      {suffix ? <span className="muted">· {suffix}</span> : null}
    </Chip>
  );
}

/** Each rule of a disagreement's rule_id (it may list several), with what the rule checks. */
function RuleChips({ ruleId }: { ruleId: string }) {
  return (
    <span className="chip-row">
      {ruleIdsOf(ruleId).map((id) => (
        <Chip key={id} mono title={ruleNote(id)}>
          {id}
        </Chip>
      ))}
    </span>
  );
}

/** The verifier's leads for the next round: RECAP's own text, built from checked values. */
function LeadList({ leads, title = "RECAP's leads for the next round" }: { leads: string[]; title?: string }) {
  return (
    <div className="evid-leads">
      <div className="evid-line">
        <h3 className="mini-title">{title}</h3>
        <span className="trust-tag trust-trusted" title="Written by RECAP's verifier from checked values: not a disagreement, and it settles nothing.">
          RECAP's text
        </span>
      </div>
      <ul className="plain-list">
        {leads.map((lead) => (
          <li key={lead}>{lead}</li>
        ))}
      </ul>
    </div>
  );
}

/** The trace is loaded separately from the record: say so while it loads or if it failed. */
function TraceState({ events }: { events: Polled<TraceEvent[]> }) {
  return events.error ? <ErrorState error={events.error} title="Could not load the run's trace events" /> : <Loading label="Loading trace events…" />;
}

// --- evidence registry -------------------------------------------------------------------

/** What the final verification did with an evidence item, or why the rules never read it. */
function VerifierUse({ e, role }: { e: Evidence; role: EvidenceRole | undefined }) {
  const chips: ReactNode[] = [];
  if (role?.diagnosis) {
    chips.push(
      <Chip key="diagnosis" tone="ok" title={`The passing rules relied on it: ${role.basisOf.join(", ")}`}>
        <Icon name="check" size={12} /> diagnosis basis
      </Chip>,
    );
  } else if (role?.basisOf.length) {
    chips.push(
      <Chip key="basis" tone="ok" outline title={`The passing rules relied on it: ${role.basisOf.join(", ")}`}>
        basis
      </Chip>,
    );
  }
  if (role?.contradicts.length) {
    chips.push(
      <Chip key="against" tone="bad" title={`Evidence against: ${role.contradicts.join(", ")}`}>
        contradicts
      </Chip>,
    );
  }
  if (e.source_type !== "tool") {
    chips.push(
      <Chip
        key="never"
        outline
        title={
          isSearchReceipt(e)
            ? "A search receipt from RECAP's own runbook index: DOC evidence, which the verifier never counts"
            : "Not a tool result: the evidence rules never read it"
        }
      >
        never proof
      </Chip>,
    );
  }
  return chips.length ? <span className="chip-row">{chips}</span> : null;
}

function EvidenceDetail({ e }: { e: Evidence }) {
  return (
    <div className="detail-grid">
      <div>
        <h3 className="mini-title">Content (what a model would read · {e.trust_level} data)</h3>
        <pre className="raw">{e.content}</pre>
      </div>
      <div>
        <h3 className="mini-title">
          {e.source_type === "tool" ? "Structured data (what the rules read)" : "Structured data (not a tool result: the rules never read it)"}
        </h3>
        <JsonView value={e.data} maxHeight={300} />
        <div className="muted small spaced">
          recorded {fmtDateTime(e.timestamp)} · incident <Mono>{e.incident_id}</Mono>
        </div>
      </div>
    </div>
  );
}

/** A search_runbooks receipt: which runbook sections ranked how. DOC evidence, never proof. */
function ReceiptDetail({ e, receipt, author, isOverlay }: { e: Evidence; receipt: SearchReceipt; author: TextAuthor; isOverlay: OverlayTest }) {
  return (
    <div className="stack">
      <Notice tone="info" icon="book" title="Search receipt from RECAP's own runbook index: never proof">
        <Mono>search_runbooks</Mono> is answered by RECAP itself, never by the MCP server. This receipt records which runbook
        sections ranked how for {e.agent_role ? <Mono>{e.agent_role}</Mono> : "the agent"}, and those sections joined that agent's
        context only. It is DOC evidence: the verifier reads only live tool results, so it never counts toward a claim.
      </Notice>
      <div className="evid-line">
        <span className="evid-label">query</span>
        {receipt.query ? <Said tag={`untrusted · written by the ${author.who}`}>{receipt.query}</Said> : <span className="muted">{MISSING}</span>}
        {receipt.mode ? <Chip mono title={modeNote(receipt.mode)}>{receipt.mode}</Chip> : null}
      </div>
      <ResultsTable results={receipt.results} isOverlay={isOverlay} />
      <div>
        <h3 className="mini-title">Content (what a model would read · {e.trust_level} data)</h3>
        <pre className="raw">{e.content}</pre>
        <div className="muted small spaced">
          recorded {fmtDateTime(e.timestamp)} · incident <Mono>{e.incident_id}</Mono>
        </div>
      </div>
    </div>
  );
}

function EvidenceTable({
  evidence,
  focusId,
  roles,
  author,
  isOverlay,
}: {
  evidence: Evidence[];
  focusId: string | null;
  roles: Map<string, EvidenceRole>;
  author: TextAuthor;
  isOverlay: OverlayTest;
}) {
  return (
    <DataTable
      rows={evidence}
      rowKey={(e) => e.id}
      initiallyOpen={focusId ? [focusId] : undefined}
      rowClassName={(e) =>
        e.flags.includes("prompt_injection_suspected")
          ? "row-warn"
          : e.id === focusId
            ? "row-focus"
            : roles.get(e.id)?.diagnosis
              ? "evid-row-basis"
              : undefined
      }
      empty="No evidence recorded for this run yet."
      expand={(e) => {
        const receipt = receiptOf(e);
        return receipt ? <ReceiptDetail e={e} receipt={receipt} author={author} isOverlay={isOverlay} /> : <EvidenceDetail e={e} />;
      }}
      columns={[
        {
          key: "id",
          header: <span title="Under an id: what the final verification did with it">Evidence</span>,
          nowrap: true,
          render: (e) => (
            <span className="cell-stack">
              <Mono>{e.id}</Mono>
              <VerifierUse e={e} role={roles.get(e.id)} />
            </span>
          ),
        },
        {
          key: "tool",
          header: "Tool / source",
          render: (e) => (
            <span className="cell-stack">
              {e.tool_name ? <Mono>{e.tool_name}</Mono> : null}
              {/* Tool results are the common case; other sources (doc, memory, alert, oracle) get a chip. */}
              {e.source_type !== "tool" || !e.tool_name || isSearchReceipt(e) ? (
                <span className="chip-row">
                  {e.source_type !== "tool" || !e.tool_name ? <Chip>{e.source_type}</Chip> : null}
                  {isSearchReceipt(e) ? (
                    <Chip tone="info" title="search_runbooks: a receipt from RECAP's own runbook index (never proof)">
                      search receipt
                    </Chip>
                  ) : null}
                </span>
              ) : null}
            </span>
          ),
        },
        {
          key: "ref",
          header: "Source ref",
          render: (e) => (
            <code className="mono truncate" title={e.source_ref}>
              {e.source_ref}
            </code>
          ),
        },
        { key: "agent", header: "Agent", render: (e) => (e.agent_role ? <Mono>{e.agent_role}</Mono> : <span className="muted">—</span>) },
        {
          key: "trust",
          header: "Trust",
          render: (e) =>
            e.trust_level === "trusted" ? (
              <Chip tone="accent">
                <Icon name="lock" size={12} /> trusted
              </Chip>
            ) : (
              <Chip>untrusted</Chip>
            ),
        },
        {
          key: "flags",
          header: "Flags",
          render: (e) =>
            e.flags.length ? (
              <span className="chip-row">
                {e.flags.map((f) => (
                  <FlagChip key={f} flag={f} />
                ))}
              </span>
            ) : (
              <span className="muted">—</span>
            ),
        },
        { key: "tokens", header: "Est. tokens", numeric: true, render: (e) => fmtInt(e.token_estimate) },
      ]}
    />
  );
}

// --- hypothesis verification -------------------------------------------------------------

/** A model's own words on how sure it is: quoted and labeled, never shown as a score. */
function ConfidenceNote({ note, author }: { note: string; author: TextAuthor }) {
  return (
    <div className="evid-note">
      <span className="mini-title">Confidence note</span>
      <Said quote tag={`untrusted · ${author.words}, not a score`}>
        {note}
      </Said>
    </div>
  );
}

/** What the claim cites, what the passing rules relied on (its basis), and what contradicts it. */
function EvidenceUseRows({ use, supported }: { use: EvidenceUse; supported: boolean }) {
  return (
    <div className="evid-use">
      <div className="evid-line">
        <span className="evid-label">cites</span>
        {use.cited.length ? use.cited.map((item) => <EvidenceChip key={item.id} item={item} />) : <span className="muted small">no evidence</span>}
      </div>
      <div className="evid-line">
        <span className="evid-label" title="The evidence the passing rules relied on (basis_evidence_ids)">
          basis
        </span>
        {use.basis === null ? (
          <span className="muted small">not recorded for this run</span>
        ) : use.basis.length ? (
          use.basis.map((item) => <EvidenceChip key={item.id} item={item} note={item.citedByClaim ? "" : "not cited by the claim"} />)
        ) : (
          <span className="muted small">{supported ? "none recorded" : "none: only a supported claim has a basis"}</span>
        )}
      </div>
      {use.contradicting.length ? (
        <div className="evid-line">
          <span className="evid-label">contradicted by</span>
          {use.contradicting.map((item) => (
            <EvidenceChip key={item.id} item={item} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

// claimDetails labels -> the Hypothesis.details key they come from.
const DETAIL_KEY: Record<string, string> = { release: "version", blames: "dependency" };

/** What the merger keyed a check by (HypothesisCheck.detail): its release or blamed dependency. */
function JudgedDetail({ check, author }: { check: HypothesisCheck; author: TextAuthor }) {
  const detail = checkDetail(check);
  if (!detail) return null;
  const mechanism = check.hypothesis.mechanism;
  const title = "What the merger keys this claim by (resolved from the evidence when the claim names none): two releases or dependencies are judged apart";
  return isCheckedValue(mechanism, detail) ? (
    <Chip mono title={title}>
      {`judged as ${detailLabel(mechanism, detail)}`}
    </Chip>
  ) : (
    // Not shaped like a version or a service: the claim's own text, shown as data.
    <span className="evid-line">
      <span className="muted small" title={title}>
        judged as
      </span>
      <Said quote tag={`untrusted · ${author.who} text`}>
        {detail}
      </Said>
    </span>
  );
}

function CheckRow({
  check,
  byId,
  author,
  diagnosis,
}: {
  check: HypothesisCheck;
  byId: ReadonlyMap<string, Evidence>;
  author: TextAuthor;
  diagnosis: boolean;
}) {
  const h = check.hypothesis;
  const proposers = proposersOf(check);
  // A rule line that repeats a release the claim named in free text quotes the agent.
  const quotes = failedQuotesClaim(check);
  return (
    <div className={diagnosis ? "check evid-diagnosis" : "check"}>
      <div className="check-head">
        <HypothesisChip status={h.status} />
        <span className="check-claim">
          <Said tag={`untrusted · ${author.who} text`}>{h.claim}</Said>
        </span>
        {diagnosis ? (
          <Chip tone="ok">
            <Icon name="check" size={12} /> diagnosis
          </Chip>
        ) : null}
      </div>
      <div className="chip-row">
        <Mono>{h.mechanism}</Mono>
        <span className="muted">on</span>
        <Mono>{h.target_service ?? "?"}</Mono>
        {claimDetails(h.details).map((d) =>
          // A release or dependency not shaped like one is the claim's own text: quoted data.
          isCheckedDetail(DETAIL_KEY[d.label] ?? "", d.value) ? (
            <span key={d.label} className="muted small">
              {d.label} <Mono>{d.value}</Mono>
            </span>
          ) : (
            <span key={d.label} className="evid-line">
              <span className="muted small">{d.label}</span>
              <Said quote tag={`untrusted · ${author.who} text`}>
                {d.value}
              </Said>
            </span>
          ),
        )}
        <Chip tone={h.origin === "memory" ? "info" : "neutral"}>origin: {h.origin}</Chip>
        <JudgedDetail check={check} author={author} />
      </div>
      <div className="evid-line">
        <span className="evid-label">proposed by</span>
        <AgentChips agents={proposers.agents} />
        {proposers.agents.length > 1 ? <span className="muted small">the same claim: their evidence was pooled</span> : null}
        {!proposers.recorded ? <span className="muted small">other proposers: not recorded</span> : null}
      </div>
      {h.confidence_note ? <ConfidenceNote note={h.confidence_note} author={author} /> : null}
      <ul className="rule-list">
        {check.passed.map((r, i) => (
          <li key={`p${i}`} className="text-ok">
            <Icon name="check" size={13} /> {r}
          </li>
        ))}
        {check.failed.map((r, i) => (
          <li key={`f${i}`} className="text-bad">
            <Icon name="x" size={13} /> {r}
            {quotes[i] ? (
              <span
                className="trust-tag trust-untrusted evid-rule-tag"
                title={`RECAP's rule line, but it repeats the release the claim named, which the ${author.who} wrote: that part is untrusted data.`}
              >
                quotes {author.who} text
              </span>
            ) : null}
          </li>
        ))}
      </ul>
      <div className="chip-row">
        {check.rule_ids.map((id) => (
          <Chip key={id} mono title={ruleNote(id)}>
            {id}
          </Chip>
        ))}
      </div>
      <EvidenceUseRows use={evidenceUse(check, byId)} supported={h.status === "supported"} />
    </div>
  );
}

function Verification({ record, evidence }: { record: RunRecord; evidence: Evidence[] }) {
  const report = record.verification;
  if (!report) return <span className="muted">The evidence verifier has not run yet.</span>;
  const author = textAuthor(record.provider_info);
  const byId = new Map(evidence.map((e) => [e.id, e]));
  const best = diagnosisCheck(report);
  return (
    <>
      <div className="verify-head">
        <HypothesisChip status={report.status} />
        <span>{report.summary || "—"}</span>
      </div>
      {report.best ? (
        <Notice tone="ok" icon="check" title="Diagnosis (best supported, actionable hypothesis)">
          <span className="evid-line">
            <Mono>{causeLabel(report.best)}</Mono>
            {best ? <JudgedDetail check={best} author={author} /> : null}
            <Said tag={`untrusted · ${author.who} text`}>{report.best.claim}</Said>
          </span>
        </Notice>
      ) : null}
      {report.unknown_evidence_ids.length ? (
        <Notice tone="bad" icon="x" title="Cited evidence that does not exist in this run (rejected)">
          {report.unknown_evidence_ids.join(", ")}
        </Notice>
      ) : null}
      {report.checks.length ? (
        report.checks.map((c, i) => <CheckRow key={i} check={c} byId={byId} author={author} diagnosis={isDiagnosis(report, c)} />)
      ) : (
        <span className="muted">No hypotheses were checked.</span>
      )}
    </>
  );
}

// --- disagreements and the merge history ---------------------------------------------------

/**
 * One disagreement. Its kind, claims, resolution and question are RECAP's own (trusted) text.
 * `events` is null while the trace loads: what became of the question is then left unsaid.
 */
function DisagreementItem({ d, round, events }: { d: DisagreementView; round: number | null; events: TraceEvent[] | null }) {
  const kind = disagreementKind(d.kind);
  const badge = disagreementBadge(d.resolved);
  return (
    <div className={d.resolved ? "check" : "check evid-open"}>
      <div className="check-head">
        <Chip tone={badge.tone}>{badge.label}</Chip>
        <span className="cell-stack">
          <span className="check-claim">{kind.label}</span>
          {kind.plain ? <span className="muted small">{kind.plain}</span> : null}
        </span>
      </div>
      {d.resolution ? <div className="evid-resolution">{d.resolution}</div> : null}
      <KeyValues
        className="evid-kv"
        rows={[
          [
            "Settled by",
            ruleIdsOf(d.rule_id).length ? (
              <RuleChips ruleId={d.rule_id} />
            ) : (
              <span className="muted">{d.resolved ? "no evidence rule" : "no evidence rule (open)"}</span>
            ),
          ],
          [
            "Claims",
            d.claims.length ? (
              <span className="chip-row">
                {d.claims.map((c) => (
                  <Chip key={c}>{c}</Chip>
                ))}
              </span>
            ) : (
              <span className="muted">none</span>
            ),
          ],
          ["Agents", <AgentChips agents={d.agents} />],
          [
            d.resolved ? "Evidence that decided it" : "Evidence in dispute",
            d.evidence_ids.length ? (
              <span className="chip-row">
                {d.evidence_ids.map((id) => (
                  <Chip key={id} mono>
                    {id}
                  </Chip>
                ))}
              </span>
            ) : (
              <span className="muted">none</span>
            ),
          ],
        ]}
      />
      {!d.resolved && d.question ? (
        <Notice tone="warn" icon="route" title="Question for the next round">
          {d.question}
          {events ? <div className="muted small spaced">{askedSummary(roundsAsked(d.question, events), round)}</div> : null}
        </Notice>
      ) : null}
    </div>
  );
}

function MergeDetail({ row, events }: { row: MergeRow; events: TraceEvent[] }) {
  // Open questions that no listed disagreement already shows.
  const questions = row.openQuestions.filter((q) => !row.disagreements.some((d) => d.question === q));
  if (!row.disagreements.length && !questions.length && !row.leads.length) {
    return <span className="muted">No disagreement, no open question and no lead in this verification.</span>;
  }
  return (
    <div className="stack">
      {row.disagreements.map((d, i) => (
        <DisagreementItem key={i} d={d} round={row.round} events={events} />
      ))}
      {questions.length ? (
        <div>
          <h3 className="mini-title">Open questions for the next round</h3>
          <ul className="plain-list">
            {questions.map((q) => (
              <li key={q}>{q}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {row.leads.length ? <LeadList leads={row.leads} /> : null}
    </div>
  );
}

/** One row per verification: what each agent claimed, what it merged into, and the conflicts. */
function MergeHistory({ rows, events }: { rows: MergeRow[]; events: TraceEvent[] }) {
  return (
    <DataTable
      className="compact wrap-head"
      rows={rows}
      rowKey={(r) => String(r.seq)}
      empty="No verification.merge events: no claims were merged yet, or this run was recorded before merges were traced."
      expand={(r) => <MergeDetail row={r} events={events} />}
      columns={[
        { key: "round", header: "Round", numeric: true, render: (r) => (r.round === null ? MISSING : r.round) },
        {
          key: "claims",
          header: <span title="Claims each agent made this epoch so far (every round since the last fix), and what they merged into">Claims made → merged</span>,
          render: (r) => (
            <span className="cell-stack">
              {r.claimsByAgent.length ? (
                r.claimsByAgent.map((c) => (
                  <span key={c.agent} className="nowrap">
                    <Mono>{c.agent}</Mono> {fmtInt(c.claims)}
                  </span>
                ))
              ) : (
                <span className="muted">no claims</span>
              )}
              <span className="muted small">merged into {fmtInt(r.mergedClaims)}</span>
            </span>
          ),
        },
        {
          key: "shared",
          header: <span title="Claims several agents made: their evidence was pooled">Shared claims</span>,
          render: (r) =>
            r.shared.length ? (
              <span className="cell-stack">
                {r.shared.map((s) => (
                  <span key={`${s.cause}-${s.proposers.join(",")}`} className="nowrap" title={`proposed by ${s.proposers.join(", ")}`}>
                    <Mono>{s.cause}</Mono>
                    {s.count > 1 ? <span className="muted"> ×{s.count}</span> : null}
                  </span>
                ))}
              </span>
            ) : (
              <span className="muted">none</span>
            ),
        },
        {
          key: "disagreements",
          header: "Disagreements",
          render: (r) =>
            r.disagreements.length ? (
              <span className="cell-stack">
                {r.disagreements.map((d, i) => {
                  const badge = disagreementBadge(d.resolved);
                  return (
                    <span key={i} className="chip-row" title={d.resolution || undefined}>
                      <Chip tone={badge.tone}>{badge.label}</Chip>
                      <span>{disagreementKind(d.kind).label}</span>
                      {ruleIdsOf(d.rule_id).map((id) => (
                        <Mono key={id}>{id}</Mono>
                      ))}
                    </span>
                  );
                })}
              </span>
            ) : (
              <span className="muted">none</span>
            ),
        },
        {
          key: "questions",
          header: <span title="Open questions for the next round, and RECAP's leads (where to look, no conflict)">Open questions</span>,
          numeric: true,
          render: (r) => (
            <span className="cell-stack">
              <span>{fmtInt(r.openQuestions.length)}</span>
              {r.leads.length ? <span className="muted small nowrap">{r.leads.length === 1 ? "1 lead" : `${r.leads.length} leads`}</span> : null}
            </span>
          ),
        },
        {
          key: "verdict",
          header: "Verdict",
          render: (r) =>
            r.status ? (
              <span className="cell-stack">
                <span className="chip-row">
                  <HypothesisChip status={r.status} />
                </span>
                {r.best ? <Mono>{r.best}</Mono> : null}
              </span>
            ) : (
              <span className="muted">{MISSING}</span>
            ),
        },
      ]}
    />
  );
}

function Disagreements({ record, events }: { record: RunRecord; events: Polled<TraceEvent[]> }) {
  const report = record.verification;
  const final = finalDisagreements(report, record.findings);
  const leads = reportLeads(report);
  const history = mergeHistory(events.data ?? []);
  // record.verification is the latest report: the one the last verification.merge closed.
  const round = history.length ? history[history.length - 1].round : null;
  const earlier = history.slice(0, -1).reduce((n, r) => n + r.disagreements.length, 0);
  const open = !!final?.some((d) => !d.resolved);
  return (
    <>
      {!report ? (
        <span className="muted">The evidence verifier has not run yet.</span>
      ) : final === null ? (
        <span className="muted">Not recorded: this run was verified before RECAP recorded disagreements.</span>
      ) : final.length ? (
        final.map((d, i) => <DisagreementItem key={i} d={d} round={round} events={events.data ?? null} />)
      ) : (
        <span className="muted">
          No disagreement in the final verification.
          {earlier ? ` Earlier verifications recorded ${earlier === 1 ? "1 disagreement" : `${earlier} disagreements`}: see the merge history.` : ""}
        </span>
      )}
      {leads.length ? <LeadList leads={leads} title="RECAP's leads for the next round (final verification)" /> : null}
      {open && isTerminal(record.state) ? (
        <Notice tone={stateTone(record.state)} icon="users" title="How the run ended">
          <span className="evid-line">
            <StateChip state={record.state} />
            <span>{record.outcome_note || MISSING}</span>
          </span>
        </Notice>
      ) : null}
      <h3 className="mini-title evid-sub">Merge history (verification.merge: one row per verification)</h3>
      {events.data ? <MergeHistory rows={history} events={events.data} /> : <TraceState events={events} />}
    </>
  );
}

// --- retrieval (searches, signals, citations) ---------------------------------------------------

function RetrievalSummaryView({ record }: { record: RunRecord }) {
  const s = retrievalSummary(record.retrieval);
  if (!s) {
    return (
      <span className="muted">
        Not recorded: this run did not record how it searched the runbooks (mode, embedder, embedding requests, agent searches).
      </span>
    );
  }
  return (
    <KeyValues
      rows={[
        [
          "Mode",
          <span className="cell-stack">
            <Mono>{s.mode}</Mono>
            <span className="muted small">{s.modeNote}</span>
          </span>,
        ],
        ["Embedder", <Mono>{s.embedder}</Mono>],
        [
          "Embedding server",
          <span title={s.weights ?? undefined}>
            <Mono>{s.server}</Mono>
          </span>,
        ],
        [
          "Embedding requests",
          <span>
            {s.embeddingRequests}
            {s.embedded ? <span className="muted small"> · {s.replayedRequests} answered from a recording</span> : null}
          </span>,
        ],
        [
          "Embedding input tokens",
          <span>
            {s.embeddingInputTokens}
            {s.tokensReported ? <span className="muted small"> · as the server reported them</span> : null}
          </span>,
        ],
        [
          "Agent searches",
          <span>
            {s.agentSearches} <span className="muted small">· search_runbooks calls answered</span>
          </span>,
        ],
      ]}
    />
  );
}

/** A search result table: rank, score and every ranking signal (ranks as integers, missing "—"). */
function ResultsTable({ results, isOverlay }: { results: SearchResult[]; isOverlay: OverlayTest }) {
  return (
    <DataTable
      className="compact"
      rows={results}
      rowKey={(r, i) => `${r.chunkId}-${i}`}
      rowClassName={(r) => (isOverlay(r) ? "row-warn" : undefined)}
      empty="No runbook section matched."
      columns={[
        { key: "rank", header: "Rank", numeric: true, render: (r) => fmtRank(r.rank) },
        {
          key: "chunk",
          header: "Section",
          render: (r) => (
            <span className="cell-stack">
              <span className="chip-row">
                <Mono>{r.chunkId || MISSING}</Mono>
                {isOverlay(r) ? <OverlayChip /> : null}
              </span>
              {r.docPath ? (
                <span className="muted small">
                  <Mono>{r.docPath}</Mono>
                </span>
              ) : null}
            </span>
          ),
        },
        { key: "heading", header: "Heading", render: (r) => (r.title ? `${r.title} › ${r.heading}` : r.heading) || MISSING },
        { key: "score", header: "Score", numeric: true, render: (r) => fmtMeasure(r.score) },
        ...SIGNAL_COLUMNS.map((s) => ({
          key: s.key,
          header: <span title={s.title}>{s.label}</span>,
          numeric: true,
          render: (r: SearchResult) => fmtSignal(r.signals, s.key),
        })),
      ]}
    />
  );
}

/** One traced search. An agent's query is its own (untrusted) text; RECAP's queries are RECAP's. */
function SearchBlock({ search, author, isOverlay }: { search: Search; author: TextAuthor; isOverlay: OverlayTest }) {
  const embedding = search.embedding;
  return (
    <div className="evid-search">
      <div className="evid-search-head">
        <Chip tone={search.skipped ? "neutral" : search.stage === "agent" ? "accent" : "info"}>
          {stageLabel(search.stage)} · {search.skipped ? "skipped" : `${search.results.length} retrieved`}
        </Chip>
        {search.agent ? <Chip mono>{search.agent}</Chip> : null}
        {search.mode ? (
          <Chip mono title={modeNote(search.mode)}>
            {search.mode}
          </Chip>
        ) : search.skipped ? null : (
          <span className="muted small">mode not recorded</span>
        )}
        {/* What this search actually ranked with (retrieval.docs `retriever`). */}
        {search.retriever && !search.skipped ? (
          <span className="muted small">
            ranked by <Mono>{search.retriever}</Mono>
          </span>
        ) : null}
        {search.reason ? <span className="muted">{search.reason}</span> : null}
      </div>
      {search.query ? (
        <div className="evid-line">
          <span className="evid-label">query</span>
          {search.queryBy === "agent" ? (
            <Said tag={`untrusted · written by the ${author.who}`}>{search.query}</Said>
          ) : (
            <>
              <Mono>{search.query}</Mono>
              <span className="muted small">{recapQueryNote(search.stage)}</span>
            </>
          )}
        </div>
      ) : null}
      {embedding ? (
        <div className="muted small">
          query embedded by <Mono>{embedding.model || MISSING}</Mono> ·{" "}
          {embedding.inputTokens === null ? "input tokens not reported" : `${fmtInt(embedding.inputTokens)} input tokens (as the server reported them)`} ·{" "}
          {fmtMs(embedding.durationMs)}
          {embedding.replayed ? " · answered from a recording" : ""}
        </div>
      ) : null}
      {search.skipped ? null : <ResultsTable results={search.results} isOverlay={isOverlay} />}
    </div>
  );
}

function Searches({
  searches,
  events,
  author,
  isOverlay,
}: {
  searches: Search[] | null;
  events: Polled<TraceEvent[]>;
  author: TextAuthor;
  isOverlay: OverlayTest;
}) {
  if (!searches) return <TraceState events={events} />;
  if (!searches.length) return <span className="muted">No runbook search was traced for this run yet.</span>;
  return (
    <div className="evid-searches">
      {searches.map((s) => (
        <SearchBlock key={s.seq} search={s} author={author} isOverlay={isOverlay} />
      ))}
    </div>
  );
}

function OverlayBanner({ overlays }: { overlays: string[] }) {
  return (
    <Notice tone="warn" icon="alert" title={`This run indexed extra knowledge folders: ${overlays.join(", ")} (red-team)`}>
      Knowledge overlays (<Mono>RECAP_KNOWLEDGE_OVERLAYS</Mono>) add runbooks for red-team tests, and they may be deliberately wrong.
      Every retrieved section from them is marked <OverlayChip /> on this page. A runbook never proves a claim: the verifier reads only
      live tool results.
    </Notice>
  );
}

function RetrievedDocs({ docs, overlays, seen }: { docs: RetrievedChunk[]; overlays: string[]; seen: Map<string, string[]> | null }) {
  const top = Math.max(0.0001, ...docs.map((d) => d.score));
  return (
    <DataTable
      className="evid-docs"
      rows={docs}
      rowKey={(d) => `${d.chunk.chunk_id}-${d.rank}`}
      rowClassName={(d) => (isOverlayPath(d.chunk.doc_path, overlays) ? "row-warn" : undefined)}
      empty="No runbook chunks were retrieved (RAG off, or retrieval skipped)."
      expand={(d) => (
        <div className="detail-grid">
          <div>
            {isOverlayPath(d.chunk.doc_path, overlays) ? (
              <div className="spaced-bottom">
                <Notice tone="warn" icon="alert" title="Overlay content (red-team)">
                  This section comes from an extra knowledge folder indexed for this run. It may be deliberately wrong.
                </Notice>
              </div>
            ) : null}
            <h3 className="mini-title">
              {d.chunk.title} › {d.chunk.heading} (untrusted text)
            </h3>
            <pre className="raw">{d.chunk.text}</pre>
          </div>
          <div>
            <KeyValues
              rows={[
                ["Checksum", <Mono>{d.chunk.checksum}</Mono>],
                ["Est. tokens", fmtInt(d.chunk.token_estimate)],
                ["Signals", signalSummary(d.signals)],
                ["Provenance", <JsonView value={d.chunk.provenance} maxHeight={160} />],
              ]}
            />
          </div>
        </div>
      )}
      columns={[
        { key: "rank", header: "Rank", numeric: true, render: (d) => fmtRank(d.rank) },
        { key: "score", header: "Score", render: (d) => <Meter value={d.score} max={top} label={fmtMeasure(d.score)} width={70} /> },
        {
          key: "chunk",
          header: "Chunk · document",
          render: (d) => (
            <span className="cell-stack">
              <span className="chip-row">
                <Mono>{d.chunk.chunk_id}</Mono>
                {isOverlayPath(d.chunk.doc_path, overlays) ? <OverlayChip /> : null}
              </span>
              <span className="muted small evid-path">
                <Mono>{d.chunk.doc_path}</Mono>
              </span>
            </span>
          ),
        },
        { key: "heading", header: "Heading", render: (d) => d.chunk.heading },
        { key: "signals", header: "Signals", render: (d) => <SignalLines signals={d.signals} /> },
        {
          key: "seen",
          header: <span title="Agents that had this section in their context packs (AgentFinding.doc_refs)">Seen by</span>,
          render: (d) =>
            seen === null ? (
              <span className="muted small">not recorded</span>
            ) : (
              <AgentChips agents={seen.get(d.chunk.chunk_id) ?? []} empty="not in an agent's pack" />
            ),
        },
        {
          key: "retriever",
          header: "Retriever",
          render: (d) => (
            <span className="cell-stack">
              <Mono>{d.retriever}</Mono>
              {d.reranker ? <span className="muted small">reranker {d.reranker}</span> : null}
            </span>
          ),
        },
      ]}
    />
  );
}

// --- knowledge search -----------------------------------------------------------------------

function KnowledgeSearch() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<KnowledgeResult[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);

  async function search(e: FormEvent) {
    e.preventDefault();
    const q = query.trim();
    if (q.length < 2) return;
    setBusy(true);
    setError(null);
    try {
      setResults(await api.knowledgeSearch(q, 5));
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Knowledge search" subtitle="GET /knowledge/search — BM25 over the runbooks, with citations, parsed action hints and injection flags">
      <form className="search-row" onSubmit={(e) => void search(e)}>
        <Icon name="search" size={16} />
        <input
          className="input"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="e.g. rollback after a bad release, OOMKilled restart, upstream dependency errors"
          aria-label="Search the knowledge base"
        />
        <button type="submit" className="btn btn-primary" disabled={busy || query.trim().length < 2}>
          {busy ? "Searching…" : "Search"}
        </button>
      </form>
      {error ? <ErrorState error={error} /> : null}
      {results && results.length === 0 ? <EmptyState title="No matching chunks" icon="search" /> : null}
      {results && results.length > 0 ? (
        <DataTable
          rows={results}
          rowKey={(r) => `${r.chunk.chunk_id}-${r.rank}`}
          rowClassName={(r) => (isOverlayPath(r.chunk.doc_path) ? "row-warn" : undefined)}
          expand={(r) => <pre className="raw">{r.chunk.text}</pre>}
          columns={[
            { key: "rank", header: "Rank", numeric: true, render: (r) => fmtRank(r.rank) },
            { key: "score", header: "Score", numeric: true, render: (r) => fmtScore(r.score) },
            {
              key: "heading",
              header: "Citation",
              render: (r) => (
                <span className="cell-stack">
                  <span>
                    {r.chunk.title} › {r.chunk.heading}
                  </span>
                  <span className="muted small">
                    <Mono>{r.chunk.doc_path}</Mono> · <Mono>{r.chunk.chunk_id}</Mono>
                  </span>
                  {isOverlayPath(r.chunk.doc_path) ? (
                    <span className="chip-row">
                      <OverlayChip />
                    </span>
                  ) : null}
                </span>
              ),
            },
            { key: "signals", header: "Signals", render: (r) => <SignalLines signals={r.signals} /> },
            {
              key: "hints",
              header: "Action hints",
              render: (r) =>
                r.action_hints.length ? (
                  <span className="chip-row">
                    {r.action_hints.map((h, i) => (
                      <Chip key={i} tone="accent" mono>
                        {h.mechanism} → {h.action}
                      </Chip>
                    ))}
                  </span>
                ) : (
                  <span className="muted">—</span>
                ),
            },
            {
              key: "flags",
              header: "Flags",
              render: (r) =>
                r.flags.length ? (
                  <span className="chip-row">
                    {r.flags.map((f) => (
                      <FlagChip key={f} flag={f} />
                    ))}
                  </span>
                ) : (
                  <span className="muted">—</span>
                ),
            },
          ]}
        />
      ) : null}
    </Card>
  );
}

// --- page -------------------------------------------------------------------------------------

function EvidenceView({ runId, running, focusId }: { runId: string; running: boolean; focusId: string | null }) {
  const detail = usePolling(() => api.run(runId), (d) => (d?.status === "running" || (running && !d?.record) ? 2000 : null), [runId]);
  const events = usePolling(() => api.events(runId), (list) => (list?.some((e) => e.category === "run.summary") || !running ? null : 2500), [runId]);
  const { refresh: refreshEvents } = events;
  useEffect(() => {
    if (running) refreshEvents();
  }, [running, refreshEvents]);

  if (detail.error && !detail.data) return <ErrorState error={detail.error} />;
  if (!detail.data) return <Loading label="Loading run checkpoint and evidence…" />;
  const record = detail.data.record;
  if (!record) {
    return (
      <EmptyState title="No checkpoint yet" icon="clock">
        {detail.data.error ?? "The run has not saved its first checkpoint."}
      </EmptyState>
    );
  }
  const evidence = detail.data.evidence;
  const flagged = evidence.filter((e) => e.flags.includes("prompt_injection_suspected")).length;
  const author = textAuthor(record.provider_info);
  const overlays = knowledgeOverlays(record.versions);
  const searches = events.data ? searchesOf(events.data) : null;
  // A search receipt names chunks without their paths: match those by chunk id.
  const overlayIds = overlayChunkIds(searches ?? [], record.retrieved_docs, overlays);
  const isOverlay: OverlayTest = (r) => (r.docPath ? isOverlayPath(r.docPath, overlays) : overlayIds.has(r.chunkId));

  return (
    <>
      {overlays.length ? <OverlayBanner overlays={overlays} /> : null}

      <Card
        title={`Evidence registry (${evidence.length})`}
        subtitle="Immutable observations with run-unique ids. Agents and the verifier pass these ids around, never mutable objects. Click a row for the raw content."
        actions={flagged ? <Chip tone="warn"><Icon name="alert" size={12} /> {flagged} injection-flagged</Chip> : undefined}
        flush
      >
        <EvidenceTable evidence={evidence} focusId={focusId} roles={evidenceRoles(record.verification)} author={author} isOverlay={isOverlay} />
      </Card>

      <Card
        title="Hypothesis verification"
        subtitle="record.verification — every claim (the agents' untrusted text) must cite evidence ids recorded in this run; RECAP's rules decide"
        actions={<OriginChip provider={record.provider_info} />}
      >
        <Verification record={record} evidence={evidence} />
      </Card>

      <Card
        title="Disagreements"
        subtitle="record.verification.disagreements — where the agents' claims conflict and which evidence rule settled each one (rules decide, never votes)"
      >
        <Disagreements record={record} events={events} />
      </Card>

      <Card title="Retrieval" subtitle="record.retrieval and every retrieval.docs event — how this run searched the runbooks, and why each section ranked where it did">
        <RetrievalSummaryView record={record} />
        <h3 className="mini-title evid-sub">Searches (retrieval.docs and retrieval.skip)</h3>
        <Searches searches={searches} events={events} author={author} isOverlay={isOverlay} />
      </Card>

      <Card title="Citations: retrieved runbook chunks" subtitle="record.retrieved_docs — source-labeled RAG results the agents could see" flush>
        <RetrievedDocs docs={record.retrieved_docs} overlays={overlays} seen={seenBy(record.findings, events.data ?? [])} />
      </Card>
    </>
  );
}

export function EvidencePage({ route }: PageProps) {
  const selection = useRunSelection(route);
  return (
    <>
      <PageHeader
        title="Evidence & RAG"
        description="What the agents observed (tool outputs are untrusted data), how each hypothesis was checked against that evidence, where the agents disagreed, and which runbooks were searched and cited."
      />
      <RunScope selection={selection} page="evidence">
        {(runId, summary) => (
          <EvidenceView runId={runId} running={!!summary && !isTerminal(summary.state)} focusId={route.params.get("ev")} />
        )}
      </RunScope>
      <KnowledgeSearch />
    </>
  );
}
