import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api, toApiError } from "../api";
import { fmtArgs, fmtCountdown, fmtDateTime, humanize, riskTone, shortDigest, tsMs } from "../format";
import { SIGN_IN_HINT } from "../auth";
import type { EvidenceItem, PolicyVerdictView, ProposalCheckView } from "../dto";
import { announceApprovalsChanged, hrefFor, useNow, useOperatorToken } from "../hooks";
import type { Approval, ApprovalRequest } from "../types";
import "../styles/incident-lab.css";
import { Chip, Mono } from "./Badge";
import { KeyValues } from "./Card";
import { Icon } from "./Icon";
import { JsonView } from "./JsonView";
import { LogView } from "./LogView";
import { Modal } from "./Modal";
import { StatusToken } from "./StatusToken";
import { UntrustedText } from "./Untrusted";

/** What the server answered to a decision sent from this tab. */
export type DecisionOutcome =
  | { ok: true; approval: Approval }
  | { ok: false; status: number; message: string; attempt: string };

/** Flip the last hex digit: a digest for a *different* proposal than the one shown. */
export function tamper(digest: string): string {
  if (!digest) return "0";
  return digest.slice(0, -1) + (digest.endsWith("0") ? "1" : "0");
}

/**
 * Send a decision on ONE exact proposal: POST /approvals/{proposal_id} with its digest. The
 * server refuses (409) any digest that does not match, an expired request, or a second decision,
 * and records the signed-in operator as the approver (never a name the browser sends). Nothing
 * here ever decides by itself: only a click sends a decision.
 */
export function useApprovalDecision(request: ApprovalRequest, onDecided?: (approval: Approval) => void) {
  const [busy, setBusy] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<DecisionOutcome | null>(null);

  async function send(decision: "approved" | "rejected", tampered = false) {
    const attempt = tampered ? "approve with a tampered digest" : decision === "approved" ? "approve" : "reject";
    setBusy(attempt);
    try {
      const approval = await api.decideApproval(request.proposal_id, {
        decision,
        proposal_digest: tampered ? tamper(request.proposal_digest) : request.proposal_digest,
      });
      setOutcome({ ok: true, approval });
      onDecided?.(approval);
    } catch (err) {
      const e = toApiError(err);
      setOutcome({ ok: false, status: e.status, message: e.message, attempt });
    } finally {
      setBusy(null);
      announceApprovalsChanged();
    }
  }

  return { busy, outcome, send };
}

/** The server's answer to a decision: what it recorded, or why it refused. */
export function DecisionResult({ outcome }: { outcome: DecisionOutcome | null }) {
  if (!outcome) return null;
  if (outcome.ok) {
    const a = outcome.approval;
    const approved = a.decision === "approved";
    return (
      <div className={`approval-response ${approved ? "tone-ok" : "tone-warn"}`} role="status">
        <Icon name={approved ? "check" : "stop"} size={15} />
        <div>
          <strong>
            {approved ? "Approved" : "Rejected"} by {a.actor}
          </strong>{" "}
          · the server recorded approval <Mono>{a.approval_id}</Mono> (scope <Mono>{a.scope}</Mono>, single use
          {approved ? `, valid until ${fmtDateTime(a.expires_at)}` : ""}).{" "}
          {approved
            ? "The controller now sends this exact action once, through the safety gateway."
            : "The fix will not run; the run stops as blocked."}
        </div>
      </div>
    );
  }
  return (
    <div className="approval-response tone-bad" role="alert">
      <Icon name="x" size={15} />
      <div>
        <strong>
          {outcome.status ? `HTTP ${outcome.status}` : "Request failed"} for “{outcome.attempt}”
        </strong>
        : {outcome.message}
        {outcome.status === 409 && outcome.attempt.includes("tampered") ? " The request stays pending: the binding held." : ""}
      </div>
    </div>
  );
}

function SignInNote({ inDialog = false }: { inDialog?: boolean }) {
  return (
    <div className="approval-response tone-warn" role="note">
      <Icon name="key" size={15} />
      <div>
        {inDialog ? (
          <>
            <strong>Sign in to decide.</strong> Choose Decide later, sign in (top right) with the operator token that the API server
            printed when it started, then reopen this approval with Review approval.
          </>
        ) : (
          SIGN_IN_HINT
        )}
      </div>
    </div>
  );
}

