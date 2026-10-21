# ADR 0015: An operator-authenticated API, and approvals where "no" wins until the approval is used

**In one sentence:** every state-changing API call needs the operator's bearer token and
records the approver from it, the API answers only loopback host names and same-site
origins, and the approval store accepts a rejection until the approval has been used
(immediately before the write is sent), with every transition a compare-and-swap and every
attempt in an append-only log.

## Status

Accepted (M1, 2026-10-15). Fixes audit finding 2.4. Amends
[ADR 0002](0002-sqlite-storage.md) ("decide once" is now "approve once; reject until used").

## Context

- **The race:** the approval store kept only the first decision. When an API client approved
  while the operator was still reading the console prompt, the operator's "n" was refused
  and the rollback ran anyway. The same held across two processes sharing the database.
- **The API:** no authentication at all. Anyone who could reach it could approve (taking the
  digest from `GET /approvals/pending`), start runs and evaluations, and claim any approver
  name through a free-text `actor` field. With no Host check, a web page the operator
  visited could reach the local API through DNS rebinding.

## Decision

Approval store ([recap/safety/approvals.py](../../recap/safety/approvals.py)):

- Approve: only a pending request. Reject: a pending request, **or** an approved one whose
  approval was not used yet (a revocation; the gateway's later attempt to use it is
  refused). After a rejection nothing is accepted. The gateway uses the approval immediately
  before it sends the write; from then on a rejection is refused ("already used to send the
  action"), because nothing can call the write back.
- Every transition runs in one `BEGIN IMMEDIATE` transaction (the write lock is taken before
  reading), so processes sharing the file are serialized. `consume` also requires the request
  to still be approved.
- `approval_events`: an append-only log of every request, approval, rejection, revocation,
  expiry, use and refused attempt. SQLite triggers refuse updates, deletes and
  `INSERT OR REPLACE`: a guard against bugs and accidental edits, not against someone who can
  write the file (they could drop the triggers; files under `data/` are trusted state).

API ([apps/api/auth.py](../../apps/api/auth.py), [apps/api/main.py](../../apps/api/main.py)):

- `Authorization: Bearer <operator token>` on `POST /incidents/run`, `/approvals/{id}`,
  `/evals/run` and the new `/runs/{id}/cancel`, compared in constant time. The token comes
  from `RECAP_OPERATOR_TOKEN`, or is generated on first start, saved to
  `data/operator-token` (mode 0600) and printed once. An existing token file is used only if
  it is not a symlink, belongs to this user and no one else can read it; otherwise the
  server refuses to start (added after the M1 review). `GET /auth/session` names the
  operator.
- The approver recorded is `operator:<name>` (`RECAP_OPERATOR_NAME`); an `actor` field in the
  body is refused (422).
- Trusted Host check (`localhost`, `127.0.0.1`, `[::1]`; `RECAP_ALLOWED_HOSTS`) and an Origin
  check on unsafe methods. Reads stay open on trusted hosts, because the dashboard's live
  event stream (EventSource) cannot send headers.
- The dashboard has an operator sign-in (the token lives in the tab's sessionStorage, never
  a cookie), sends the header, shows the decision log, and can cancel a run. Its development
  server (Vite) answers only the loopback host names too (403 otherwise) and proxies to the
  API without CORS headers (added after the M1 review).

## Consequences

### Good

- All seven AUDIT-2.4 reproductions pass, including the console race and the
  cross-process race. A cross-process revoke-against-consume race always ends with exactly
  one winner and a consistent store.
- Every decision is attributable and auditable after the fact.

### Bad

- One token, one operator: no per-user accounts, roles or audit of *which* human. A shared
  deployment needs SSO/OIDC, roles and TLS.
- Reads are unauthenticated on the trusted hosts. Run data is simulated and holds no
  secrets, but a real deployment must protect reads too.
- The token is printed to the server's terminal once, like a Jupyter token.

## Alternatives considered

- **Session cookies with a login page.** Cookies bring CSRF back; a bearer token in
  sessionStorage cannot be sent by another site at all.
- **First decision wins (V1).** That is exactly what let a separate approval override the
  operator's "no".
- **Let a rejection always win, even after the approval was used.** It cannot call back a
  write that was already sent, and would make the record claim something that did not happen.

## Amended after the M2 human-approved run review

- The console approver reads an answer only from an interactive terminal: piped or missing
  input is rejected unread, and anything typed before the prompt appeared is discarded, so
  `console-user` means an answer typed on a terminal after the proposal was shown. The
  append-only decision log records how each console answer came ("interactive terminal;
  answered after 53.4 s", flagged when faster than a person reads; a revoking "no" keeps its
  note too) through `ApprovalStore.decide(note=...)`
  ([ADR 0019](0019-checkable-evidence-of-a-real-model-run.md)). It proves a terminal and a
  timing, not a person.
