/**
 * Regression tests for the honest labels in src/format.ts. Run with `npm test`.
 *
 * Node runs these TypeScript files directly (built-in type stripping, Node 22.18+), so the
 * dashboard needs no test framework. Components and hooks are checked by `npm run build`
 * (strict TypeScript) only.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import {
  SIMULATED_LABEL,
  costLabel,
  describeEvent,
  environmentBadge,
  isTerminal,
  originBadge,
  providerLabel,
  stateLabel,
  stateTone,
} from "../src/format.ts";
import type { TraceEvent } from "../src/types.ts";

const UNKNOWN_COST = "cost unknown (estimated tokens only)";
const SCRIPTED = { name: "deterministic-scripted-policy", kind: "scripted", origin: "scripted" } as const;
const LOCAL = { name: "ollama:qwen3:8b", kind: "llm", origin: "local_llm", model: "qwen3:8b" } as const;
const HOSTED = { name: "groq:openai/gpt-oss-120b", kind: "llm", origin: "free_hosted_llm" } as const;
const REPLAY = { name: "replay:ollama:qwen3:8b", kind: "replay", origin: "replay" } as const;

test("costLabel always prefers the backend's own cost line", () => {
  const backend = "LLM provider: cost not fully reported (some calls had no usage data); tokens shown are estimates";
  assert.equal(costLabel(backend, SCRIPTED), backend);
  assert.equal(costLabel(backend, LOCAL), backend);
});

test("costLabel falls back to $0.00 only for providers that cannot be billed", () => {
  assert.equal(costLabel(undefined, SCRIPTED), "$0.00 external API charged (deterministic mode)");
  assert.equal(costLabel(undefined, LOCAL), "$0.00 - local model on this machine");
  assert.equal(costLabel(undefined, REPLAY), "$0.00 - replayed recording: no model was called");
  // A free plan is $0 only if every response passed the tripwire: the backend line says so.
  assert.equal(costLabel(undefined, HOSTED), UNKNOWN_COST);
  // A run recorded before origins existed: scripted is still known; an old LLM run is not.
  assert.equal(costLabel(undefined, { name: "old", kind: "scripted" }), "$0.00 external API charged (deterministic mode)");
  assert.equal(costLabel(undefined, { name: "old", kind: "llm" }), UNKNOWN_COST);
  // The provider of an older run may not be the one configured now: unknown is not free.
  assert.equal(costLabel(undefined, undefined), UNKNOWN_COST);
  assert.equal(costLabel("", null), UNKNOWN_COST);
});

test("the run-origin badge never lets scripted or replayed runs look like live reasoning", () => {
  assert.equal(originBadge(SCRIPTED).label, "SCRIPTED");
  assert.equal(originBadge(LOCAL).label, "LOCAL LLM");
  assert.equal(originBadge(HOSTED).label, "FREE HOSTED LLM");
  assert.equal(originBadge(REPLAY).label, "REPLAY");
  assert.match(originBadge(REPLAY).title, /No model was called/);
  assert.equal(originBadge({ name: "x", kind: "llm", origin: "paid_llm" }).tone, "bad");
  assert.equal(originBadge({ name: "old", kind: "scripted" }).label, "SCRIPTED");
  assert.equal(originBadge(undefined).label, "ORIGIN UNKNOWN");
});

test("a deferred run (free quota ran out) is terminal and amber, not a failure", () => {
  assert.equal(stateLabel("QUOTA_EXHAUSTED"), "Quota exhausted (deferred)");
  assert.equal(isTerminal("QUOTA_EXHAUSTED"), true);
  assert.equal(stateTone("QUOTA_EXHAUSTED"), "warn");
});

function llmCall(payload: Record<string, unknown>): TraceEvent {
  return {
    event_id: "evt-1",
    run_id: "run-1",
    seq: 7,
    stage: "INVESTIGATING",
    ts: "2026-10-21T10:00:00+00:00",
    agent: "investigator",
    category: "llm.call",
    payload,
    duration_ms: 16400,
    usage: null,
    schema_version: "recap.v1",
  };
}

test("an llm.call event shows the model, what it chose, exact tokens and time", () => {
  const line = describeEvent(
    llmCall({ model: "qwen3:8b", mode: "tools", response_kind: "tool_request", input_tokens: 2065,
              output_tokens: 57, duration_ms: 16400, ok: true, attempt: 1 }),
  );
  assert.equal(line.tag, "LLM");
  const text = line.parts.map((part) => (typeof part === "string" ? part : part.code)).join("");
  assert.match(text, /qwen3:8b/);
  assert.match(text, /tool_request/);
  assert.match(text, /in 2,065 \/ out 57 tokens/);
  assert.deepEqual(line.chips, []);
});

test("a replayed or unusable model answer is flagged", () => {
  const replayed = describeEvent(llmCall({ model: "qwen3:8b", replayed: true, ok: true }));
  assert.deepEqual(replayed.chips?.map((c) => c.label), ["replayed"]);
  const unusable = describeEvent(llmCall({ model: "qwen3:8b", ok: false, attempt: 2 }));
  assert.deepEqual(unusable.chips?.map((c) => c.label), ["unusable answer", "attempt 2"]);
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

test("the header's provider label names the origin and never calls a replay live", () => {
  assert.equal(providerLabel(SCRIPTED), "deterministic-scripted-policy — scripted, not an LLM");
  assert.equal(providerLabel(LOCAL), "ollama:qwen3:8b — local llm (qwen3:8b)");
  assert.equal(providerLabel(REPLAY), "replay:ollama:qwen3:8b — replay");
  assert.doesNotMatch(providerLabel(REPLAY), /LLM/);
  assert.equal(providerLabel(undefined), "provider unknown");
});

test("a replay's badge is amber, never the live-model tone", () => {
  assert.equal(originBadge(REPLAY).tone, "warn");
  assert.notEqual(originBadge(REPLAY).tone, originBadge(LOCAL).tone);
});