/** The expiry countdown: amber while it runs, red once expired (text says which, not only color). */
function Countdown({ expiresAt, now }: { expiresAt: string; now: number }) {
  const expired = tsMs(expiresAt) <= now;
  return (
    <StatusToken
      tone={expired ? "bad" : "warn"}
      icon="clock"
      label={expired ? "Expired" : fmtCountdown(expiresAt, now)}
      title={`The request expires at ${fmtDateTime(expiresAt)}`}
      size="sm"
    />
  );
}

/** The expiry countdown with its own clock (so the page around it does not re-render every second). */
export function ApprovalCountdown({ expiresAt }: { expiresAt: string }) {
  const now = useNow(1000);
  return <Countdown expiresAt={expiresAt} now={now} />;
}

/** The trusted tier from the registry next to the proposer's own (never trusted) claim. */
function RiskTokens({ trusted, declared }: { trusted: string | null | undefined; declared: string | null | undefined }) {
  return (
    <span className="chip-row">
      <Chip tone={riskTone(trusted ?? "unknown")} mono title="From RECAP's trusted tool registry (configs/policies.yaml)">
        trusted risk: {trusted || "see the policy decision"}
      </Chip>
      <Chip mono title="Declared by the proposer; the policy never trusts it">
        declared: {declared || "not recorded"}
      </Chip>
    </span>
  );
}

/**
 * A pending approval for ONE exact proposal, inline (the Safety & Approvals page). Approve/Reject
 * POST /approvals/{proposal_id} with the proposal's digest; the server refuses (409) any digest
 * that does not match, an expired request, or a second decision.
 *
 * Deciding needs the operator token (sign in, top right). A rejection also revokes an approval
 * that was not used yet: "no" wins until the gateway uses the approval, right before it sends
 * the write.
 */
export function ApprovalCard({
  request,
  trustedRisk,
  onDecided,
}: {
  request: ApprovalRequest;
  /** Risk tier from the trusted registry (mitigation.proposed event), if known. */
  trustedRisk?: string;
  onDecided?: (approval: Approval) => void;
}) {
  const { busy, outcome, send } = useApprovalDecision(request, onDecided);
  const now = useNow(1000);
  const signedIn = useOperatorToken() !== null;
  const p = request.proposal;
  const expired = tsMs(request.expires_at) <= now;
  const decided = outcome?.ok === true;
  const params = p.params ?? {};
  const disabled = !!busy || decided || expired || !signedIn;

  return (
    <div className="approval">
      <div className="approval-head">
        <div className="approval-title">
          <Icon name="key" size={18} />
          <div>
            <div className="approval-heading">Human approval required</div>
            <div className="muted">
              The policy returned <Mono>APPROVAL_REQUIRED</Mono> for a reversible write. Nothing runs until you decide.
            </div>
          </div>
        </div>
        <Countdown expiresAt={request.expires_at} now={now} />
      </div>

      <div className="approval-action">
        <code className="mono">{p.action}</code>
        <span className="muted">on</span>
        <code className="mono">{p.target}</code>
        {Object.keys(params).length ? <code className="mono muted">({fmtArgs(params)})</code> : null}
      </div>

      <KeyValues
        className="kv-wide"
        rows={[
          ["Params", Object.keys(params).length ? <JsonView value={params} maxHeight={120} /> : <span className="muted">none</span>],
          ["Risk", <RiskTokens trusted={trustedRisk} declared={p.risk_level} />],
          [
            "Rationale",
            p.rationale ? (
              <UntrustedText text={p.rationale} author={`the proposer (source: ${p.source})`} />
            ) : (
              <span className="muted">none recorded</span>
            ),
          ],
          [
            "Cited evidence",
            p.evidence_ids.length ? (
              <span className="chip-row">
                {p.evidence_ids.map((id) => (
                  <a key={id} className="chip tone-neutral mono" href={hrefFor("evidence", { run: request.run_id, ev: id })}>
                    {id}
                  </a>
                ))}
              </span>
            ) : (
              <span className="muted">none</span>
            ),
          ],
          ["Source", <Chip tone="accent" mono>{p.source}</Chip>],
          [
            "Digest",
            <span>
              <Mono title="The first 16 hex characters of the SHA-256 digest">{shortDigest(request.proposal_digest)}…</Mono>
              <span className="muted"> · bound to incident, action, target and params</span>
            </span>,
          ],
          ["Proposal", <Mono>{request.proposal_id}</Mono>],
          ["Expires", fmtDateTime(request.expires_at)],
        ]}
      />

      {signedIn ? null : <SignInNote />}

      <div className="approval-buttons">
        <button type="button" className="btn btn-approve" disabled={disabled} onClick={() => void send("approved")}>
          <Icon name="check" size={15} /> {busy === "approve" ? "Approving…" : "Approve this exact action"}
        </button>
        <button type="button" className="btn btn-reject" disabled={disabled} onClick={() => void send("rejected")}>
          <Icon name="x" size={15} /> {busy === "reject" ? "Rejecting…" : "Reject"}
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-small"
          disabled={disabled}
          title="Sends a digest for a different proposal. The server must refuse it with 409; the request stays pending."
          onClick={() => void send("approved", true)}
        >
          Test binding: send a tampered digest (expect 409)
        </button>
      </div>

      <DecisionResult outcome={outcome} />
    </div>
  );
}

