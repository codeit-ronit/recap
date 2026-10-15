/**
 * Tests for the operator sign-in state (src/auth.ts). Run with `npm test`.
 *
 * Node has no sessionStorage, so these also show that the token falls back to memory.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { authHeaders, getToken, onTokenChange, setToken } from "../src/auth.ts";
import { approvalEventTone } from "../src/format.ts";

test("no Authorization header is sent while signed out", () => {
  setToken(null);
  assert.equal(getToken(), null);
  assert.deepEqual(authHeaders(), {});
});

test("signing in adds the bearer header to every request, signing out removes it", () => {
  setToken("  token-0123456789abcdef  ");
  assert.equal(getToken(), "token-0123456789abcdef"); // surrounding spaces are trimmed
  assert.deepEqual(authHeaders(), { Authorization: "Bearer token-0123456789abcdef" });
  setToken(null);
  assert.deepEqual(authHeaders(), {});
});

test("a candidate token can be tried without signing in", () => {
  setToken(null);
  assert.deepEqual(authHeaders("candidate"), { Authorization: "Bearer candidate" });
  assert.equal(getToken(), null); // checking a candidate does not sign anyone in
});

test("listeners hear sign-in and sign-out, and can stop listening", () => {
  const heard: (string | null)[] = [];
  const stop = onTokenChange((token) => heard.push(token));
  setToken("abc");
  setToken(null);
  stop();
  setToken("ignored");
  setToken(null);
  assert.deepEqual(heard, ["abc", null]);
});

test("a blank token counts as signed out", () => {
  setToken("   ");
  assert.equal(getToken(), null);
});

/** A minimal Web Storage stand-in that shows what was written to it. */
function fakeStorage() {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => void data.set(key, String(value)),
    removeItem: (key: string) => void data.delete(key),
  };
}

/** Put fake browser storages on globalThis; returns the function that puts back the old ones. */
function installStorages(storages: Record<string, unknown>): () => void {
  const saved = Object.keys(storages).map(
    (name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)] as const,
  );
  for (const [name, value] of Object.entries(storages)) {
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  }
  return () => {
    for (const [name, descriptor] of saved) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete (globalThis as Record<string, unknown>)[name];
    }
  };
}

test("the token is kept in this tab's sessionStorage, never in localStorage", () => {
  const session = fakeStorage();
  const local = fakeStorage();
  const restore = installStorages({ sessionStorage: session, localStorage: local });
  try {
    setToken("tab-token-0123456789");
    assert.equal(session.data.get("recap.operatorToken"), "tab-token-0123456789");
    assert.equal(local.data.size, 0); // other tabs and later visits never see it
    setToken(null);
    assert.equal(session.data.size, 0); // signing out removes it from the tab too
  } finally {
    restore();
  }
});

test("refusals, rejections and revocations stand out in the decision log", () => {
  assert.equal(approvalEventTone("approved"), "ok");
  assert.equal(approvalEventTone("consumed"), "accent");
  assert.equal(approvalEventTone("revoked"), "bad");
  assert.equal(approvalEventTone("rejected"), "bad");
  assert.equal(approvalEventTone("refused"), "warn");
  assert.equal(approvalEventTone("requested"), "neutral");
});
