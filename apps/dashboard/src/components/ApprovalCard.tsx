import { useState } from "react";
import { api, toApiError } from "../api";
import { fmtArgs, fmtCountdown, fmtDateTime, riskTone, shortDigest, tsMs } from "../format";
import { hrefFor, useNow } from "../hooks";
import type { Approval, ApprovalRequest } from "../types";
import { Chip, Mono } from "./Badge";
import { KeyValues } from "./Card";
import { Icon } from "./Icon";
import { JsonView } from "./JsonView";

type Outcome =
  | { ok: true; approval: Approval }
  | { ok: false; status: number; message: string; attempt: string };

const ACTOR = "dashboard-user";

/** Flip the last hex digit: a digest for a *different* proposal than the one shown. */
function tamper(digest: string): string {
  if (!digest) return "0";
  return digest.slice(0, -1) + (digest.endsWith("0") ? "1" : "0");
}

/**
 * A pending approval for ONE exact proposal. Approve/Reject POST
 * /approvals/{proposal_id} with the proposal's digest; the server refuses (409) any
 * digest that does not match, an expired request, or a second decision.
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
  const [busy, setBusy] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const now = useNow(1000);
  const p = request.proposal;
  const expired = tsMs(request.expires_at) <= now;
  const decided = outcome?.ok === true;

  async function send(decision: "approved" | "rejected", tampered = false) {
    const attempt = tampered ? "approve with a tampered digest" : decision === "approved" ? "approve" : "reject";
    setBusy(attempt);
    try {
      const approval = await api.decideApproval(request.proposal_id, {
        decision,
        proposal_digest: tampered ? tamper(request.proposal_digest) : request.proposal_digest,
        actor: ACTOR,
      });
      setOutcome({ ok: true, approval });
      onDecided?.(approval);
    } catch (err) {
      const e = toApiError(err);
      setOutcome({ ok: false, status: e.status, message: e.message, attempt });
    } finally {
      setBusy(null);
    }
  }

  const params = p.params ?? {};
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
        <Chip tone={expired ? "bad" : "warn"}>
          <Icon name="clock" size={12} />
          {expired ? "expired" : fmtCountdown(request.expires_at, now)}
        </Chip>
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
          ["Action", <Mono>{p.action}</Mono>],
          ["Target", <Mono>{p.target}</Mono>],
          ["Params", Object.keys(params).length ? <JsonView value={params} maxHeight={120} /> : <span className="muted">none</span>],
          ["Rationale", p.rationale || <span className="muted">—</span>],
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
            "Citations",
            p.citations.length ? (
              <span className="chip-row">
                {p.citations.map((c) => (
                  <Chip key={c} mono>
                    {c}
                  </Chip>
                ))}
              </span>
            ) : (
              <span className="muted">none</span>
            ),
          ],
          [
            "Risk",
            <span className="chip-row">
              <Chip tone={riskTone(trustedRisk ?? "unknown")} mono title="From the trusted registry (configs/policies.yaml)">
                trusted: {trustedRisk ?? "see policy decision"}
              </Chip>
              <Chip mono title="Declared by the proposer; never trusted by the policy">
                declared: {p.risk_level}
              </Chip>
            </span>,
          ],
          ["Expected change", p.expected_health_change || <span className="muted">—</span>],
          ["Rollback hint", p.rollback_hint || <span className="muted">—</span>],
          [
            "Digest",
            <span>
              <Mono title={request.proposal_digest}>{shortDigest(request.proposal_digest)}…</Mono>
              <span className="muted"> · bound to incident, action, target and params</span>
            </span>,
          ],
          ["Proposal", <Mono>{request.proposal_id}</Mono>],
          ["Expires", fmtDateTime(request.expires_at)],
        ]}
      />

      <div className="approval-buttons">
        <button
          type="button"
          className="btn btn-approve"
          disabled={!!busy || decided || expired}
          onClick={() => void send("approved")}
        >
          <Icon name="check" size={15} /> {busy === "approve" ? "Approving…" : "Approve this exact action"}
        </button>
        <button
          type="button"
          className="btn btn-reject"
          disabled={!!busy || decided || expired}
          onClick={() => void send("rejected")}
        >
          <Icon name="x" size={15} /> {busy === "reject" ? "Rejecting…" : "Reject"}
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-small"
          disabled={!!busy || decided || expired}
          title="Sends a digest for a different proposal. The server must refuse it with 409; the request stays pending."
          onClick={() => void send("approved", true)}
        >
          Test binding: send a tampered digest (expect 409)
        </button>
      </div>

      {outcome ? (
        outcome.ok ? (
          <div className="approval-response tone-ok" role="status">
            <Icon name="check" size={15} />
            <div>
              <strong>Server recorded {outcome.approval.decision}</strong> · approval <Mono>{outcome.approval.approval_id}</Mono> by{" "}
              {outcome.approval.actor} · scope <Mono>{outcome.approval.scope}</Mono> (single use) · valid until{" "}
              {fmtDateTime(outcome.approval.expires_at)}. The controller executes it through the safety gateway.
            </div>
          </div>
        ) : (
          <div className="approval-response tone-bad" role="alert">
            <Icon name="x" size={15} />
            <div>
              <strong>
                {outcome.status ? `HTTP ${outcome.status}` : "Request failed"} for “{outcome.attempt}”
              </strong>
              : {outcome.message}
            </div>
          </div>
        )
      ) : null}
    </div>
  );
}
