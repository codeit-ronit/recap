/**
 * Operator sign-in for the dashboard (audit finding 2.4).
 *
 * The API needs `Authorization: Bearer <operator token>` for everything that changes state:
 * starting a run, approving or rejecting, cancelling, starting an evaluation. The API server
 * prints the token when it starts (and saves it in data/operator-token). Read-only views
 * and the live event stream work without it.
 *
 * Where the token is kept: sessionStorage, so it lives only in this browser tab and is gone
 * when the tab closes. It is never a cookie, so another web site cannot make the browser
 * send it. If storage is blocked (private mode, blocked site data) it is kept in memory for
 * this page only.
 *
 * This file has no React in it, so `npm test` can run it directly in Node.
 */

const STORAGE_KEY = "recap.operatorToken";

/** Shown whenever the API answers 401. */
export const SIGN_IN_HINT =
  "Sign in (top right) with the operator token that the API server printed when it started.";

let memoryToken: string | null = null; // the current token; storage is only its backup
let loaded = false;
const listeners = new Set<(token: string | null) => void>();

function storage(): Storage | null {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage;
  } catch {
    return null; // some browsers throw when site data is blocked
  }
}

/** The operator token for this tab, or null when signed out. */
export function getToken(): string | null {
  if (!loaded) {
    loaded = true;
    try {
      memoryToken = storage()?.getItem(STORAGE_KEY) ?? null;
    } catch {
      memoryToken = null;
    }
  }
  return memoryToken;
}

/** Sign in (a token) or out (null). Everyone listening is told. */
export function setToken(token: string | null): void {
  const clean = token?.trim() || null;
  loaded = true;
  memoryToken = clean;
  try {
    const store = storage();
    if (store && clean) store.setItem(STORAGE_KEY, clean);
    else if (store) store.removeItem(STORAGE_KEY);
  } catch {
    // Storage is blocked: the token stays in memory for this page only.
  }
  for (const listener of listeners) listener(clean);
}

/** Call `listener` whenever the token changes; returns the function that stops it. */
export function onTokenChange(listener: (token: string | null) => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** The Authorization header for an API request: present only when signed in. */
export function authHeaders(token: string | null = getToken()): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}
