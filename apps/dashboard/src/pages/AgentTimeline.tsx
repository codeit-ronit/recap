import { useEffect, useMemo } from "react";
import { api } from "../api";
import { Chip, FlagChip, HypothesisChip, Mono, OriginChip } from "../components/Badge";
import { Card, KeyValues, Meter, Notice, PageHeader, StatTile } from "../components/Card";
import { EmptyState, ErrorState, Loading } from "../components/EmptyState";
import { Icon } from "../components/Icon";
import { RunScope } from "../components/RunPicker";
import { AgentGantt, buildTimeline } from "../components/Timeline";
import { arr, fmtInt, fmtMs, fmtScore, humanize, isTerminal, num, obj, roleLabel, splitSummary, str } from "../format";
import { hrefFor, usePolling, useRunSelection } from "../hooks";
import type { PageProps } from "../hooks";
import type { OrchestrationAgent, OrchestrationPayload, RoutingPayload, RunRecord, TraceEvent } from "../types";
import { buildRounds, droppedLine, firstIdSuffix, shortRole, textAuthor } from "../view/findings";
import type {
  AgentView,
  CheckView,
  ClaimView,
  EvidenceRef,
  FinalVerdict,
  JudgedBy,
  RoundView,
  RunbookSearch,
  TextAuthor,
  VerifierStep,
} from "../view/findings";
import { knowledgeOverlays, overlayChunkIds, searchesOf } from "../view/retrieval";
import { detailLabel, isCheckedDetail, ruleNote } from "../view/verification";
import "../styles/timeline-m3.css";

/** What each routing signal means (documentation for the signals table). */
const SIGNAL_NOTES: Record<string, string> = {
  services_affected: "services named by the alert",
  symptom_categories: "kinds of symptoms observed",
  memory_hits: "similar past episodes found",
  top_memory_similarity: "best memory similarity (0–1)",
  familiar_success: "a verified success is very similar",
  memory_conflict: "similar episodes disagree",
  prior_failed_attempts: "failed mitigations in this run + very similar FAILED past episodes",
  escalation: "previous round was inconclusive",
  disagreement: "the last round left a conflict no evidence rule settled",
  remaining_tool_calls: "tool-call budget left",
  remaining_steps: "step budget left",
  max_parallel_agents: "parallelism cap",
};

function signalValue(value: unknown) {
  if (typeof value === "boolean") return <Chip tone={value ? "accent" : "neutral"}>{value ? "yes" : "no"}</Chip>;
  if (typeof value === "number") return <span className="num">{Number.isInteger(value) ? fmtInt(value) : fmtScore(value)}</span>;
  if (Array.isArray(value)) return value.length ? <span className="chip-row">{value.map((v, i) => <Chip key={i}>{str(v)}</Chip>)}</span> : <span className="muted">none</span>;
  return <span>{str(value, "—")}</span>;
}

function OrchestrationView({ event }: { event: TraceEvent | undefined }) {
  if (!event) return <span className="muted">Agents still running…</span>;
  const p = event.payload as OrchestrationPayload;
  const agents = arr<OrchestrationAgent>(p.agents);
  const wall = num(p.wall_ms);
  const sum = num(p.sum_ms);
  const overlap = num(p.overlap_ms) ?? 0;
  const overlapped = p.mode === "parallel" && overlap > 0;
  const longest = Math.max(1, ...agents.map((a) => num(a.duration_ms) ?? 0));
  return (
    <div className="orch">
      <div className={overlapped ? "orch-headline ok" : "orch-headline"}>
        {p.mode !== "parallel"
          ? "Single agent — no concurrency in this round"
          : overlapped
            ? "Parallel round — the agents ran at the same time"
            : "Parallel round, but the agents did not overlap"}
      </div>
      {/* The overlap is measured. "Back to back" never ran (it is the sum of the agents' times), so it is labeled an estimate. */}
      <div className="muted">
        wall clock {fmtMs(wall)} ·{" "}
        {overlapped
          ? `agents overlapped for ${fmtMs(overlap)} (running them back to back would have taken about ${fmtMs(sum)} — an estimate)`
          : `sum of agent time ${fmtMs(sum)}`}
      </div>
      <div className="orch-agents">
        {agents.map((a, i) => (
          <div className="orch-agent" key={`${str(a.role)}-${i}`}>
            <Mono>{str(a.role, "agent")}</Mono>
            <Meter value={num(a.duration_ms) ?? 0} max={longest} label={fmtMs(num(a.duration_ms))} width={120} />
            {a.error ? <Chip tone="bad">{a.error}</Chip> : null}
          </div>
        ))}
      </div>
    </div>
  );
}

