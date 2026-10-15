/**
 * Regression tests for the honest labels in src/format.ts. Run with `npm test`.
 *
 * Node runs these TypeScript files directly (built-in type stripping, Node 22.18+), so the
 * dashboard needs no test framework. Components and hooks are checked by `npm run build`
 * (strict TypeScript) only.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { SIMULATED_LABEL, costLabel, environmentBadge } from "../src/format.ts";

const UNKNOWN_COST = "cost unknown (estimated tokens only)";

test("costLabel always prefers the backend's own cost line", () => {
  const backend = "LLM provider: cost not fully reported (some calls had no usage data); tokens shown are estimates";
  assert.equal(costLabel(backend, "scripted"), backend);
  assert.equal(costLabel(backend, "llm"), backend);
});

test("costLabel falls back to $0.00 only for the scripted provider", () => {
  assert.equal(costLabel(undefined, "scripted"), "$0.00 external API charged (deterministic mode)");
  assert.equal(costLabel(undefined, "llm"), UNKNOWN_COST);
  // The provider of an older run may not be the one configured now: unknown is not free.
  assert.equal(costLabel(undefined, undefined), UNKNOWN_COST);
  assert.equal(costLabel("", undefined), UNKNOWN_COST);
});

test("environment badge is amber for the simulator", () => {
  const badge = environmentBadge({ kind: "simulated", is_simulated: true });
  assert.equal(badge.label, SIMULATED_LABEL);
  assert.equal(badge.tone, "warn");
});

test("environment badge is red and names the kind for a live environment", () => {
  const badge = environmentBadge({ kind: "kubernetes", is_simulated: false });
  assert.equal(badge.label, "LIVE ENVIRONMENT (kubernetes) · V1");
  assert.equal(badge.tone, "bad");
});

test("environment badge stays muted until GET /system answers", () => {
  const loading = environmentBadge(undefined);
  assert.equal(loading.label, SIMULATED_LABEL);
  assert.equal(loading.tone, "neutral");
  // /health may answer first: its label is shown, still muted.
  const early = environmentBadge(undefined, "LIVE ENVIRONMENT (kubernetes) · V1");
  assert.equal(early.label, "LIVE ENVIRONMENT (kubernetes) · V1");
  assert.equal(early.tone, "neutral");
});

test("environment badge trusts /system over a stale /health label", () => {
  const badge = environmentBadge({ kind: "kubernetes", is_simulated: false }, SIMULATED_LABEL);
  assert.equal(badge.label, "LIVE ENVIRONMENT (kubernetes) · V1");
  assert.equal(badge.tone, "bad");
});