// ==================================================================================================
// The approval modal (Incident Lab)
// ==================================================================================================

/** The evidence a proposal cites, as GET /runs/{id}/evidence has it (or why it cannot be shown). */
export interface CitedEvidence {
  /** The run's evidence registry; null while it loads or when it could not be read. */
  registry: EvidenceItem[] | null;
  /** Why the registry is not shown (loading, an error message). */
  note?: string;
}

/** RECAP's reason text as a sentence: capitalized, ending with a period. */
function asSentence(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return trimmed;
  const capital = trimmed.charAt(0).toUpperCase() + trimmed.slice(1);
  return /[.!?]$/.test(capital) ? capital : `${capital}.`;
}

function Section({ title, hint, children }: { title: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <section className="lab-approval-section">
      <h3 className="lab-approval-title">{title}</h3>
      {hint ? <p className="lab-approval-hint">{hint}</p> : null}
      {children}
    </section>
  );
}

/** One cited observation: where it came from, who recorded it, its flags and its (untrusted) text. */
function CitedItem({ id, runId, item }: { id: string; runId: string; item: EvidenceItem | undefined }) {
  const link = (
    <a className="mono" href={hrefFor("evidence", { run: runId, ev: id })}>
      {id}
    </a>
  );
  if (!item) {
    return (
      <li className="lab-cited">
        <div className="lab-cited-head">
          {link}
          <StatusToken tone="warn" size="sm" label="Not in this run's evidence registry" title="The proposal cites an id this run never recorded" />
        </div>
      </li>
    );
  }
  return (
    <li className="lab-cited">
      <div className="lab-cited-head">
        {link}
        <Mono>{item.source_ref}</Mono>
        <span className="muted small">
          {item.recorded_by ? `recorded by ${humanize(item.recorded_by)}` : "recorded by RECAP"}
          {item.round !== null ? ` · round ${item.round}` : ""}
        </span>
        {item.flags.map((flag) => (
          <Chip key={flag} tone={flag === "prompt_injection_suspected" ? "warn" : "neutral"} mono>
            {flag}
          </Chip>
        ))}
      </div>
      <LogView
        text={item.content}
        label={`Observation ${item.id}`}
        untrusted={item.tool ? `tool output · ${item.tool}` : humanize(item.trust_class)}
        maxHeight={150}
        initialLines={12}
        maxChars={2400}
      />
    </li>
  );
}

/**
 * The approval decision for the Incident Lab, as a modal that cannot be dismissed by Escape or a
 * click outside: the decision is the point. It shows the exact action, target and params, the
 * trusted risk tier, the policy verdict and the evidence gate's check, the digest that binds the
 * approval (short form), the expiry countdown, the proposal's rationale as untrusted text with
 * its author, and the evidence it cites. Approve and Reject are explicit; "Decide later" closes
 * the dialog and the request stays pending (closing is never a decision). After a decision the
 * server's answer is shown until the operator closes the dialog.
 */
