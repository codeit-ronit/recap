import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent, ReactNode } from "react";
import { api } from "../api";
import { Chip, FlagChip, HypothesisChip, Mono, VerdictChip } from "../components/Badge";
import { Card, Notice, PageHeader, Segmented } from "../components/Card";
import { DataSourceBadge, summarySource } from "../components/DataSourceBadge";
import type { SummarySourceFields } from "../components/DataSourceBadge";
import { FilterBar, applyFilters, filterOptions } from "../components/FilterBar";
import type { FilterField, FilterSpec } from "../components/FilterBar";
import { Icon } from "../components/Icon";
import { LogView } from "../components/LogView";
import { Pager, pageWindow } from "../components/Pager";
import { RunScope } from "../components/RunPicker";
import { EmptyView, PolledView } from "../components/StateViews";
import { StateToken, StatusToken } from "../components/StatusToken";
import { UntrustedText } from "../components/Untrusted";
import type {
  CheckView,
  EvidenceItem,
  EvidenceView,
  PolicyVerdictView,
  ProposalCheckView,
  ProposalView,
  TextAuthor as AuthorKind,
  VerificationView,
} from "../dto";
import { findingOutcomeLabel, findingOutcomeTone, fmtDateTime, fmtInt, fmtTime, isTerminal, riskTone, stateLabel } from "../format";
import { PAGE_LABELS, hrefFor, navigate, usePolling, useRunSelection } from "../hooks";
import type { PageProps } from "../hooks";
import type { RunSummary } from "../types";
import { liveTargetWords, summaryTarget, targetFacts, targetPlace } from "../view/lab";
import type { TargetFacts, TargetFields } from "../view/lab";
import {
  EVIDENCE_KIND_LABELS,
  NOT_RECORDED,
  anyRuleNote,
  authorFromKind,
  causalChain,
  claimDetails,
  decidingRule,
  disagreementBadge,
  disagreementKind,
  disagreementsOf,
  domId,
  evidenceKind,
  isCheckedDetail,
  isCheckedValue,
  isServiceName,
  isUntrustedClass,
  openQuestionsOf,
  policyVerdictsOf,
  prettyJson,
  proposalChecksOf,
  provenanceOf,
  quotedRuleLines,
  readDisagreement,
  recordsDisagreements,
  registryUse,
  registryUseKey,
  ruleIdsOf,
  runAuthorKind,
  splitEvidenceIds,
  splitReport,
  trustClassLabel,
} from "../view/verification";
import type { ChainAgent, ChainClaim, ChainRound, DisagreementEntry, EvidenceKindKey, RegistryUse } from "../view/verification";
import { ruleTitle } from "../view/explain";
import "../styles/evidence-m3.css";

/**
 * Evidence & Decisions (#/evidence?run=<run id>&ev=<evidence id>), read from GET /runs/{id}/evidence:
 *
 *   1. the decision: the supported diagnosis, apart from the claims that were not accepted;
 *   2. the causal chain: each round's claims per agent, with the verdict of RECAP's evidence
 *      rules after that round, its leads and open questions;
 *   3. disagreements between agents and the rule that settled each one (rules, never votes);
 *   4. the fix drafted for the diagnosis, the evidence gate's check and the policy verdict;
 *   5. the evidence registry: every observation, expandable to its original text.
 *
 * Text an agent's provider wrote (claims, summaries, notes, a fix's rationale) and tool output
 * are untrusted: escaped text, labelled with their author. RECAP's own text (rule lines, leads,
 * resolutions, a verification's summary) is labelled as RECAP's.
 */

const PAGE_SIZES = [25, 50, 100] as const;
const FOCUS_EVENT = "recap:evidence-focus";

/** Poll while the run is live; retry slowly while nothing loaded (the API may be restarting). */
export function evidenceInterval(view: EvidenceView | undefined): number | null {
  if (!view) return 10_000;
  if (view.status === "running") return 2500;
  return view.status === "external" && !isTerminal(view.state) ? 5000 : null;
}

// --- links between the sections --------------------------------------------------------------------

function plainClick(event: ReactMouseEvent): boolean {
  return event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
}

function scrollToElement(id: string): void {
  const element = document.getElementById(id);
  if (!element) return;
  const reduce = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  element.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
  element.focus({ preventScroll: true });
}

/** An evidence id that jumps to its registry row (the ?ev= deep link); an id this run never recorded stays text. */
function EvidenceLink({
  id,
  runId,
  known,
  inline = false,
  tone = "neutral",
}: {
  id: string;
  runId: string;
  known: ReadonlySet<string>;
  inline?: boolean;
  tone?: "neutral" | "ok" | "bad";
}) {
  if (!known.has(id)) {
    return (
      <Chip tone="bad" mono title="Cited, but this run recorded no observation with this id">
        <Icon name="x" size={12} />
        {id}
        <span className="ed-chip-note">not in this run</span>
      </Chip>
    );
  }
  return (
    <a
      className={inline ? "ed-evlink-inline mono" : `chip tone-${tone} mono ed-evlink`}
      href={hrefFor("evidence", { run: runId, ev: id })}
      title={`Show ${id} in the evidence registry`}
      onClick={(event) => {
        event.stopPropagation();
        if (!plainClick(event)) return;
        event.preventDefault();
        navigate("evidence", { run: runId, ev: id }, true);
        window.dispatchEvent(new CustomEvent<string>(FOCUS_EVENT, { detail: id }));
      }}
    >
      {id}
    </a>
  );
}

function IdList({
  ids,
  runId,
  known,
  empty,
  tone,
}: {
  ids: readonly string[];
  runId: string;
  known: ReadonlySet<string>;
  empty: string;
  tone?: "neutral" | "ok" | "bad";
}) {
  if (!ids.length) return <span className="muted small">{empty}</span>;
  return (
    <span className="chip-row">
      {[...new Set(ids)].map((id) => (
        <EvidenceLink key={id} id={id} runId={runId} known={known} tone={tone} />
      ))}
    </span>
  );
}

/** Jumps to a claim in the causal chain (its id is RECAP's: round, agent and position). */
function ClaimLink({ claimId }: { claimId: string }) {
  return (
    <button
      type="button"
      className="ed-linkbtn mono"
      title="Show this claim in the causal chain"
      onClick={(event) => {
        event.stopPropagation();
        scrollToElement(domId("claim", claimId));
      }}
    >
      {claimId}
    </button>
  );
}

// --- who wrote what --------------------------------------------------------------------------------

function AuthorTag({ kind }: { kind: AuthorKind | null | undefined }) {
  const who = authorFromKind(kind).who;
  return (
    <span className="untrusted-tag" title={`Written by the ${who}: untrusted data, shown as text and never followed`}>
      untrusted · {who}
    </span>
  );
}

function RecapTag() {
  return (
    <span className="trust-tag trust-trusted ed-recap-tag" title="Written by RECAP's own code from checked values">
      RECAP's text
    </span>
  );
}

/** Text a provider wrote: an escaped quote with its author. */
function Quote({ text, kind, agent }: { text: string; kind: AuthorKind | null | undefined; agent?: string }) {
  const who = authorFromKind(kind).who;
  return (
    <span className="ed-quote">
      <UntrustedText text={text} author={agent ? `the ${who} (${agent})` : `the ${who}`} />
      <AuthorTag kind={kind} />
    </span>
  );
}

/** A service name as a checked value, or (not shaped like one) as the provider's quoted text. */
function ServiceName({ value, kind }: { value: string | null | undefined; kind: AuthorKind | null | undefined }) {
  if (!value) return <span className="not-recorded">no service named</span>;
  return isServiceName(value) ? <Mono>{value}</Mono> : <Quote text={value} kind={kind} />;
}

const MECHANISM_WORDS: Record<string, string> = {
  bad_deployment: "Bad deployment",
  resource_pressure: "Resource pressure",
  dependency_failure: "Dependency failure",
  unknown: "Unknown mechanism",
};