/** The router's view of a round: the signals it used, the step allocation and how the agents ran. */
function RoutingDetails({ event, orchestration }: { event: TraceEvent; orchestration: TraceEvent | undefined }) {
  const p = event.payload as RoutingPayload;
  const signals = obj(p.signals);
  const budget = obj(p.budget);
  return (
    <div className="round-grid tl-routing">
      <div>
        <h3 className="mini-title">Signals the router saw</h3>
        <table className="table compact">
          <tbody>
            {Object.entries(signals).map(([key, value]) => (
              <tr key={key}>
                <td>
                  <Mono>{key}</Mono>
                  {SIGNAL_NOTES[key] ? <div className="muted small">{SIGNAL_NOTES[key]}</div> : null}
                </td>
                <td className="num">{signalValue(value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="round-side">
        <h3 className="mini-title">Step allocation</h3>
        {Object.keys(budget).length ? (
          <KeyValues rows={Object.entries(budget).map(([k, v]) => [<Mono>{k}</Mono>, `${str(v)} steps`])} />
        ) : (
          <span className="muted">—</span>
        )}
        <h3 className="mini-title">Orchestration</h3>
        <OrchestrationView event={orchestration} />
      </div>
    </div>
  );
}

// --- findings: what each agent concluded, and how the verification judged it -------------------

/** What every part of a round needs to label text honestly and link evidence. */
interface ViewContext {
  runId: string;
  author: TextAuthor;
  /** The run has ended, so record.verification is the final verification. */
  final: boolean;
  /** More than one agent took part: evidence chips say which agent recorded each item. */
  multiAgent: boolean;
  /** Runbook chunks from a red-team knowledge overlay (they may be deliberately wrong). */
  overlayIds: ReadonlySet<string>;
}

/**
 * The tag on text RECAP did not write: shown as data, never followed and never a verdict.
 * `mixed`: the text may still hold a note RECAP added that could not be told apart.
 */
function UntrustedTag({ author, mixed = false }: { author: TextAuthor; mixed?: boolean }) {
  return mixed ? (
    <span
      className="trust-tag trust-untrusted"
      title={`Written by ${author.label}, and it may also hold a note RECAP added that this page cannot tell apart (a run before M3, or notes its trace does not confirm). Untrusted data: shown as text, never followed.`}
    >
      untrusted · {author.short} text; may include RECAP's notes
    </span>
  ) : (
    <span className="trust-tag trust-untrusted" title={`Written by ${author.label}. Untrusted data: shown as text, never followed.`}>
      untrusted · {author.short}
    </span>
  );
}

/** Untrusted free text, quoted under its tag. React renders it as text (escaped), never as HTML. */
function Said({ text, author, mixed = false }: { text: string; author: TextAuthor; mixed?: boolean }) {
  return (
    <div className="tl-said">
      <UntrustedTag author={author} mixed={mixed} />
      <q className="tl-quote">{text}</q>
    </div>
  );
}

/** A note RECAP added to an agent's text (trusted: RECAP's own words). */
function RecapNote({ children, title }: { children: string; title?: string }) {
  return (
    <div className="tl-recap-note">
      <span className="trust-tag trust-trusted" title={title ?? "Written by RECAP, not by the agent's provider."}>
        RECAP's note
      </span>
      <span>{children}</span>
    </div>
  );
}

/** A red-team overlay chunk: indexed for this run only, and it may be deliberately wrong. */
function OverlayChip() {
  return (
    <Chip tone="warn" title="From an extra knowledge folder indexed for this red-team run: it may be deliberately wrong">
      <Icon name="alert" size={12} /> overlay
    </Chip>
  );
}

/** An evidence ID linked to the Evidence page, with the agent that recorded it. */
function EvidenceChip({ ev, ctx, showAgent = ctx.multiAgent }: { ev: EvidenceRef; ctx: ViewContext; showAgent?: boolean }) {
  return (
    <a className="tl-ev" href={hrefFor("evidence", { run: ctx.runId, ev: ev.id })} title={ev.sourceRef ?? undefined}>
      <Mono nowrap>{ev.id}</Mono>
      {showAgent && ev.agent ? <span className="tl-ev-agent">{shortRole(ev.agent)}</span> : null}
      {ev.known === false ? <span className="tl-ev-missing">not in this run</span> : null}
    </a>
  );
}

function EvidenceRow({ label, refs, ctx }: { label: string; refs: EvidenceRef[]; ctx: ViewContext }) {
  return (
    <div className="tl-ev-row">
      <span className="tl-ev-label">{label}</span>
      {refs.length ? (
        refs.map((ev, i) => <EvidenceChip key={`${ev.id}-${i}`} ev={ev} ctx={ctx} />)
      ) : (
        <span className="muted small">none</span>
      )}
    </div>
  );
}

/** What the merger keyed a claim by (its release or blamed dependency), as the verdict judged it. */
function JudgedDetail({ check, mechanism, ctx }: { check: CheckView; mechanism: string; ctx: ViewContext }) {
  if (!check.detail) return null;
  if (!check.detailChecked) {
    // Not shaped like a version or a service: the claim's own text, so it is shown as data.
    return (
      <span className="chip-row">
        <span className="muted small" title="The merger keys a claim by this detail: two releases are judged apart">
          judged as
        </span>
        <Said text={check.detail} author={ctx.author} />
      </span>
    );
  }
  return (
    <Chip mono title="What the merger keys this claim by (resolved from the evidence when the claim names none): two releases or dependencies are judged apart">
      {`judged as ${detailLabel(mechanism, check.detail)}`}
    </Chip>
  );
}

function CheckLine({ check, mechanism, ctx }: { check: CheckView; mechanism: string; ctx: ViewContext }) {
  return (
    <>
      <div className="chip-row">
        <HypothesisChip status={check.status} />
        <JudgedDetail check={check} mechanism={mechanism} ctx={ctx} />
        <span className="muted small">proposed by</span>
        {check.proposers === null ? (
          <span className="muted small">not recorded</span>
        ) : check.proposers.length ? (
          check.proposers.map((role) => (
            <Chip key={role} mono>
              {role}
            </Chip>
          ))
        ) : (
          <span className="muted small">—</span>
        )}
      </div>
      {check.failed.length ? (
        <ul className="rule-list">
          {check.failed.map((line, i) => (
            <li key={i} className="text-bad">
              <Icon name="x" size={13} /> {line}
              {/* RECAP's sentence, repeating a value the claim named that is not a version: model text. */}
              {check.failedQuotesClaim[i] ? (
                <span
                  className="trust-tag trust-untrusted tl-rule-tag"
                  title={`RECAP's rule line, but it repeats a value the claim named, which ${ctx.author.label} wrote: that part is untrusted data.`}
                >
                  quotes {ctx.author.whose} text
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      {check.basis?.length ? <EvidenceRow label="its rules relied on" refs={check.basis} ctx={ctx} /> : null}
    </>
  );
}

/** Which verification a claim's verdict comes from (the last one that read it). */
function verdictSource(judged: JudgedBy, verdict: FinalVerdict, ctx: ViewContext): { label: string; title?: string } {
  if (verdict.kind === "unverified" && verdict.reason !== "not_recorded") return { label: "Verification" };
  if (judged.kind === "earlier") {
    return {
      label: `Verification before fix ${judged.fix}`,
      title: `The last verification that read this claim. Fix ${judged.fix} did not recover the system, so RECAP started over: later verifications, the final one included, judged only newer findings.`,
    };
  }
  return { label: ctx.final ? "Final verification" : "Latest verification · run still going" };
}

const UNVERIFIED_NOTES: Record<"partial" | "stopped" | "pending", string> = {
  partial: "partial: round cut short by a budget stop, not verified",
  stopped: "never verified: the run stopped before the verifier read this round",
  pending: "not verified yet: the verifier has not read this round",
};

/** The verification's verdict on one claim, labeled as the final one only once the run has ended. */
function VerdictLine({ verdict, judged, mechanism, ctx }: { verdict: FinalVerdict; judged: JudgedBy; mechanism: string; ctx: ViewContext }) {
  const source = verdictSource(judged, verdict, ctx);
  return (
    <div className="tl-verdict">
      <span className="tl-verdict-label" title={source.title}>
        {source.label}
      </span>
      {verdict.kind === "unverified" ? (
        <span className={verdict.reason === "not_recorded" ? "muted small" : "text-warn small"}>
          {verdict.reason === "not_recorded" ? (ctx.final ? "no verification was recorded" : "not recorded yet") : UNVERIFIED_NOTES[verdict.reason]}
        </span>
      ) : null}
      {verdict.kind === "unchecked" ? (
        <span className="muted small" title="No check in this verification has the claim's mechanism, target and origin.">
          did not check this claim
        </span>
      ) : null}
      {verdict.kind === "matched" ? <CheckLine check={verdict.check} mechanism={mechanism} ctx={ctx} /> : null}
      {verdict.kind === "ambiguous" ? (
        <div className="chip-row">
          <span className="muted small">
            {verdict.checks.length} checks share this claim's mechanism and target; the record does not say which one judged it:
          </span>
          {verdict.checks.map((c, i) => (
            <span key={i} className="chip-row">
              <HypothesisChip status={c.status} />
              <JudgedDetail check={c} mechanism={mechanism} ctx={ctx} />
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ClaimCard({ claim, judged, ctx }: { claim: ClaimView; judged: JudgedBy; ctx: ViewContext }) {
  return (
    <div className="tl-claim">
      {claim.claim ? <Said text={claim.claim} author={ctx.author} /> : <span className="muted small">no claim text</span>}
      <div className="chip-row">
        <Mono>{claim.mechanism}</Mono>
        <span className="muted">on</span>
        <Mono>{claim.target ?? "no service named"}</Mono>
        {claim.details.map(([key, value]) =>
          // A release or dependency not shaped like one is the claim's own text: quoted data.
          isCheckedDetail(key, value) ? (
            <Chip key={key} mono>
              {`${key}=${value}`}
            </Chip>
          ) : (
            <span key={key} className="chip-row">
              <span className="muted small">{key}</span>
              <Said text={value} author={ctx.author} />
            </span>
          ),
        )}
        {claim.origin === "memory" ? <Chip tone="info">recalled from memory</Chip> : null}
      </div>
      <EvidenceRow label="cites" refs={claim.supporting} ctx={ctx} />
      {claim.contradicting.length ? <EvidenceRow label="argues against" refs={claim.contradicting} ctx={ctx} /> : null}
      {claim.confidenceNote ? (
        <div className="tl-note">
          {/* The model's own words: quoted and labeled, never shown as a score. */}
          <span className="tl-note-label">Confidence note · {ctx.author.whose} own words, not a score</span>
          <Said text={claim.confidenceNote} author={ctx.author} />
        </div>
      ) : null}
      <VerdictLine verdict={claim.verdict} judged={judged} mechanism={claim.mechanism} ctx={ctx} />
    </div>
  );
}

function SearchStatus({ search }: { search: RunbookSearch }) {
  switch (search.status) {
    case "failed":
      return (
        <>
          <Chip tone="bad">failed</Chip>
          {search.error ? <span className="text-bad small">{search.error}</span> : null}
        </>
      );
    case "running":
      return <Chip tone="accent">in flight</Chip>;
    case "no_result":
      return <span className="muted small">no result recorded</span>;
    default:
      return null;
  }
}

/** One search_runbooks call: RECAP's own index answered it, never the environment's MCP server. */
function SearchView({ search, ctx }: { search: RunbookSearch; ctx: ViewContext }) {
  return (
    <div className="tl-search">
      <div className="chip-row">
        <Chip tone="info">
          <Icon name="book" size={12} /> answered by RECAP's runbook index
        </Chip>
        <span className="muted small">a local read tool, not the environment's MCP server</span>
        <SearchStatus search={search} />
      </div>
      <div className="tl-ev-row">
        <span className="tl-ev-label">query{search.k !== null ? ` (up to ${fmtInt(search.k)} sections)` : ""}</span>
      </div>
      {search.query ? <Said text={search.query} author={ctx.author} /> : <span className="muted small">no query recorded</span>}
      {search.hits === null ? (
        <span className="muted small">results not recorded</span>
      ) : search.hits.length ? (
        <>
          <span className="tl-ev-label">sections found (headings are runbook text: untrusted)</span>
          <ol className="tl-hits">
            {search.hits.map((hit, i) => (
              <li key={`${hit.chunkId}-${i}`}>
                <span className="tl-hit-rank">#{hit.rank}</span>
                <Mono>{hit.chunkId}</Mono>
                {ctx.overlayIds.has(hit.chunkId) ? <OverlayChip /> : null}
                {hit.heading ? <span className="muted">{hit.heading}</span> : null}
                {hit.signals.length ? (
                  <span className="tl-signals">{hit.signals.map((s) => `${s.label} ${s.value}`).join(" · ")}</span>
                ) : null}
              </li>
            ))}
          </ol>
        </>
      ) : (
        <span className="muted small">no sections found</span>
      )}
      {search.evidenceId ? (
        <div className="tl-ev-row">
          <span className="tl-ev-label">receipt</span>
          <EvidenceChip ev={{ id: search.evidenceId, agent: null, sourceRef: null, sourceType: "doc", flags: [], known: null }} ctx={ctx} />
          <span className="muted small">DOC evidence: the verifier never counts it as proof</span>
        </div>
      ) : null}
    </div>
  );
}

function sameQuestions(a: string[] | null, b: string[] | null): boolean {
  return !!a && !!b && a.length === b.length && a.every((q, i) => q === b[i]);
}

/** "1 step", "3 steps"; "— steps" when not recorded. */
function counted(n: number | null, one: string): string {
  return `${fmtInt(n)} ${n === 1 ? one : `${one}s`}`;
}

function AgentPanel({ agent, round, ctx }: { agent: AgentView; round: RoundView; ctx: ViewContext }) {
  const ownQuestions = agent.questions?.length && !sameQuestions(agent.questions, round.questions) ? agent.questions : null;
  // The provider's words and the notes RECAP added to them (format.ts splitSummary).
  const ended = agent.status === "finished" ? agent.outcome.key : "";
  const summary = splitSummary(agent.summary, ended, agent.modelNotes);
  return (
    <div className={agent.partial ? "tl-agent tl-partial" : "tl-agent"}>
      <div className="tl-agent-head">
        <h3 className="tl-agent-name">{roleLabel(agent.role)}</h3>
        <Chip tone={agent.outcome.tone} dot title={agent.outcome.key ? `outcome: ${agent.outcome.key}` : undefined}>
          {agent.outcome.label}
        </Chip>
        {agent.partial ? <Chip tone="warn">partial</Chip> : null}
        <span className="tl-agent-stats">
          {counted(agent.steps, "step")} · {counted(agent.toolCalls, "tool call")}
        </span>
      </div>
      {agent.outcome.note ? <div className="tl-outcome-note">{agent.outcome.note}</div> : null}
      {agent.partial ? (
        <div
          className="tl-partial-note"
          title="AgentFinding.partial: this finding was kept when a budget stop cut its round short. No verification read it, and it never reaches memory."
        >
          <Icon name="alert" size={13} /> partial: round cut short by a budget stop, not verified
        </div>
      ) : null}

      {ownQuestions ? (
        <Notice tone="info" icon="search" title="The verifier asked this agent (RECAP's own text)">
          <ul className="tl-questions">
            {ownQuestions.map((q, i) => (
              <li key={i}>{q}</li>
            ))}
          </ul>
        </Notice>
      ) : null}

      {agent.claims.length ? (
        <div className="tl-section">
          <h4 className="mini-title">Its claims ({agent.claims.length})</h4>
          <div className="tl-claims">
            {agent.claims.map((claim, i) => (
              <ClaimCard key={i} claim={claim} judged={round.judgedBy} ctx={ctx} />
            ))}
          </div>
        </div>
      ) : agent.claimTexts.length ? (
        <div className="tl-section">
          <h4 className="mini-title">Its claims (from agent.finish)</h4>
          {agent.claimTexts.map((claim, i) => (
            <Said key={i} text={claim} author={ctx.author} />
          ))}
          <span className="muted small">Verdicts appear once the run record has this finding.</span>
        </div>
      ) : agent.status === "finished" ? (
        <span className="muted small">No claims.</span>
      ) : null}

      {agent.dropped?.length ? (
        <div className="tl-dropped">
          {agent.dropped.map((d) => (
            <div className="tl-dropped-line" key={d.mechanism}>
              <Chip tone="warn">dropped</Chip>
              <span>{droppedLine(agent.role, d)}</span>
            </div>
          ))}
          <span className="muted small">
            RECAP removed these claims before the verifier saw them: a role's contract lists the mechanisms it may conclude.
          </span>
        </div>
      ) : null}

      <div className="tl-section">
        <h4 className="mini-title">Evidence it recorded{agent.evidence.length ? ` (${agent.evidence.length})` : ""}</h4>
        {agent.evidence.length ? (
          <ul className="tl-ev-list">
            {agent.evidence.map((ev) => (
              <li key={ev.id}>
                <EvidenceChip ev={ev} ctx={ctx} showAgent={false} />
                {ev.sourceRef ? (
                  <code className="mono truncate" title={ev.sourceRef}>
                    {ev.sourceRef}
                  </code>
                ) : null}
                {ev.sourceType === "doc" ? <Chip title="DOC evidence: the verifier never counts it as proof">doc</Chip> : null}
                {ev.flags.map((flag) => (
                  <FlagChip key={flag} flag={flag} />
                ))}
              </li>
            ))}
          </ul>
        ) : (
          <span className="muted small">{agent.status === "running" ? "none yet" : "none in this round"}</span>
        )}
      </div>

      {agent.searches.length ? (
        <div className="tl-section">
          <h4 className="mini-title">Runbook searches ({agent.searches.length})</h4>
          {agent.searches.map((search) => (
            <SearchView key={search.id} search={search} ctx={ctx} />
          ))}
        </div>
      ) : null}

      {agent.hasFinding ? (
        <div className="tl-section">
          <h4 className="mini-title">Citations: runbook chunks it saw</h4>
          {agent.docRefs === null ? (
            <span className="muted small">not recorded</span>
          ) : agent.docRefs.length ? (
            <span className="chip-row">
              {agent.docRefs.map((id) =>
                ctx.overlayIds.has(id) ? (
                  <span key={id} className="chip-row">
                    <Chip mono tone="warn">
                      {id}
                    </Chip>
                    <OverlayChip />
                  </span>
                ) : (
                  <Chip key={id} mono>
                    {id}
                  </Chip>
                ),
              )}
            </span>
          ) : (
            <span className="muted small">none</span>
          )}
        </div>
      ) : null}

      {summary.said || summary.notes.length ? (
        <div className="tl-section">
          <h4 className="mini-title">Its summary</h4>
          {summary.said ? <Said text={summary.said} author={ctx.author} mixed={!summary.sure} /> : null}
          {summary.notes.map((note, i) => (
            <RecapNote key={i} title="Added to the summary by RECAP (a correction may quote a value the model wrote).">
              {note}
            </RecapNote>
          ))}
        </div>
      ) : null}

      {agent.error ? (
        <div className="tl-error">
          <Icon name="alert" size={14} />
          <Mono>{agent.error}</Mono>
        </div>
      ) : null}
    </div>
  );
}

/** RECAP's leads: where the verifier said the next round should look (trusted RECAP text). */
function Leads({ leads, title }: { leads: string[]; title: string }) {
  return (
    <div className="tl-leads">
      <span className="tl-leads-title">
        {title}{" "}
        <span className="trust-tag trust-trusted" title="Written by RECAP's verifier from checked values: not a disagreement, and it settles nothing.">
          RECAP's text
        </span>
      </span>
      <ul className="tl-questions">
        {leads.map((lead, i) => (
          <li key={i}>{lead}</li>
        ))}
      </ul>
    </div>
  );
}

/** The verifier's open questions and leads this round was given (trusted RECAP text). */
function RoundQuestions({ round }: { round: RoundView }) {
  if (round.questions === null) {
    return (
      <>
        <div className="muted small">The verifier's open questions: not recorded (runs before M3 do not record them).</div>
        {round.leads.length ? <Leads leads={round.leads} title="RECAP's leads for this round" /> : null}
      </>
    );
  }
  if (!round.questions.length) {
    return round.leads.length ? (
      <Notice tone="info" icon="search" title="RECAP's leads for this round">
        <Leads leads={round.leads} title="Where the verifier said to look next" />
      </Notice>
    ) : null;
  }
  const askedOf = round.agents.filter((a) => sameQuestions(a.questions, round.questions)).map((a) => a.role);
  return (
    <Notice
      tone={round.followUp ? "warn" : "info"}
      icon="search"
      title={round.followUp ? "Follow-up round: the verifier's open questions" : "The verifier's open questions for this round"}
    >
      <ul className="tl-questions">
        {round.questions.map((q, i) => (
          <li key={i}>{q}</li>
        ))}
      </ul>
      <div className="muted small">
        RECAP's own text, built from checked values only{askedOf.length ? ` · asked of ${askedOf.join(", ")}` : ""}
        {round.followUp && round.ruleId ? ` · routed by ${round.ruleId}` : ""}
      </div>
      {round.leads.length ? <Leads leads={round.leads} title="RECAP's leads for this round" /> : null}
    </Notice>
  );
}

function IndependentViews({ round }: { round: RoundView }) {
  if (round.independent === true) {
    return (
      <Notice tone="info" icon="users" title="Independent views">
        Each specialist saw what was known before this round plus its own results, never the other's evidence while the round ran;
        the verifier then read all of it. Evidence recorded in this round gets per-agent IDs, so each item says who recorded it:{" "}
        {round.agents.map((a, i) => (
          <span key={a.role}>
            {i ? ", " : ""}
            <Mono nowrap>…{firstIdSuffix(round.round, a.role)}</Mono> is {shortRole(a.role)}'s first
          </span>
        ))}
        .
      </Notice>
    );
  }
  if (round.independent === null && round.agents.every((a) => a.status !== "running")) {
    return <div className="muted small">Independent views: not recorded (runs before M3 do not record them).</div>;
  }
  return null;
}

/** The verifier's report right after this round (trusted RECAP text). */
function VerifierAfter({ round, step }: { round: number; step: VerifierStep }) {
  return (
    <div className="tl-after">
      <div className="tl-after-head">
        <h4 className="mini-title">The verifier after round {round}</h4>
        <HypothesisChip status={step.status} />
        <span>{step.summary || "—"}</span>
      </div>
      {step.disagreements.length ? (
        <ul className="tl-disagreements">
          {step.disagreements.map((d, i) => (
            <li key={i}>
              {d.resolved ? <Chip tone="ok">settled</Chip> : <Chip tone="warn">open</Chip>}
              {d.resolved
                ? d.ruleIds.map((id) => (
                    <Chip key={id} mono title={ruleNote(id)}>
                      {id}
                    </Chip>
                  ))
                : null}
              <span>
                {humanize(d.kind)}
                {d.claims.length ? `: ${d.claims.join(" vs ")}` : ""}
              </span>
              {d.resolution ? <span className="muted">{d.resolution}</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
      {step.leads.length ? <Leads leads={step.leads} title="RECAP's leads for the next round" /> : null}
    </div>
  );
}

function InvestigationRound({ round, ctx }: { round: RoundView; ctx: ViewContext }) {
  const p = (round.routing?.payload ?? {}) as RoutingPayload;
  return (
    <div className="round">
      <div className="round-head">
        <span className="round-title">Round {round.round}</span>
        <Chip tone="accent" mono>
          {round.mode}
        </Chip>
        {round.ruleId ? <Mono>{round.ruleId}</Mono> : <span className="muted">no routing decision recorded</span>}
        {round.followUp ? (
          <Chip tone="warn" title="The last round left claims that conflict (level root causes, or different bad releases) and no evidence rule settled them (the router's disagreement signal, whichever rule fired)">
            follow-up for an open disagreement
          </Chip>
        ) : null}
        {round.judgedBy.kind === "earlier" ? (
          <Chip title={`Fix ${round.judgedBy.fix} did not recover the system, so RECAP started over after it: later verifications judged only newer findings.`}>
            before fix {round.judgedBy.fix}
          </Chip>
        ) : null}
        {round.escalation ? <Chip title="The previous round was inconclusive, or an earlier fix failed">escalated</Chip> : null}
        <span className="chip-row">
          {arr<string>(p.agents).map((a) => (
            <Chip key={a} mono>
              {a}
            </Chip>
          ))}
        </span>
        {p.router ? <span className="muted">router {p.router}</span> : null}
      </div>
      {round.reason ? <p className="round-reason">{round.reason}</p> : null}
      <div className="tl-round">
        {round.focusServices.length ? (
          <div className="muted small">
            focus services: <span className="mono">{round.focusServices.join(", ")}</span>
          </div>
        ) : null}
        <RoundQuestions round={round} />
        <IndependentViews round={round} />
        {round.agents.length ? (
          <div className={round.agents.length > 1 ? "tl-agents" : "tl-agents single"}>
            {round.agents.map((agent) => (
              <AgentPanel key={agent.role} agent={agent} round={round} ctx={ctx} />
            ))}
          </div>
        ) : (
          <span className="muted">No agent activity recorded for this round.</span>
        )}
        {round.verifier ? <VerifierAfter round={round.round} step={round.verifier} /> : null}
        {round.routing ? <RoutingDetails event={round.routing} orchestration={round.orchestration} /> : null}
      </div>
    </div>
  );
}

/** Who wrote the text below, and which verification the claims' verdicts come from. */
function RoundsIntro({ record, rounds, ctx }: { record: RunRecord | null; rounds: RoundView[]; ctx: ViewContext }) {
  if (!record) {
    return (
      <div className="tl-intro">
        <span className="muted">Claims and their verdicts appear once the run record loads.</span>
      </div>
    );
  }
  const report = record.verification;
  const epochs = rounds.some((r) => r.judgedBy.kind === "earlier");
  return (
    <div className="tl-intro">
      <div>
        Claims, confidence notes, summaries and search queries were written by <strong>{ctx.author.label}</strong>: untrusted data,
        shown as text and never followed (a note RECAP added to a summary is shown apart, as RECAP's). Questions, leads and verdicts
        are RECAP's own text. Rule lines are RECAP's sentences too, but one can repeat a value a claim named (its release version),
        which the agent wrote: such a line is tagged.
      </div>
      <div className="chip-row">
        <span className="muted">
          Each claim's verdict comes from the last verification that read it
          {epochs ? " (for a claim from before a fix that did not recover the system: the verification before that fix)" : ""}. The{" "}
          {ctx.final ? "final verification" : "latest verification (the run is still going)"}:
        </span>
        {report ? (
          <>
            <HypothesisChip status={report.status} />
            <span>{report.summary || "—"}</span>
          </>
        ) : (
          <span className="muted">none recorded yet</span>
        )}
      </div>
    </div>
  );
}

function TimelineView({ runId, running }: { runId: string; running: boolean }) {
  const events = usePolling(
    () => api.events(runId),
    (list) => (list?.some((e) => e.category === "run.summary") || !running ? null : 1500),
    [runId],
  );
  // The run record and evidence registry: findings, the final verification, who recorded what.
  const detail = usePolling(
    () => api.run(runId),
    (d) => (d?.status === "running" || (running && !d?.record) ? 2000 : null),
    [runId],
  );
  // A run that just started may appear in /runs a moment later: restart polling then.
  const { refresh } = events;
  const { refresh: refreshDetail } = detail;
  useEffect(() => {
    if (running) {
      refresh();
      refreshDetail();
    }
  }, [running, refresh, refreshDetail]);
  const list = useMemo(() => events.data ?? [], [events.data]);
  const model = useMemo(() => buildTimeline(list), [list]);
  const record = detail.data?.record ?? null;
  const evidence = detail.data ? detail.data.evidence : null;
  const rounds = useMemo(() => buildRounds({ events: list, record, evidence, running }), [list, record, evidence, running]);
  const orchestration = list.filter((e) => e.category === "orchestration.summary");

  if (events.error && !events.data) return <ErrorState error={events.error} />;
  if (!events.data) return <Loading label="Loading trace events…" />;
  if (!model) return <EmptyState title="This run has no trace events yet" icon="timeline" />;

  // Red-team knowledge overlays: the chunks from them, in RECAP's searches or the agents' own.
  const overlays = knowledgeOverlays(record?.versions);
  const ctx: ViewContext = {
    runId,
    author: textAuthor(record?.provider_info),
    final: !!record && isTerminal(record.state),
    multiAgent: rounds.some((r) => r.agents.length > 1),
    overlayIds: overlayChunkIds(searchesOf(list), record?.retrieved_docs ?? [], overlays),
  };
  const agentLanes = model.lanes.filter((l) => l !== "controller");
  const parallelRounds = rounds.filter((r) => r.mode === "parallel").length;
  const bestOverlap = Math.max(0, ...orchestration.map((e) => num(e.payload.overlap_ms) ?? 0));
  const approvalWait = model.phases
    .filter((p) => p.state === "AWAITING_APPROVAL")
    .reduce((sum, p) => sum + (p.end - p.start), 0);
  const searches = model.bars.filter((b) => b.local).length;

  return (
    <>
      <div className="stat-grid">
        <StatTile
          label="Run duration"
          value={fmtMs(model.total)}
          note={approvalWait > 0 ? `includes ${fmtMs(approvalWait)} waiting for approval` : "first to last trace event"}
        />
        <StatTile
          label="Tool calls"
          value={fmtInt(model.bars.length)}
          note={`${model.bars.filter((b) => b.status === "failed").length} failed${searches ? ` · ${searches} answered by RECAP's runbook index` : ""}`}
        />
        <StatTile label="Agents" value={fmtInt(agentLanes.length)} note={agentLanes.map(roleLabel).join(", ") || "none"} />
        <StatTile label="Parallel rounds" value={fmtInt(parallelRounds)} tone={parallelRounds ? "accent" : "neutral"} />
        <StatTile
          label="Concurrent agent time"
          value={bestOverlap > 0 ? fmtMs(bestOverlap) : "—"}
          tone={bestOverlap > 0 ? "ok" : "neutral"}
          note="overlap_ms from orchestration.summary"
        />
      </div>

      <Card
        title="Agent timeline"
        subtitle="x = time since the incident was created · one lane per agent · bars are tool calls (tool.call paired with tool.result by invocation_id): MCP tools, and runbook searches RECAP's own index answers"
      >
        {model.bars.length === 0 && model.spans.length === 0 ? (
          <Notice tone="info" title="No agent activity recorded yet">
            Tool calls appear here as soon as the investigators start.
          </Notice>
        ) : null}
        <AgentGantt model={model} author={ctx.author} />
      </Card>

      <Card
        title="Investigation rounds"
        subtitle="Per round: why the router chose it, the verifier's open questions, what each agent concluded and recorded, and how the verification judged each claim"
        actions={record ? <OriginChip provider={record.provider_info} /> : undefined}
      >
        {detail.error && !detail.data ? (
          <Notice tone="warn" icon="alert" title="The run record did not load">
            {detail.error.message} Claims, verdicts and who recorded each piece of evidence need it; what follows comes from the trace
            only.
          </Notice>
        ) : null}
        {overlays.length ? (
          <Notice tone="warn" icon="alert" title={`This run indexed extra knowledge folders: ${overlays.join(", ")} (red-team)`}>
            Runbook sections from them may be deliberately wrong; they are marked <OverlayChip /> in the agents' citations and
            searches below. A runbook never proves a claim: the verifier reads only live tool results.
          </Notice>
        ) : null}
        {rounds.length === 0 ? (
          <span className="muted">No routing decision recorded yet.</span>
        ) : (
          <>
            <RoundsIntro record={record} rounds={rounds} ctx={ctx} />
            {rounds.map((round) => (
              <InvestigationRound key={round.round} round={round} ctx={ctx} />
            ))}
          </>
        )}
      </Card>
    </>
  );
}

export function AgentTimelinePage({ route }: PageProps) {
  const selection = useRunSelection(route);
  return (
    <>
      <PageHeader
        title="Agent Timeline"
        description="When each agent ran, which tools it called, and what it concluded in each round. In a parallel round the specialists' bars overlap in time: that overlap is measured, not drawn for effect."
      />
      <RunScope selection={selection} page="timeline">
        {(runId, summary) => <TimelineView runId={runId} running={!!summary && !isTerminal(summary.state)} />}
      </RunScope>
    </>
  );
}
