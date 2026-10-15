import { useState } from "react";
import type { FormEvent } from "react";
import { api, toApiError } from "../api";
import { setToken } from "../auth";
import { useOperatorToken, usePolling } from "../hooks";
import { Icon } from "./Icon";

/**
 * Topbar control for the operator token (see src/auth.ts and apps/api/auth.py).
 *
 * Signed out: a "Sign in" button that opens a small form. The token is checked with
 * GET /auth/session BEFORE it is saved, so a typo never becomes the stored token.
 * Signed in: the operator's name and a "Sign out" button.
 */
export function OperatorSignIn() {
  const token = useOperatorToken();
  // Re-check a stored token (e.g. the API restarted with a new one); deps change on sign-in/out.
  const session = usePolling(token ? () => api.session() : null, 60000, [token !== null]);
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function signIn(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.session(draft.trim());
      setToken(draft.trim());
      setDraft("");
      setOpen(false);
    } catch (err) {
      const e = toApiError(err);
      setError(e.status === 401 ? "That is not the operator token of this API server." : e.message);
    } finally {
      setBusy(false);
    }
  }

  if (token && session.data) {
    return (
      <span className="operator signed-in" title={`Decisions are recorded as ${session.data.actor}`}>
        <Icon name="key" size={14} />
        <span className="operator-name">{session.data.operator}</span>
        <button type="button" className="btn btn-ghost btn-small" onClick={() => setToken(null)}>
          Sign out
        </button>
      </span>
    );
  }
  if (token && session.error?.status === 401) {
    // The stored token no longer works (for example, the API now uses a different one).
    return (
      <span className="operator">
        <span className="operator-warn">token rejected</span>
        <button type="button" className="btn btn-small" onClick={() => setToken(null)}>
          Sign in again
        </button>
      </span>
    );
  }
  return (
    <span className="operator">
      <button type="button" className="btn btn-small" aria-expanded={open} onClick={() => setOpen(!open)}>
        <Icon name="key" size={14} /> Sign in
      </button>
      {open ? (
        <form className="operator-form" onSubmit={(event) => void signIn(event)}>
          <label htmlFor="operator-token" className="small muted">
            Operator token (printed by the API server; also in <code>data/operator-token</code>)
          </label>
          <input
            id="operator-token"
            className="input mono"
            type="password"
            autoComplete="off"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            autoFocus
          />
          {error ? (
            <div className="small operator-warn" role="alert">
              {error}
            </div>
          ) : null}
          <div className="chip-row">
            <button type="submit" className="btn btn-primary btn-small" disabled={busy || !draft.trim()}>
              {busy ? "Checking…" : "Sign in"}
            </button>
            <button type="button" className="btn btn-ghost btn-small" onClick={() => setOpen(false)}>
              Cancel
            </button>
          </div>
        </form>
      ) : null}
    </span>
  );
}
