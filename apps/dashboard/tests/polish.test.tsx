/**
 * The cross-page polish pass: the run scope's round chips, the "not recorded" notice placed
 * after the facts it qualifies, the offline hint that names where the API is expected, and the
 * approvals-changed signal the shell listens to.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { apiWhere } from "../src/components/EmptyState";
import { RunScope } from "../src/components/RunPicker";
import type { RetrievalView } from "../src/dto";
import { APPROVALS_CHANGED, announceApprovalsChanged } from "../src/hooks";
import type { RunSelection } from "../src/hooks";
import { RunRetrievalBody } from "../src/pages/Knowledge";
import type { RunSummary } from "../src/types";
import { polled, render, tags, textOf } from "./support/render";

const SUMMARY: RunSummary = {
  run_id: "run-f9395a4471",
  scenario_id: "m3-inventory-leak-behind-frontend",
  state: "RESOLVED",
  label: "seed-demo",
  created_at: "2026-10-26T05:01:36.512000+00:00",
  updated_at: "2026-10-26T05:01:37.350000+00:00",
  modes: ["parallel", "parallel"],
  recovered: true,
  outcome_note: "oracle verified recovery (simulated environment)",
  provider: "replay:ollama:qwen3:8b",
  tool_calls: 16,
  pending_approval: false,
};

function selection(summary: RunSummary): RunSelection {
  return { runId: summary.run_id, runs: { ...polled([summary]), refresh: () => undefined }, select: () => undefined };
}

test("the run scope numbers each round's routing instead of repeating bare modes", () => {
  const html = render(
    <RunScope selection={selection(SUMMARY)} page="knowledge">
      {() => null}
    </RunScope>,
  );
  const text = textOf(html);
  assert.match(text, /Resolved R1 parallel R2 parallel/);
  assert.doesNotMatch(text, /parallel parallel/);
  const chips = tags(html, "span").filter((t) => t.attrs.title?.startsWith("Round "));
  assert.deepEqual(
    chips.map((t) => t.attrs.title),
    ["Round 1 routing: parallel", "Round 2 routing: parallel"],
  );
  // A one-round run has one chip; a run with no recorded round has none.
  const single = textOf(render(<RunScope selection={selection({ ...SUMMARY, modes: ["single"] })} page="timeline">{() => null}</RunScope>));
  assert.match(single, /R1 single/);
  assert.doesNotMatch(single, /R2/);
  const none = render(<RunScope selection={selection({ ...SUMMARY, modes: [] })} page="timeline">{() => null}</RunScope>);
  assert.equal(tags(none, "span").filter((t) => t.attrs.title?.startsWith("Round ")).length, 0);
});

/** GET /runs/run-f9395a4471/retrieval of the seeded golden replay, its search lists emptied. */
const RETRIEVAL: RetrievalView = {
  schema_version: "recap-api-v1",
  run_id: "run-f9395a4471",
  status: "finished",
  state: "RESOLVED",
  trace_available: true,
  use_rag: true,
  mode: "bm25",
  embedder: null,
  embedding_server: null,
  embedding_requests: 0,
  embedding_input_tokens: null,
  replayed_embedding_requests: 0,
  agent_searches: 0,
  agent_search_refusals: [],
  knowledge_overlays: [],
  searches: [],
  skipped: [],
  context_docs: [],
  memory: {
    enabled: true,
    read_partitions: ["train", "live"],
    write_partition: "live",
    write_enabled: false,
    searches: [{ seq: 5, readable_partitions: ["train", "live"], created_before: "2026-10-25T12:18:28.811038+00:00", semantic: null, hits: [] }],
    assessments: [],
    final_hits: [],
    written: null,
    skipped: ["a replay reproduces a recorded run; it is not new experience"],
  },
  not_recorded: ["search duration (only a hybrid search's embedding request is timed)"],
};

test("a run's missing fields are listed after the summary they qualify, not above it", () => {
  const text = textOf(render(<RunRetrievalBody view={RETRIEVAL} runId="run-f9395a4471" summary={SUMMARY} />));
  const searched = text.indexOf("How this run searched");
  const memory = text.indexOf("Experience memory");
  const missing = text.indexOf("Not recorded for this run");
  assert.ok(searched >= 0 && memory >= 0 && missing >= 0, "all three are shown");
  assert.ok(searched < missing && memory < missing, "the run's own facts come first");
  assert.match(text, /Not recorded for this run search duration \(only a hybrid search's embedding request is timed\)/);
  // Nothing missing: no notice at all.
  const complete = textOf(render(<RunRetrievalBody view={{ ...RETRIEVAL, not_recorded: [] }} runId="run-f9395a4471" summary={SUMMARY} />));
  assert.doesNotMatch(complete, /Not recorded for this run/);
});

test("the offline hint says where this page expects the API", () => {
  assert.equal(apiWhere("/api", "http://localhost:5173"), "It listens on http://127.0.0.1:8000 (the dev server proxies /api to it).");
  assert.equal(apiWhere("", "http://127.0.0.1:8780"), "It served this page, so it is expected back at http://127.0.0.1:8780.");
  assert.equal(apiWhere("", null), "It serves this page.");
  assert.equal(apiWhere("http://127.0.0.1:9000", "http://localhost:5173"), "This page expects it at http://127.0.0.1:9000 (VITE_API_URL).");
});

test("a decision announces itself so the shell's approvals count refreshes at once", () => {
  // No window (a server render): nothing to tell, and nothing throws.
  assert.equal(typeof (globalThis as { window?: unknown }).window, "undefined");
  assert.doesNotThrow(() => announceApprovalsChanged());
  // A window: one event, named as App.tsx listens for it.
  const target = new EventTarget();
  const seen: string[] = [];
  target.addEventListener(APPROVALS_CHANGED, (event) => seen.push(event.type));
  (globalThis as { window?: unknown }).window = target;
  try {
    announceApprovalsChanged();
  } finally {
    delete (globalThis as { window?: unknown }).window;
  }
  assert.deepEqual(seen, ["recap:approvals-changed"]);
});