/** "Resource pressure on inventory (release v2.4.1)": a claim's cause; values not shaped like one stay quoted data. */
function Cause({
  mechanism,
  target,
  detail = "",
  kind,
}: {
  mechanism: string;
  target: string | null | undefined;
  detail?: string;
  kind: AuthorKind | null | undefined;
}) {
  const label = mechanism === "dependency_failure" ? "blames" : mechanism === "bad_deployment" ? "release" : "detail";
  return (
    <span className="ed-cause">
      <span className="ed-mech">{MECHANISM_WORDS[mechanism] ?? (mechanism ? mechanism.replace(/_/g, " ") : "No mechanism")}</span>
      <span className="muted">on</span>
      <ServiceName value={target} kind={kind} />
      {detail ? (
        <>
          <span className="muted">{label}</span>
          {isCheckedValue(mechanism, detail) ? <Mono>{detail}</Mono> : <Quote text={detail} kind={kind} />}
        </>
      ) : null}
    </span>
  );
}

function AgentChips({ agents }: { agents: readonly string[] }) {
  if (!agents.length) return <span className="not-recorded">{NOT_RECORDED}</span>;
  return (
    <span className="chip-row">
      {agents.map((agent, i) => (
        <Chip key={`${agent}-${i}`} mono>
          {agent}
        </Chip>
      ))}
    </span>
  );
}

function Line({ label, children, title }: { label: string; children: ReactNode; title?: string }) {
  return (
    <div className="ed-line">
      <span className="ed-label" title={title}>
        {label}
      </span>
      <span className="ed-line-value">{children}</span>
    </div>
  );
}

// --- RECAP's rule lines -------------------------------------------------------------------------------

function RuleChips({ ids, deciding }: { ids: readonly string[]; deciding?: string | null }) {
  if (!ids.length) return null;
  return (
    <span className="chip-row ed-rule-ids">
      {ids.map((id, i) => (
        <Chip key={`${id}-${i}`} mono tone={id === deciding ? "accent" : "neutral"} title={anyRuleNote(id) ?? ruleTitle(id)}>
          {id}
          {id === deciding ? <span className="ed-chip-note">deciding</span> : null}
        </Chip>
      ))}
    </span>
  );
}

/** A line of RECAP's text with the evidence ids it names turned into links to the registry. */
function LinkedLine({ line, runId, known }: { line: string; runId: string; known: ReadonlySet<string> }) {
  return (
    <>
      {splitEvidenceIds(line, known).map((part, i) =>
        "id" in part ? <EvidenceLink key={i} id={part.id} runId={runId} known={known} inline /> : <Fragment key={i}>{part.text}</Fragment>,
      )}
    </>
  );
}

function RuleLines({
  check,
  runId,
  known,
  details,
  show = "all",
}: {
  check: CheckView;
  runId: string;
  known: ReadonlySet<string>;
  details?: Record<string, unknown>;
  show?: "all" | "failed";
}) {
  const quotes = quotedRuleLines(check, details);
  const passed = show === "failed" ? [] : check.passed;
  if (!passed.length && !check.failed.length) return null;
  return (
    <ul className="ed-rules" aria-label="RECAP's rule lines">
      {passed.map((line, i) => (
        <li key={`p${i}`} className="ed-rule is-ok">
          <Icon name="check" size={13} />
          <span>
            <span className="sr-only">Passed: </span>
            <LinkedLine line={line} runId={runId} known={known} />
          </span>
        </li>
      ))}
      {check.failed.map((line, i) => (
        <li key={`f${i}`} className="ed-rule is-bad">
          <Icon name="x" size={13} />
          <span>
            <span className="sr-only">Failed: </span>
            <LinkedLine line={line} runId={runId} known={known} />
            {quotes[i] ? (
              <span className="untrusted-tag ed-inline-tag" title="This rule line repeats a value the claim named as it was written: that part is the provider's text">
                quotes the claim
              </span>
            ) : null}
          </span>
        </li>
      ))}
    </ul>
  );
}

// --- run bar and notices --------------------------------------------------------------------------

/** Where the run's observations were read: a live cluster by name and namespace, or the simulated shop. */
function TargetValue({ target }: { target: TargetFacts }) {
  if (target.state === "unknown") return <span className="not-recorded">{NOT_RECORDED}</span>;
  const place = targetPlace(target);
  return (
    <span className="ed-target">
      <StatusToken
        size="sm"
        tone={target.state === "live" ? "bad" : "warn"}
        icon={target.state === "live" ? "alert" : "flask"}
        label={target.state === "live" ? `live · ${target.kind || "kind not reported"}` : "simulated"}
        title={target.description || undefined}
      />
      {place ? <span className="ed-fact-note">{place}</span> : null}
    </span>
  );
}

function RunBar({
  view,
  summary,
  kind,
  target,
}: {
  view: EvidenceView;
  summary: RunSummary | undefined;
  kind: AuthorKind | null;
  target: TargetFacts;
}) {
  // GET /runs sends the typed M4 summary (provider class, environment): the badge reads only those fields.
  const source = summary ? summarySource(summary as unknown as SummarySourceFields) : null;
  const flagged = view.registry.filter((e) => e.flags.includes("prompt_injection_suspected")).length;
  const claims = view.findings.reduce((n, f) => n + f.claims.length, 0);
  const agents = new Set(view.findings.map((f) => f.agent)).size;
  const rounds = new Set(view.findings.map((f) => f.round)).size;
  const report = view.final_report;
  const fix = view.proposals[view.proposals.length - 1];
  const fixVerdicts = fix ? policyVerdictsOf(view.policy_verdicts, fix.action) : [];
  const fixVerdict = fixVerdicts[fixVerdicts.length - 1];
  return (
    <section className="ed-runbar" aria-label="This run at a glance">
      <div className="ed-runbar-badges">
        {source ? <DataSourceBadge provider={source.provider} environment={source.environment} label="Where this run's decisions came from" /> : null}
        <StateToken state={view.state} />
        {view.status === "running" ? <StatusToken tone="accent" icon="clock" label="Running · refreshing" /> : null}
        {!view.trace_available ? (
          <StatusToken tone="neutral" icon="minus" label="Trace not recorded" title="Only the run record exists: no per-round verification history" />
        ) : null}
      </div>
      <dl className="ed-facts">
        <div>
          <dt>Observations</dt>
          <dd>
            {fmtInt(view.registry.length)}
            {flagged ? <span className="ed-fact-note"> · {fmtInt(flagged)} flagged</span> : null}
          </dd>
        </div>
        <div>
          <dt>Claims</dt>
          <dd>
            {fmtInt(claims)}
            {claims ? (
              <span className="ed-fact-note">
                {" "}
                · {fmtInt(agents)} agent{agents === 1 ? "" : "s"}, {fmtInt(rounds)} round{rounds === 1 ? "" : "s"}
              </span>
            ) : null}
          </dd>
        </div>
        <div>
          <dt>Final verdict</dt>
          <dd>{report ? <HypothesisChip status={report.status} /> : <span className="not-recorded">{view.status === "running" ? "not yet" : NOT_RECORDED}</span>}</dd>
        </div>
        <div>
          <dt>Fix proposed</dt>
          <dd>
            {fix ? (
              <span className="ed-fact-fix">
                <Mono>{fix.action}</Mono> <span className="muted">on</span> <ServiceName value={fix.target} kind={kind} />
                {fixVerdict ? <VerdictChip verdict={fixVerdict.verdict} /> : null}
              </span>
            ) : (
              <span className="muted">none</span>
            )}
          </dd>
        </div>
        <div className="ed-fact-target">
          <dt>Observed on</dt>
          <dd>
            <TargetValue target={target} />
          </dd>
        </div>
      </dl>
    </section>
  );
}

