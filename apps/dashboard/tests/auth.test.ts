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

test("an explicit token is used as given (the sign-in box checks it before saving)", () => {
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

test("refusals, rejections and revocations stand out in the decision log", () => {
  assert.equal(approvalEventTone("approved"), "ok");
  assert.equal(approvalEventTone("consumed"), "accent");
  assert.equal(approvalEventTone("revoked"), "bad");
  assert.equal(approvalEventTone("rejected"), "bad");
  assert.equal(approvalEventTone("refused"), "warn");
  assert.equal(approvalEventTone("requested"), "neutral");
});