export function ApprovalModal({
  request,
  open,
  onDefer,
  onDecided,
  trustedRisk,
  policy,
  check,
  author,
  evidence,
  closedReason,
}: {
  request: ApprovalRequest;
  open: boolean;
  /** "Decide later" / "Close": hide the dialog. The request stays pending until decided or expired. */
  onDefer: () => void;
  onDecided?: (approval: Approval) => void;
  /** Risk tier from the trusted registry, if known. */
  trustedRisk?: string | null;
  /** The policy decision that asked for the approval (GET /runs/{id}/evidence policy_verdicts). */
  policy?: PolicyVerdictView | null;
  /** The evidence gate's check of this proposal (proposal_checks). */
  check?: ProposalCheckView | null;
  /** Who wrote the rationale, in words, and whether it is RECAP's own (trusted) text. */
  author: { who: string; trusted: boolean };
  evidence: CitedEvidence;
  /** Set when the request can no longer be decided here (the run ended or the request closed). */
  closedReason?: string | null;
}) {
  const { busy, outcome, send } = useApprovalDecision(request, onDecided);
  const now = useNow(1000);
  const signedIn = useOperatorToken() !== null;
  const focusRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const p = request.proposal;
  const params = p.params ?? {};
  const expired = tsMs(request.expires_at) <= now;
  const decided = outcome?.ok === true;
  const done = decided || expired || !!closedReason;
  const disabled = !!busy || done || !signedIn;
  const registry = evidence.registry;

  // Once nothing can be decided any more, the way out takes focus (the buttons just disabled).
  useEffect(() => {
    if (open && done) closeRef.current?.focus();
  }, [open, done]);

  // The server's answer sits in the decision bar, which stays in view however far the dialog scrolls.
  const answer = outcome ? (
    <div className="lab-foot-answer">
      <DecisionResult outcome={outcome} />
    </div>
  ) : null;
  const footer = done ? (
    <>
      {answer}
      <button ref={closeRef} type="button" className="btn btn-primary lab-foot-btn" onClick={onDefer}>
        {decided ? "Close and watch the run" : "Close"}
      </button>
    </>
  ) : (
    <>
      {answer}
      <button
        type="button"
        className="btn btn-ghost btn-small lab-foot-btn lab-tamper"
        disabled={disabled}
        title="Sends a digest for a different proposal. The server must refuse it with 409; the request stays pending."
        onClick={() => void send("approved", true)}
      >
        Test binding: send a tampered digest (expect 409)
      </button>
      <button type="button" className="btn btn-ghost lab-foot-btn" disabled={!!busy} onClick={onDefer}>
        Decide later
      </button>
      <button type="button" className="btn btn-reject lab-foot-btn" disabled={disabled} onClick={() => void send("rejected")}>
        <Icon name="x" size={15} /> {busy === "reject" ? "Rejecting…" : "Reject"}
      </button>
      <button type="button" className="btn btn-approve lab-foot-btn" disabled={disabled} onClick={() => void send("approved")}>
        <Icon name="check" size={15} /> {busy === "approve" ? "Approving…" : "Approve this exact action"}
      </button>
    </>
  );

  return (
    <Modal
      open={open}
      onClose={onDefer}
      dismissible={false}
      size="large"
      title="Approve this exact action?"
      description="The safety policy will not let this change run until a person approves it. Nothing changes in the simulated shop until you decide; Decide later keeps the request pending."
      initialFocusRef={focusRef}
      footer={footer}
    >
      <div ref={focusRef} tabIndex={-1} className="lab-approval-action" role="group" aria-label="The proposed action">
        <div className="lab-approval-command">
          <code className="mono">{p.action}</code>
          <span className="muted">on</span>
          <code className="mono">{p.target}</code>
          {Object.keys(params).length ? <code className="mono muted">({fmtArgs(params)})</code> : <span className="muted small">no params</span>}
        </div>
        <div className="chip-row">
          <RiskTokens trusted={trustedRisk ?? policy?.risk} declared={p.risk_level} />
          <Countdown expiresAt={request.expires_at} now={now} />
        </div>
        <div className="lab-approval-meta">
          <span>
            digest <Mono title="The first 16 hex characters of the SHA-256 digest">{shortDigest(request.proposal_digest)}…</Mono>
          </span>
          <span>
            proposal <Mono>{request.proposal_id}</Mono>
          </span>
          <span>expires {fmtDateTime(request.expires_at)}</span>
        </div>
        <p className="lab-approval-binding">
          The digest binds an approval to this incident, action, target and params: any other change is refused, and the approval is used once.
        </p>
      </div>

      {signedIn || done ? null : <SignInNote inDialog />}

      {closedReason ? (
        <div className="approval-response tone-neutral" role="status">
          <Icon name="info" size={15} />
          <div>{closedReason}</div>
        </div>
      ) : expired && !decided ? (
        <div className="approval-response tone-bad" role="status">
          <Icon name="clock" size={15} />
          <div>
            <strong>This request expired</strong> at {fmtDateTime(request.expires_at)}. The server refuses a decision now, so the fix
            will not run.
          </div>
        </div>
      ) : null}

      <Section title="Why you are asked" hint="RECAP's own checks, from trusted configuration: not the model's words.">
        <ul className="lab-checks">
          <li>
            {policy ? (
              <>
                <StatusToken tone="warn" icon="shield" size="sm" label="Policy: approval required" />
                <span>
                  {policy.reasons.length ? asSentence(policy.reasons.join("; ")) : "No reason recorded."}
                  {policy.rule_ids.length ? (
                    <>
                      {" "}
                      Deciding rule <Mono>{policy.rule_ids[policy.rule_ids.length - 1]}</Mono>.
                    </>
                  ) : null}
                </span>
              </>
            ) : (
              <>
                <StatusToken tone="warn" icon="shield" size="sm" label="Policy: approval required" />
                <span>A reversible write needs a person's approval bound to this exact proposal.</span>
              </>
            )}
          </li>
          <li>
            {check ? (
              <>
                <StatusToken
                  tone={check.ok ? "ok" : "bad"}
                  size="sm"
                  label={check.ok ? "Evidence gate passed" : "Evidence gate failed"}
                  title={check.enforced ? "Enforced" : "Not enforced (a simulation-only ablation)"}
                />
                <span>
                  {check.rule_ids.length ? check.rule_ids.join(" · ") : "No rule ids recorded."}
                  {check.problems.length ? ` · ${check.problems.join("; ")}` : ""}
                </span>
              </>
            ) : (
              <>
                <StatusToken tone="neutral" size="sm" label="Evidence gate" />
                <span className="muted">Not loaded yet.</span>
              </>
            )}
          </li>
        </ul>
      </Section>

      <Section
        title="The proposal's rationale"
        hint={
          author.trusted
            ? "Written by RECAP."
            : `Written by ${author.who}: untrusted text, shown as data. RECAP's checks above do not rely on it.`
        }
      >
        {p.rationale ? (
          author.trusted ? (
            <p className="lab-rationale">{p.rationale}</p>
          ) : (
            <p className="lab-rationale">
              <UntrustedText text={p.rationale} author={author.who} />
            </p>
          )
        ) : (
          <p className="muted">No rationale recorded.</p>
        )}
        {p.expected_health_change || p.rollback_hint ? (
          <KeyValues
            className="kv-wide"
            rows={[
              ...(p.expected_health_change
                ? ([["Expected change", author.trusted ? p.expected_health_change : <UntrustedText text={p.expected_health_change} author={author.who} />]] as [ReactNode, ReactNode][])
                : []),
              ...(p.rollback_hint
                ? ([["Rollback hint", author.trusted ? p.rollback_hint : <UntrustedText text={p.rollback_hint} author={author.who} />]] as [ReactNode, ReactNode][])
                : []),
            ]}
          />
        ) : null}
      </Section>

      <Section
        title={`Evidence it cites (${p.evidence_ids.length})`}
        hint="Observations from the run's read-only tool calls, as recorded. Tool output is untrusted data."
      >
        {p.evidence_ids.length === 0 ? (
          <p className="muted">The proposal cites no evidence.</p>
        ) : registry === null ? (
          <p className="muted">{evidence.note ?? "Loading the run's evidence…"}</p>
        ) : (
          <ul className="lab-cited-list">
            {p.evidence_ids.map((id) => (
              <CitedItem key={id} id={id} runId={request.run_id} item={registry.find((e) => e.id === id)} />
            ))}
          </ul>
        )}
        {p.citations.length ? (
          <div className="chip-row">
            <span className="muted small">Runbook sections cited:</span>
            {p.citations.map((c) => (
              <Chip key={c} mono>
                {c}
              </Chip>
            ))}
          </div>
        ) : null}
      </Section>

    </Modal>
  );
}