function RunNotices({ view, runId, known }: { view: EvidenceView; runId: string; known: ReadonlySet<string> }) {
  const flags = view.injection_flags;
  return (
    <>
      {flags.length ? (
        <Notice tone="warn" icon="alert" title={`${flags.length === 1 ? "1 observation" : `${flags.length} observations`} flagged as possible prompt injection`}>
          <p className="ed-notice-text">
            RECAP's detector found instruction-like text in tool output. It is shown on this page as data and never followed.
          </p>
          <ul className="ed-flag-list">
            {flags.map((flag) => (
              <li key={flag.seq}>
                <EvidenceLink id={flag.evidence_id} runId={runId} known={known} tone="neutral" />
                {flag.tool ? <span className="muted small">from {flag.tool}</span> : null}
                {flag.patterns.map((pattern, i) => (
                  <Chip key={`${pattern}-${i}`} tone="warn" mono title="The pattern RECAP's detector matched">
                    {pattern}
                  </Chip>
                ))}
              </li>
            ))}
          </ul>
        </Notice>
      ) : null}
      {view.unknown_cited_ids.length ? (
        <Notice tone="bad" icon="x" title="Cited evidence ids this run never recorded">
          <p className="ed-notice-text">
            A claim or a fix cited these ids, but no observation with them exists in this run. The rules do not accept such a
            citation (V1-structural for a claim, VP1-evidence-exists for a fix).
          </p>
          <span className="chip-row">
            {view.unknown_cited_ids.map((id, i) => (
              <Chip key={`${id}-${i}`} tone="bad" mono>
                {id}
              </Chip>
            ))}
          </span>
        </Notice>
      ) : null}
      {view.not_recorded.length ? (
        <Notice tone="info" icon="info" title="Not recorded for this run">
          <ul className="plain-list ed-plain">
            {view.not_recorded.map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </Notice>
      ) : null}
    </>
  );
}

// --- 1. the decision ----------------------------------------------------------------------------------

function CheckCard({
  check,
  runId,
  known,
  kind,
  diagnosis = false,
}: {
  check: CheckView;
  runId: string;
  known: ReadonlySet<string>;
  kind: AuthorKind | null;
  diagnosis?: boolean;
}) {
  // A claim that was not accepted shows why (its failed lines); a supported one shows every line.
  const reasonsOnly = check.status !== "supported" && check.failed.length > 0;
  return (
    <article className={diagnosis ? "ed-check is-diagnosis" : "ed-check"}>
      <div className="ed-check-head">
        <HypothesisChip status={check.status} />
        {diagnosis ? <StatusToken tone="ok" size="sm" icon="check" label="Diagnosis" /> : null}
        <Cause mechanism={check.mechanism} target={check.target} detail={check.detail} kind={kind} />
      </div>
      <Quote text={check.claim} kind={kind} />
      <Line label="Proposed by">
        <AgentChips agents={check.proposers} />
        {check.proposers.length > 1 ? <span className="muted small">the same claim: their evidence was pooled</span> : null}
      </Line>
      <RuleLines check={check} runId={runId} known={known} show={reasonsOnly ? "failed" : "all"} />
      <RuleChips ids={check.rule_ids} />
      {check.status === "supported" ? (
        <Line label="Relied on" title="The evidence the passing rules relied on (basis_evidence_ids)">
          <IdList ids={check.basis_evidence_ids} runId={runId} known={known} tone="ok" empty="none recorded" />
        </Line>
      ) : null}
      {check.contradicting_evidence_ids?.length ? (
        <Line label="Cited against">
          <IdList ids={check.contradicting_evidence_ids} runId={runId} known={known} tone="bad" empty="none" />
        </Line>
      ) : null}
    </article>
  );
}

function DecisionCard({ view, runId, known, kind }: { view: EvidenceView; runId: string; known: ReadonlySet<string>; kind: AuthorKind | null }) {
  const report = view.final_report;
  const title = "The decision";
  const subtitle = "What RECAP's evidence rules concluded in the final verification: the supported root cause, kept apart from the claims they did not accept.";
  if (!report) {
    const running = view.status === "running";
    return (
      <Card title={title} subtitle={subtitle}>
        <EmptyView
          title={running ? "Not judged yet" : "No verification recorded"}
          icon="shield"
          reason={
            running
              ? "RECAP's evidence verifier judges the agents' claims after each investigation round; this run has not finished one yet."
              : isTerminal(view.state)
                ? `This run ended (${stateLabel(view.state)}) before the evidence verifier judged any claim, so it has no diagnosis.`
                : "The server reports no verification for this run."
          }
        />
      </Card>
    );
  }
  const split = splitReport(report);
  return (
    <Card title={title} subtitle={subtitle}>
      <div className="ed-verdict">
        <HypothesisChip status={report.status} />
        <span className="ed-verdict-text">{report.summary || NOT_RECORDED}</span>
        <RecapTag />
      </div>
      <div className="ed-split">
        <section className="ed-pane is-ok" aria-labelledby="ed-diagnosis-title">
          <h3 className="ed-pane-title" id="ed-diagnosis-title">
            <Icon name="check" size={15} /> Supported diagnosis
          </h3>
          <p className="ed-help">The root cause the rules support (supported, never "proven"). A fix is drafted only for this claim.</p>
          {split.diagnosis ? (
            <CheckCard check={split.diagnosis} runId={runId} known={known} kind={kind} diagnosis />
          ) : report.best ? (
            <article className="ed-check is-diagnosis">
              <div className="ed-check-head">
                <StatusToken tone="ok" size="sm" icon="check" label="Diagnosis" />
                <Cause mechanism={report.best.mechanism} target={report.best.target_service} kind={kind} />
              </div>
              <Quote text={report.best.claim} kind={kind} />
              <p className="muted small">More than one check matches this claim; its rule lines are in the causal chain below.</p>
            </article>
          ) : (
            <p className="ed-none">No actionable claim passed the rules in the final verification, so there is no diagnosis.</p>
          )}
        </section>
        <section className="ed-pane is-bad" aria-labelledby="ed-rejected-title">
          <h3 className="ed-pane-title" id="ed-rejected-title">
            <Icon name="x" size={15} /> Not accepted ({fmtInt(split.notAccepted.length)})
          </h3>
          <p className="ed-help">Claims the rules found insufficient or contradicted, with the lines that decided.</p>
          {split.notAccepted.length ? (
            <div className="ed-checks">
              {split.notAccepted.map((check, i) => (
                <CheckCard key={i} check={check} runId={runId} known={known} kind={kind} />
              ))}
            </div>
          ) : (
            <p className="ed-none">Every claim in the final verification was supported.</p>
          )}
        </section>
      </div>
      {split.alsoSupported.length ? (
        <section className="ed-also" aria-labelledby="ed-also-title">
          <h3 className="ed-pane-title" id="ed-also-title">
            Also supported, not the root cause ({fmtInt(split.alsoSupported.length)})
          </h3>
          <p className="ed-help">
            Supported by the evidence but not chosen as the root cause: a dependency failure is never a fix target by itself, and V7
            prefers the deepest cause.
          </p>
          <div className="ed-checks ed-checks-grid">
            {split.alsoSupported.map((check, i) => (
              <CheckCard key={i} check={check} runId={runId} known={known} kind={kind} />
            ))}
          </div>
        </section>
      ) : null}
    </Card>
  );
}

// --- 2. the causal chain ------------------------------------------------------------------------------

function ClaimVerdict({ item, judged, running }: { item: ChainClaim; judged: boolean; running: boolean }) {
  if (item.check) return <HypothesisChip status={item.check.status} />;
  if (!judged) {
    return <StatusToken tone="neutral" size="sm" icon={running ? "clock" : "minus"} label={running ? "Not judged yet" : "No verdict recorded"} />;
  }
  return (
    <StatusToken
      tone="neutral"
      size="sm"
      icon="minus"
      label="Verdict not identified"
      title="No single check of this round's verification matches this claim"
    />
  );
}

/** The release, dependency or episode a claim names (values not shaped like one are its own text). */
function NamedDetails({ details, kind }: { details: Record<string, unknown>; kind: AuthorKind | null }) {
  const named = claimDetails(details);
  if (!named.length) return null;
  const keyOf: Record<string, string> = { release: "version", blames: "dependency", episode: "episode_id" };
  return (
    <Line label="Names">
      {named.map((d) => (
        <span key={d.label} className="ed-named">
          <span className="muted small">{d.label}</span>
          {isCheckedDetail(keyOf[d.label] ?? "", d.value) ? <Mono>{d.value}</Mono> : <Quote text={d.value} kind={kind} />}
        </span>
      ))}
    </Line>
  );
}

function ClaimItem({
  item,
  runId,
  known,
  judged,
  running,
}: {
  item: ChainClaim;
  runId: string;
  known: ReadonlySet<string>;
  judged: boolean;
  running: boolean;
}) {
  const { claim, check } = item;
  const classes = ["ed-claim", item.diagnosis ? "is-diagnosis" : "", check ? `is-${check.status}` : ""].filter(Boolean).join(" ");
  return (
    <li id={domId("claim", claim.claim_id)} tabIndex={-1} className={classes}>
      <div className="ed-claim-head">
        <ClaimVerdict item={item} judged={judged} running={running} />
        {item.diagnosis ? <StatusToken tone="ok" size="sm" icon="check" label="Diagnosis" title="The final verification's supported root cause" /> : null}
        {claim.origin === "memory" ? (
          <Chip tone="info" title="A claim from a past episode: the same rules judge it (V5-memory)">
            from memory
          </Chip>
        ) : null}
        {claim.partial ? (
          <Chip tone="warn" title="Kept from a turn a budget stop ended">
            partial
          </Chip>
        ) : null}
        <span className="ed-claim-id mono" title="Claim id (round, agent, position)">
          {claim.claim_id}
        </span>
      </div>
      <Cause mechanism={claim.mechanism} target={claim.target_service} kind={claim.author} />
      <Quote text={claim.claim} kind={claim.author} agent={claim.agent} />
      <NamedDetails details={claim.details} kind={claim.author} />
      <Line label="Cites">
        <IdList ids={claim.supporting_evidence_ids} runId={runId} known={known} empty="no evidence" />
      </Line>
      {claim.contradicting_evidence_ids.length ? (
        <Line label="Cites against">
          <IdList ids={claim.contradicting_evidence_ids} runId={runId} known={known} tone="bad" empty="none" />
        </Line>
      ) : null}
      {check ? (
        <div className="ed-judged">
          <div className="ed-judged-head">
            <span className="ed-label">RECAP's rules</span>
            <RuleChips ids={check.rule_ids} />
          </div>
          <RuleLines check={check} runId={runId} known={known} details={claim.details} />
          {check.basis_evidence_ids.length ? (
            <Line label="Relied on" title="The evidence the passing rules relied on (basis_evidence_ids)">
              <IdList ids={check.basis_evidence_ids} runId={runId} known={known} tone="ok" empty="none" />
            </Line>
          ) : null}
          {item.finalStatus ? <p className="ed-help">The final verification judged the same claim {item.finalStatus}.</p> : null}
          {item.checkSource === "final" ? <p className="ed-help">Verdict from the run record's final report: the trace has no verification for this round.</p> : null}
        </div>
      ) : null}
      {claim.confidence_note ? (
        <details className="ed-more">
          <summary>Confidence note</summary>
          <p className="ed-more-body">
            <Quote text={claim.confidence_note} kind={claim.author} agent={claim.agent} />
          </p>
        </details>
      ) : null}
    </li>
  );
}

function AgentLane({ lane, runId, known, judged, running }: { lane: ChainAgent; runId: string; known: ReadonlySet<string>; judged: boolean; running: boolean }) {
  const f = lane.finding;
  return (
    <article className="ed-lane" aria-label={`${f.agent}, round ${f.round}`}>
      <header className="ed-lane-head">
        <Chip mono tone="accent">
          {f.agent}
        </Chip>
        <StatusToken
          tone={findingOutcomeTone(f.outcome)}
          size="sm"
          variant="outline"
          label={f.outcome ? `Finished ${findingOutcomeLabel(f.outcome)}` : "Outcome not recorded"}
        />
        {f.partial ? <StatusToken tone="warn" size="sm" label="Partial" title="A budget stop ended this turn; its finding was kept" /> : null}
        <span className="muted small">
          {fmtInt(f.evidence_ids.length)} observation{f.evidence_ids.length === 1 ? "" : "s"} · {fmtTime(f.started_at)}–{fmtTime(f.finished_at)}
        </span>
      </header>
      {f.error ? (
        <p className="ed-error">
          <Icon name="alert" size={13} /> <span className="muted">Error as recorded:</span> <span>{f.error}</span>
        </p>
      ) : null}
      {f.summary ? (
        <details className="ed-more">
          <summary>Agent's summary</summary>
          <p className="ed-more-body">
            <Quote text={f.summary} kind={f.author} agent={f.agent} />
          </p>
        </details>
      ) : null}
      {lane.claims.length ? (
        <ol className="ed-claims">
          {lane.claims.map((item, i) => (
            <ClaimItem key={`${item.claim.claim_id}-${i}`} item={item} runId={runId} known={known} judged={judged} running={running} />
          ))}
        </ol>
      ) : (
        <p className="ed-none">No claim in this turn.</p>
      )}
    </article>
  );
}

function VerificationBlock({
  v,
  round,
  runId,
  known,
  kind,
  running,
}: {
  v: VerificationView | null;
  round: number | null;
  runId: string;
  known: ReadonlySet<string>;
  kind: AuthorKind | null;
  running: boolean;
}) {
  if (!v) {
    return (
      <div className="ed-verify is-missing">
        <Icon name="shield" size={15} />
        <span>{running ? "The evidence verifier has not judged this round yet." : "No verification was recorded after this round."}</span>
      </div>
    );
  }
  const disagreements = v.disagreements.map(readDisagreement);
  const questions = openQuestionsOf(v);
  const title = v.source === "run_record" ? "Final verification (run record)" : `Verification after round ${round ?? "?"}`;
  return (
    <section className="ed-verify" aria-label={title}>
      <div className="ed-verify-head">
        <Icon name="shield" size={15} />
        <span className="ed-verify-title">{title}</span>
        <HypothesisChip status={v.status} />
        <RecapTag />
      </div>
      <p className="ed-verify-summary">{v.summary || NOT_RECORDED}</p>
      {v.best ? (
        <Line label="Best supported">
          <Cause mechanism={v.best.mechanism} target={v.best.target_service} kind={kind} />
        </Line>
      ) : null}
      {disagreements.length ? (
        <Line label="Disagreements">
          <span className="ed-stack">
            {disagreements.map((d, i) => {
              const badge = disagreementBadge(d.resolved);
              return (
                <span key={i} className="chip-row">
                  <StatusToken tone={badge.tone} size="sm" icon={d.resolved ? "check" : "alert"} label={badge.label} />
                  <span>{disagreementKind(d.kind).label}</span>
                  <RuleChips ids={ruleIdsOf(d.rule_id)} />
                </span>
              );
            })}
          </span>
        </Line>
      ) : null}
      {v.leads.length ? (
        <Line label="Leads" title="Where the verifier told the next round to look (RECAP's text, built from checked values)">
          <ul className="ed-list">
            {v.leads.map((lead, i) => (
              <li key={i}>{lead}</li>
            ))}
          </ul>
        </Line>
      ) : null}
      {questions.length ? (
        <Line label="Open questions">
          <ul className="ed-list">
            {questions.map((q) => (
              <li key={q}>{q}</li>
            ))}
          </ul>
        </Line>
      ) : null}
      {v.focus_services.length ? (
        <Line label="Look next at">
          <span className="chip-row">
            {v.focus_services.map((service, i) => (
              <ServiceName key={`${service}-${i}`} value={service} kind={kind} />
            ))}
          </span>
        </Line>
      ) : null}
      {v.unknown_evidence_ids.length ? (
        <Line label="Unknown ids cited">
          <IdList ids={v.unknown_evidence_ids} runId={runId} known={known} empty="none" />
        </Line>
      ) : null}
    </section>
  );
}

function RoundBlock({ r, runId, known, kind, running }: { r: ChainRound; runId: string; known: ReadonlySet<string>; kind: AuthorKind | null; running: boolean }) {
  const titleId = `ed-round-${r.round ?? "none"}`;
  const n = r.agents.length;
  return (
    <li className="ed-round" aria-labelledby={titleId}>
      <div className="ed-round-head">
        <span className="ed-round-num" aria-hidden="true">
          {r.round ?? "?"}
        </span>
        <h3 id={titleId} className="ed-round-title">
          {r.round === null ? "Round not recorded" : `Round ${r.round}`}
        </h3>
        <span className="muted small">
          {n ? `${n} agent${n === 1 ? "" : "s"}: ${r.agents.map((a) => a.finding.agent).join(", ")}` : "no agent finding recorded"}
        </span>
      </div>
      {n ? (
        <div className={n > 1 ? "ed-lanes is-parallel" : "ed-lanes"}>
          {r.agents.map((lane, i) => (
            <AgentLane key={`${lane.finding.agent}-${i}`} lane={lane} runId={runId} known={known} judged={!!r.verification} running={running} />
          ))}
        </div>
      ) : null}
      <VerificationBlock v={r.verification} round={r.round} runId={runId} known={known} kind={kind} running={running} />
    </li>
  );
}

function ChainCard({ view, runId, known, kind }: { view: EvidenceView; runId: string; known: ReadonlySet<string>; kind: AuthorKind | null }) {
  const rounds = useMemo(() => causalChain(view), [view]);
  const running = view.status === "running";
  const actions = (
    <a className="btn btn-small btn-ghost" href={hrefFor("timeline", { run: runId })}>
      Open in {PAGE_LABELS.timeline}
      <Icon name="chevronRight" size={12} />
    </a>
  );
  return (
    <Card
      title="Causal chain"
      subtitle="Round by round: each agent's claims in its own words, then the verdict of RECAP's evidence rules on them. Evidence ids jump to the registry."
      actions={actions}
    >
      {rounds.length ? (
        <ol className="ed-rounds">
          {rounds.map((r) => (
            <RoundBlock key={String(r.round)} r={r} runId={runId} known={known} kind={kind} running={running} />
          ))}
        </ol>
      ) : (
        <EmptyView
          title={running ? "No agent has finished a turn yet" : "No claim recorded"}
          icon="users"
          reason={
            running
              ? "Claims appear here as each agent finishes its turn."
              : `No agent finished a turn in this run${isTerminal(view.state) ? ` (it ended: ${stateLabel(view.state)})` : ""}, so nothing was claimed or judged.`
          }
        />
      )}
    </Card>
  );
}

// --- 3. disagreements --------------------------------------------------------------------------------

function roundsText(entry: DisagreementEntry): string {
  const rounds = entry.rounds.map((r) => (r === null ? "?" : String(r)));
  if (rounds.length) return `recorded after round${rounds.length === 1 ? "" : "s"} ${rounds.join(", ")}`;
  return entry.final ? "recorded by the final verification" : "";
}

function DisagreementItem({ entry, runId, known }: { entry: DisagreementEntry; runId: string; known: ReadonlySet<string> }) {
  const d = entry.view;
  const kind = disagreementKind(d.kind);
  const badge = disagreementBadge(d.resolved);
  const rules = ruleIdsOf(d.rule_id);
  return (
    <article className={d.resolved ? "ed-dis" : "ed-dis is-open"}>
      <div className="ed-dis-head">
        <StatusToken tone={badge.tone} icon={d.resolved ? "check" : "alert"} label={badge.label} />
        <span className="ed-dis-kind">{kind.label}</span>
        <span className="muted small">{roundsText(entry)}</span>
      </div>
      {kind.plain ? <p className="ed-help">{kind.plain}</p> : null}
      {d.resolution ? (
        <p className="ed-resolution">
          {d.resolution} <RecapTag />
        </p>
      ) : null}
      <div className="ed-dis-grid">
        <Line label="Settled by">
          {rules.length ? <RuleChips ids={rules} /> : <span className="muted small">{d.resolved ? "no evidence rule" : "no evidence rule yet (open)"}</span>}
        </Line>
        <Line label="Claims">
          {d.claims.length ? (
            <span className="chip-row">
              {d.claims.map((c, i) => (
                <Chip key={`${c}-${i}`}>{c}</Chip>
              ))}
            </span>
          ) : (
            <span className="muted small">none</span>
          )}
        </Line>
        <Line label="Agents">
          <AgentChips agents={d.agents} />
        </Line>
        <Line label={d.resolved ? "Evidence that decided" : "Evidence in dispute"}>
          <IdList ids={d.evidence_ids} runId={runId} known={known} empty="none" />
        </Line>
      </div>
      {!d.resolved && d.question ? (
        <Notice tone="warn" icon="route" title="Question for the next round (RECAP's text)">
          {d.question}
        </Notice>
      ) : null}
    </article>
  );
}

function DisagreementsCard({ view, runId, known }: { view: EvidenceView; runId: string; known: ReadonlySet<string> }) {
  const entries = disagreementsOf(view);
  const verified = view.verifications.length > 0 || !!view.final_report;
  const recorded = recordsDisagreements(view);
  return (
    <Card
      title={`Disagreements${recorded && verified ? ` (${fmtInt(entries.length)})` : ""}`}
      subtitle="Where the agents' claims conflicted, and the evidence rule that settled each conflict. Rules decide; how many agents agree never does."
    >
      {!verified ? (
        <p className="ed-none">No verification ran, so no disagreement could be recorded.</p>
      ) : !recorded ? (
        <p className="not-recorded">Not recorded: this run was verified before RECAP recorded disagreements.</p>
      ) : entries.length ? (
        <div className="ed-dis-list">
          {entries.map((entry, i) => (
            <DisagreementItem key={i} entry={entry} runId={runId} known={known} />
          ))}
        </div>
      ) : (
        <p className="ed-none">The agents' claims did not conflict: no verification of this run recorded a disagreement.</p>
      )}
    </Card>
  );
}

// --- 4. from diagnosis to action ------------------------------------------------------------------------

function GateChecks({ checks }: { checks: ProposalCheckView[] }) {
  return (
    <section className="ed-gate" aria-label="Evidence gate">
      <h4 className="ed-gate-title">Evidence gate (VP1–VP4)</h4>
      <p className="ed-help">Before anyone is asked to approve, RECAP checks that the fix cites real evidence, targets the diagnosis and names a known action.</p>
      {checks.length ? (
        checks.map((c) => (
          <div key={c.seq} className="ed-gate-row">
            <span className="chip-row">
              <StatusToken tone={c.ok ? "ok" : "bad"} label={c.ok ? "Passed" : "Failed"} />
              {!c.enforced ? (
                <StatusToken tone="warn" size="sm" label="Not enforced" title="Safety was switched off for this run: the check was recorded, not applied" />
              ) : null}
            </span>
            <RuleChips ids={c.rule_ids} />
            {c.problems.length ? (
              <ul className="ed-rules">
                {c.problems.map((problem, i) => (
                  <li key={i} className="ed-rule is-bad">
                    <Icon name="x" size={13} />
                    <span>{problem}</span>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ))
      ) : (
        <p className="not-recorded">No evidence-gate check was recorded for this proposal.</p>
      )}
    </section>
  );
}

function PolicyVerdicts({ verdicts, tool }: { verdicts: PolicyVerdictView[]; tool: string }) {
  return (
    <section className="ed-gate" aria-label="Safety policy">
      <h4 className="ed-gate-title">Safety policy</h4>
      <p className="ed-help">The gateway's verdict from RECAP's trusted policy file; the last rule listed decided it.</p>
      {verdicts.length ? (
        verdicts.map((v, i) => (
          <div key={i} className="ed-gate-row">
            <span className="chip-row">
              <VerdictChip verdict={v.verdict} />
              {v.risk ? (
                <Chip tone={riskTone(v.risk)} title="The risk tier from RECAP's own policy table (trusted), not the proposer's">
                  risk: {v.risk}
                </Chip>
              ) : null}
            </span>
            <RuleChips ids={v.rule_ids} deciding={decidingRule(v.rule_ids)} />
            {v.reasons.length ? (
              <ul className="ed-list">
                {v.reasons.map((reason, i) => (
                  <li key={i}>{reason}</li>
                ))}
              </ul>
            ) : null}
            <span className="muted small">
              {v.caller ? `asked by ${v.caller} · ` : ""}
              {fmtDateTime(v.decided_at)}
            </span>
          </div>
        ))
      ) : (
        <p className="not-recorded">No policy verdict on {tool} was recorded.</p>
      )}
    </section>
  );
}

function ProposalBlock({ p, view, runId, known }: { p: ProposalView; view: EvidenceView; runId: string; known: ReadonlySet<string> }) {
  const params = Object.entries(p.params);
  return (
    <article className="ed-proposal">
      <div className="ed-proposal-head">
        <Icon name="tool" size={16} />
        <span className="ed-proposal-action">
          <Mono>{p.action}</Mono> <span className="muted">on</span> <ServiceName value={p.target} kind={p.author} />
        </span>
        {params.map(([key, value]) => (
          <Chip key={key} mono title="A parameter as the proposer wrote it; the gateway validates it before any call">
            {key}={typeof value === "string" ? value : JSON.stringify(value)}
          </Chip>
        ))}
        <Chip outline title="The risk the proposer declared. RECAP never trusts it: the policy uses its own risk table.">
          declared risk: {p.declared_risk}
        </Chip>
        <Chip title="Where the fix came from">source: {p.source}</Chip>
      </div>
      <div className="ed-proposal-body">
        <Line label="Rationale">{p.rationale ? <Quote text={p.rationale} kind={p.author} /> : <span className="not-recorded">{NOT_RECORDED}</span>}</Line>
        <Line label="Cites evidence">
          <IdList ids={p.evidence_ids} runId={runId} known={known} empty="none" />
        </Line>
        <Line label="Runbook citations">
          {p.citations.length ? (
            <span className="chip-row">
              {p.citations.map((c, i) => (
                <Chip key={`${c}-${i}`} mono tone="info">
                  {c}
                </Chip>
              ))}
              <a className="small" href={hrefFor("knowledge", { run: runId })}>
                See the run's searches on {PAGE_LABELS.knowledge}
              </a>
            </span>
          ) : (
            <span className="muted small">none</span>
          )}
        </Line>
        <Line label="Approval binding" title="The digest binds an approval to exactly this action, target and parameters">
          <Mono title={p.digest}>{p.digest ? `${p.digest.slice(0, 16)}…` : NOT_RECORDED}</Mono>
          <span className="muted small">proposal {p.proposal_id}</span>
        </Line>
      </div>
      <div className="ed-gates">
        <GateChecks checks={proposalChecksOf(view.proposal_checks, p.proposal_id)} />
        <PolicyVerdicts verdicts={policyVerdictsOf(view.policy_verdicts, p.action)} tool={p.action} />
      </div>
    </article>
  );
}

function ActionCard({ view, runId, known }: { view: EvidenceView; runId: string; known: ReadonlySet<string> }) {
  const tools = new Set(view.proposals.map((p) => p.action));
  const others = view.policy_verdicts.filter((v) => !tools.has(v.tool));
  const otherTools = [...new Set(others.map((v) => v.tool))];
  const actions = (
    <a className="btn btn-small btn-ghost" href={hrefFor("safety", { run: runId })}>
      Approval and write: {PAGE_LABELS.safety}
      <Icon name="chevronRight" size={12} />
    </a>
  );
  return (
    <Card
      title="From diagnosis to action"
      subtitle="The fix drafted for the diagnosis, the evidence gate's check of it and the safety policy's verdict. A person approves the exact action on Safety & Approvals."
      actions={actions}
    >
      {view.proposals.length ? (
        view.proposals.map((p) => <ProposalBlock key={p.proposal_id} p={p} view={view} runId={runId} known={known} />)
      ) : (
        <EmptyView
          title="No fix proposed"
          icon="tool"
          reason={
            view.status === "running"
              ? "RECAP drafts a fix only after the verifier supports a root cause."
              : view.final_report?.best
                ? "The run recorded a supported diagnosis but no fix proposal."
                : "No supported root cause was recorded, and no fix was proposed."
          }
        />
      )}
      {otherTools.map((tool) => (
        <PolicyVerdicts key={tool} verdicts={others.filter((v) => v.tool === tool)} tool={tool} />
      ))}
    </Card>
  );
}

// --- 5. the evidence registry ----------------------------------------------------------------------------

interface RegistryRow {
  item: EvidenceItem;
  kind: EvidenceKindKey;
  use: RegistryUse;
}

const USE_LABELS: Record<string, string> = {
  "diagnosis basis": "Diagnosis basis",
  "rule basis": "Rule basis",
  cited: "Cited by a claim",
  "not cited": "Not cited",
};

const READERS: Record<string, (row: RegistryRow) => FilterField> = {
  kind: (row) => row.kind,
  agent: (row) => row.item.recorded_by,
  round: (row) => row.item.round,
  use: (row) => registryUseKey(row.use),
};

function UseChips({ use }: { use: RegistryUse }) {
  const chips: ReactNode[] = [];
  if (use.diagnosisBasis) {
    chips.push(
      <Chip key="d" tone="ok" title="The diagnosis' passing rules relied on it">
        <Icon name="check" size={12} /> diagnosis basis
      </Chip>,
    );
  } else if (use.ruleBasis) {
    chips.push(
      <Chip key="r" tone="ok" outline title="A passing rule of some verification relied on it">
        rule basis
      </Chip>,
    );
  }
  if (use.supports.length) {
    chips.push(
      <Chip key="s" title={`Cited as support by ${use.supports.join(", ")}`}>
        supports {use.supports.length} claim{use.supports.length === 1 ? "" : "s"}
      </Chip>,
    );
  }
  if (use.contradicts.length) {
    chips.push(
      <Chip key="c" tone="bad" title={`Cited against ${use.contradicts.join(", ")}`}>
        against {use.contradicts.length} claim{use.contradicts.length === 1 ? "" : "s"}
      </Chip>,
    );
  }
  if (!chips.length) return <span className="muted small">not cited</span>;
  return <span className="chip-row">{chips}</span>;
}

function TrustToken({ trustClass, note }: { trustClass: string; note: string | undefined }) {
  const untrusted = isUntrustedClass(trustClass);
  return (
    <StatusToken
      size="sm"
      variant="outline"
      tone={untrusted ? "neutral" : "accent"}
      icon={untrusted ? "alert" : "lock"}
      label={untrusted ? `untrusted · ${trustClassLabel(trustClass)}` : trustClassLabel(trustClass)}
      title={note}
    />
  );
}

function EvidenceDetail({
  item,
  runId,
  trustNote,
  patterns,
  use,
  target,
}: {
  item: EvidenceItem;
  runId: string;
  trustNote: string | undefined;
  patterns: string[];
  use: RegistryUse;
  /** The run's own target: where a tool's answer was read. */
  target: TargetFacts;
}) {
  const formatted = useMemo(() => prettyJson(item.content), [item.content]);
  const [mode, setMode] = useState<"formatted" | "recorded">("formatted");
  const showFormatted = mode === "formatted" && formatted !== null;
  const untrusted = isUntrustedClass(item.trust_class);
  const provenance = provenanceOf(item, target);
  return (
    <div className="ed-detail">
      <div className="ed-detail-main">
        {formatted !== null ? (
          <Segmented
            value={mode}
            onChange={setMode}
            label={`How to show observation ${item.id}`}
            options={[
              { value: "formatted", label: "Formatted" },
              { value: "recorded", label: "As recorded" },
            ]}
          />
        ) : null}
        <LogView
          text={showFormatted ? formatted : item.content}
          label={`Original observation ${item.id}`}
          untrusted={untrusted ? trustClassLabel(item.trust_class) : false}
          downloadName={`${runId}-${item.id}${showFormatted ? "-formatted" : ""}`}
          initialLines={60}
          maxChars={12_000}
          maxHeight={380}
          emptyText="The observation recorded no text."
        />
        {showFormatted ? (
          <p className="ed-help">Formatted for reading: indentation added, nothing else changed. "As recorded" shows the exact text the agents read.</p>
        ) : null}
      </div>
      <dl className="kv ed-detail-meta">
        <div className="kv-row">
          <dt>Source</dt>
          <dd>
            <span className="cell-stack">
              <code className="mono ed-ref">{item.source_ref}</code>
              {provenance ? (
                <span className={target.state === "live" ? "ed-provenance is-live" : "ed-provenance"}>
                  {target.state === "live" ? <Icon name="alert" size={12} /> : null}
                  {provenance}
                </span>
              ) : null}
            </span>
          </dd>
        </div>
        <div className="kv-row">
          <dt>Recorded</dt>
          <dd>
            {item.recorded_by ? <Mono>{item.recorded_by}</Mono> : <span className="not-recorded">{NOT_RECORDED}</span>}
            {item.round !== null ? <span className="muted"> · round {item.round}</span> : null} <span className="muted">· {fmtDateTime(item.timestamp)}</span>
          </dd>
        </div>
        <div className="kv-row">
          <dt>Trust</dt>
          <dd>
            <span className="cell-stack">
              <TrustToken trustClass={item.trust_class} note={trustNote} />
              {trustNote ? <span className="muted small">{trustNote}</span> : null}
            </span>
          </dd>
        </div>
        {item.flags.length || patterns.length ? (
          <div className="kv-row">
            <dt>Flags</dt>
            <dd>
              <span className="chip-row">
                {item.flags.map((flag, i) => (
                  <FlagChip key={`${flag}-${i}`} flag={flag} />
                ))}
                {patterns.map((pattern, i) => (
                  <Chip key={`${pattern}-${i}`} tone="warn" mono title="The pattern RECAP's injection detector matched">
                    {pattern}
                  </Chip>
                ))}
              </span>
            </dd>
          </div>
        ) : null}
        <div className="kv-row">
          <dt>Cited by</dt>
          <dd>
            {use.supports.length || use.contradicts.length ? (
              <span className="cell-stack">
                {use.supports.length ? (
                  <span className="chip-row">
                    <span className="muted small">support for</span>
                    {use.supports.map((id) => (
                      <ClaimLink key={id} claimId={id} />
                    ))}
                  </span>
                ) : null}
                {use.contradicts.length ? (
                  <span className="chip-row">
                    <span className="muted small">against</span>
                    {use.contradicts.map((id) => (
                      <ClaimLink key={id} claimId={id} />
                    ))}
                  </span>
                ) : null}
              </span>
            ) : (
              <span className="muted">no claim</span>
            )}
          </dd>
        </div>
        <div className="kv-row">
          <dt>Rules</dt>
          <dd>
            {use.diagnosisBasis
              ? "The diagnosis' passing rules relied on it."
              : use.ruleBasis
                ? "A passing rule relied on it."
                : "No passing rule relied on it."}
          </dd>
        </div>
        <div className="kv-row">
          <dt>Size</dt>
          <dd>
            ≈ {fmtInt(item.token_estimate)} tokens <span className="muted small">(RECAP's estimate)</span>
          </dd>
        </div>
      </dl>
    </div>
  );
}

function offsetOf(rows: readonly RegistryRow[], id: string | null, limit: number): number {
  const index = id ? rows.findIndex((row) => row.item.id === id) : -1;
  return index > 0 ? Math.floor(index / limit) * limit : 0;
}

function Registry({
  view,
  runId,
  focusId,
  diagnosisBasis,
  target,
}: {
  view: EvidenceView;
  runId: string;
  focusId: string | null;
  diagnosisBasis: CheckView | null;
  target: TargetFacts;
}) {
  const rows = useMemo<RegistryRow[]>(
    () => view.registry.map((item) => ({ item, kind: evidenceKind(item), use: registryUse(item, diagnosisBasis) })),
    [view.registry, diagnosisBasis],
  );
  const patternsById = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const flag of view.injection_flags) map.set(flag.evidence_id, [...new Set([...(map.get(flag.evidence_id) ?? []), ...flag.patterns])]);
    return map;
  }, [view.injection_flags]);
  const [filters, setFilters] = useState<Record<string, string | null>>({});
  const [limit, setLimit] = useState<number>(PAGE_SIZES[0]);
  const [offset, setOffset] = useState(() => offsetOf(rows, focusId, PAGE_SIZES[0]));
  const [open, setOpen] = useState<ReadonlySet<string>>(() => new Set(focusId ? [focusId] : []));
  const [focused, setFocused] = useState<string | null>(focusId);
  const [scrollTarget, setScrollTarget] = useState<{ id: string; at: number } | null>(null);

  const filtered = applyFilters(rows, filters, READERS);
  const w = pageWindow(offset, limit, filtered.length);
  const page = filtered.slice(w.offset, w.offset + w.limit);

  // Show one observation: clear filters that hide it, turn to its page, open it and scroll to it.
  const focus = (id: string) => {
    if (!rows.some((row) => row.item.id === id)) return;
    let list = applyFilters(rows, filters, READERS);
    if (!list.some((row) => row.item.id === id)) {
      setFilters({});
      list = rows;
    }
    const index = list.findIndex((row) => row.item.id === id);
    setOffset(Math.floor(index / limit) * limit);
    setOpen((prev) => new Set(prev).add(id));
    setFocused(id);
    setScrollTarget({ id, at: Date.now() });
  };
  const focusRef = useRef(focus);
  focusRef.current = focus;
  useEffect(() => {
    const onFocus = (event: Event) => focusRef.current(String((event as CustomEvent<unknown>).detail));
    window.addEventListener(FOCUS_EVENT, onFocus);
    return () => window.removeEventListener(FOCUS_EVENT, onFocus);
  }, []);
  useEffect(() => {
    if (focusId) focusRef.current(focusId);
  }, [focusId]);
  useEffect(() => {
    if (scrollTarget) scrollToElement(domId("ev", scrollTarget.id));
  }, [scrollTarget]);

  const toggle = (id: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const specs: FilterSpec[] = [
    { key: "kind", label: "Kind", options: filterOptions(rows, READERS.kind, (v) => EVIDENCE_KIND_LABELS[v as EvidenceKindKey] ?? v) },
    { key: "agent", label: "Recorded by", options: filterOptions(rows, READERS.agent) },
    { key: "round", label: "Round", options: filterOptions(rows, READERS.round, (v) => `Round ${v}`) },
    { key: "use", label: "Use", options: filterOptions(rows, READERS.use, (v) => USE_LABELS[v] ?? v) },
  ].filter((spec) => spec.options.length > 1 || !!filters[spec.key]);

  if (!rows.length) {
    const running = view.status === "running";
    return (
      <EmptyView
        title={running ? "No observation yet" : "No observation recorded"}
        icon="evidence"
        reason={
          running
            ? "Observations appear here as the agents' tool calls return."
            : `This run recorded no tool result${isTerminal(view.state) ? ` before it ended (${stateLabel(view.state)})` : ""}.`
        }
      />
    );
  }

  return (
    <div className="ed-registry">
      {specs.length ? (
        <FilterBar
          filters={specs}
          values={filters}
          label="Filter the evidence registry"
          onChange={(key, value) => {
            setFilters((prev) => ({ ...prev, [key]: value }));
            setOffset(0);
          }}
          onClear={() => {
            setFilters({});
            setOffset(0);
          }}
          summary={`Showing ${fmtInt(filtered.length)} of ${fmtInt(rows.length)} observations`}
        />
      ) : null}
      <div className="table-wrap">
        <table className="table ed-table">
          <caption className="sr-only">Observations recorded by run {runId}</caption>
          <thead>
            <tr>
              <th className="col-expand">
                <span className="sr-only">Details</span>
              </th>
              <th scope="col">Evidence</th>
              <th scope="col" className="ed-col-opt">
                Source
              </th>
              <th scope="col" className="ed-col-opt">
                Recorded by
              </th>
              <th scope="col" className="ed-col-trust">
                Trust
              </th>
              <th scope="col">How the verifier used it</th>
            </tr>
          </thead>
          <tbody>
            {page.map(({ item, kind, use }) => {
              const isOpen = open.has(item.id);
              const detailId = `${domId("ev", item.id)}-detail`;
              const classes = [
                "clickable",
                isOpen ? "open" : "",
                item.flags.includes("prompt_injection_suspected") ? "row-warn" : use.diagnosisBasis ? "ed-row-basis" : "",
                item.id === focused ? "ed-row-focus" : "",
              ]
                .filter(Boolean)
                .join(" ");
              return (
                <Fragment key={item.id}>
                  <tr className={classes} onClick={() => toggle(item.id)}>
                    <td className="col-expand">
                      <button
                        type="button"
                        className="icon-btn"
                        aria-expanded={isOpen}
                        aria-controls={isOpen ? detailId : undefined}
                        aria-label={`${isOpen ? "Hide" : "Show"} the original observation ${item.id}`}
                        onClick={(event) => {
                          event.stopPropagation();
                          toggle(item.id);
                        }}
                      >
                        <Icon name={isOpen ? "chevronDown" : "chevronRight"} size={14} />
                      </button>
                    </td>
                    <td>
                      <span className="cell-stack">
                        <span id={domId("ev", item.id)} tabIndex={-1} className="ed-evid">
                          <Mono>{item.id}</Mono>
                        </span>
                        <span className="chip-row">
                          <Chip>{EVIDENCE_KIND_LABELS[kind]}</Chip>
                        </span>
                        {/* On a phone the Trust column is hidden and the token shows here instead. */}
                        <span className="ed-phone-only">
                          <TrustToken trustClass={item.trust_class} note={view.trust_classes[item.trust_class]} />
                        </span>
                      </span>
                    </td>
                    <td className="ed-col-opt">
                      <span className="cell-stack">
                        {item.tool ? <Mono>{item.tool}</Mono> : <span className="muted">{item.source_type}</span>}
                        <code className="mono muted small ed-ref">{item.source_ref}</code>
                      </span>
                    </td>
                    <td className="ed-col-opt">
                      <span className="cell-stack">
                        {item.recorded_by ? <Mono>{item.recorded_by}</Mono> : <span className="not-recorded">{NOT_RECORDED}</span>}
                        <span className="muted small">
                          {item.round !== null ? `round ${item.round}` : "outside the rounds"} · {fmtTime(item.timestamp)}
                        </span>
                      </span>
                    </td>
                    <td className="ed-col-trust">
                      <span className="cell-stack">
                        <TrustToken trustClass={item.trust_class} note={view.trust_classes[item.trust_class]} />
                        {item.flags.length ? (
                          <span className="chip-row">
                            {item.flags.map((flag, i) => (
                              <FlagChip key={`${flag}-${i}`} flag={flag} />
                            ))}
                          </span>
                        ) : null}
                      </span>
                    </td>
                    <td>
                      <UseChips use={use} />
                    </td>
                  </tr>
                  {isOpen ? (
                    <tr className="row-detail" id={detailId}>
                      <td colSpan={6}>
                        <EvidenceDetail
                          item={item}
                          runId={runId}
                          trustNote={view.trust_classes[item.trust_class]}
                          patterns={patternsById.get(item.id) ?? []}
                          use={use}
                          target={target}
                        />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
          </tbody>
        </table>
        {!page.length ? <div className="table-empty">No observation matches these filters.</div> : null}
      </div>
      {filtered.length > PAGE_SIZES[0] ? (
        <Pager
          offset={w.offset}
          limit={limit}
          total={filtered.length}
          onChange={setOffset}
          pageSizes={PAGE_SIZES}
          onPageSizeChange={(n) => {
            setLimit(n);
            setOffset(0);
          }}
          noun="observations"
          label="Evidence registry pages"
        />
      ) : null}
    </div>
  );
}

function RegistryCard({ view, runId, focusId, target }: { view: EvidenceView; runId: string; focusId: string | null; target: TargetFacts }) {
  const diagnosis = useMemo(() => splitReport(view.final_report).diagnosis, [view.final_report]);
  const classes = Object.entries(view.trust_classes);
  const where =
    target.state === "live"
      ? ` The agents' tool calls read ${liveTargetWords(target).place} live: ${liveTargetWords(target).real}, and what it returned is still untrusted data.`
      : "";
  return (
    <Card
      title={`Evidence registry (${fmtInt(view.registry.length)})`}
      subtitle={`Every observation the run recorded: the tool and agent behind it, how far it can be trusted and how the verifier used it. Expand a row for the original observation and where it was read.${where}`}
    >
      {classes.length ? (
        <details className="ed-legend">
          <summary>What the trust classes mean</summary>
          <dl className="kv ed-legend-list">
            {classes.map(([key, note]) => (
              <div key={key} className="kv-row">
                <dt>
                  <TrustToken trustClass={key} note={note} />
                </dt>
                <dd>{note}</dd>
              </div>
            ))}
          </dl>
        </details>
      ) : null}
      <Registry view={view} runId={runId} focusId={focusId} diagnosisBasis={diagnosis} target={target} />
    </Card>
  );
}

// --- the page ----------------------------------------------------------------------------------------------

/** One run's evidence and decisions (exported for the component tests). */
export function EvidenceBody({
  view,
  runId,
  summary,
  focusId = null,
  environment,
}: {
  view: EvidenceView;
  runId: string;
  summary?: RunSummary;
  focusId?: string | null;
  /** The run's own target (GET /runs/{id} record.environment); left out, the summary's kind only. */
  environment?: TargetFields | null;
}) {
  const known = useMemo(() => new Set(view.registry.map((item) => item.id)), [view.registry]);
  const kind = runAuthorKind(view);
  // GET /runs sends the typed M4 summary: its environment kind and whether it is simulated.
  const target = targetFacts(environment ?? summaryTarget(summary as unknown as SummarySourceFields | undefined));
  return (
    <>
      <RunBar view={view} summary={summary} kind={kind} target={target} />
      <RunNotices view={view} runId={runId} known={known} />
      <DecisionCard view={view} runId={runId} known={known} kind={kind} />
      <ChainCard view={view} runId={runId} known={known} kind={kind} />
      <DisagreementsCard view={view} runId={runId} known={known} />
      <ActionCard view={view} runId={runId} known={known} />
      <RegistryCard view={view} runId={runId} focusId={focusId} target={target} />
    </>
  );
}

/** Ask for the run's record again while a just-started run has none yet (its environment is in it). */
function recordInterval(detail: { status: string; record: unknown } | undefined): number | null {
  return detail && !detail.record && detail.status === "running" ? 3000 : null;
}

function EvidenceRun({ runId, summary, focusId }: { runId: string; summary: RunSummary | undefined; focusId: string | null }) {
  const polled = usePolling(() => api.evidenceView(runId), evidenceInterval, [runId]);
  // The evidence view names no environment: the run's record does (which cluster and namespace
  // its tools read). Without it, the page still says live or simulated from the run's summary.
  const detail = usePolling(() => api.runView(runId), recordInterval, [runId]);
  const environment = detail.data?.record?.environment ?? null;
  return (
    <PolledView polled={polled} loadingLabel="Loading this run's evidence and verdicts…" errorTitle="Could not load the run's evidence (GET /runs/{id}/evidence)">
      {(view) => <EvidenceBody view={view} runId={runId} summary={summary} focusId={focusId} environment={environment} />}
    </PolledView>
  );
}

export function EvidencePage({ route }: PageProps) {
  const selection = useRunSelection(route);
  return (
    <>
      <PageHeader
        title="Evidence & Decisions"
        description="What the agents observed, what they claimed, and how RECAP's evidence rules judged every claim: from the original observation to the supported diagnosis and the fix it led to."
      />
      <RunScope selection={selection} page="evidence">
        {(runId, summary) => <EvidenceRun key={runId} runId={runId} summary={summary} focusId={route.params.get("ev")} />}
      </RunScope>
    </>
  );
}
