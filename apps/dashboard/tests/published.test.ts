/**
 * Tests for the published-run view helpers (src/view/published.ts) and the Safety page's
 * approval layer for a run decided elsewhere (src/view/safety.ts). Run with `npm test`.
 *
 * tests/fixtures/published-view.json is GET /published as a server on a fresh data folder
 * answered it: the four real-cluster runs whose traces were published (three on Kind, one on
 * SREGym) and the eight real-cluster runs known only from their evaluation records.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import type { PublishedView, TimelineEntry } from "../src/dto.ts";
import { HISTORICAL_LABEL, dayOf, graderView, historicalWords, realClusterRows, rowCounts, targetName } from "../src/view/published.ts";
import { runLayerOutcomes, traceDecisions } from "../src/view/safety.ts";

const VIEW = JSON.parse(readFileSync(new URL("./fixtures/published-view.json", import.meta.url), "utf8")) as PublishedView;
const SREGYM = "run-9d7f47104d";
const KIND = ["run-047aac6eb0", "run-f5b9e5bf45", "run-34a3580d2f"];

test("every published run carries one label, word for word", () => {
  assert.equal(HISTORICAL_LABEL, "Recorded real Kubernetes execution — historical, read-only");
});

test("targets and days in words; a missing one is said, never guessed", () => {
  assert.equal(targetName("kind"), "Kind cluster");
  assert.equal(targetName("sregym"), "SREGym cluster");
  assert.equal(targetName(""), "Target not recorded");
  assert.equal(targetName("other"), "other");
  assert.equal(dayOf("2026-11-07T05:44:36.891620Z"), "2026-11-07");
  assert.equal(dayOf(null), "Not recorded");
});

test("a published run says where and when it ran, who decided, and that nothing runs now", () => {
  const sregym = VIEW.runs.find((run) => run.run_id === SREGYM);
  assert.ok(sregym);
  const words = historicalWords(sregym);
  assert.match(words, /^The record of a real run on the SREGym cluster, on 2026-11-07: qwen3:8b \(ollama-vm:qwen3:8b\) made its decisions then/);
  assert.match(words, /Nothing runs now: no model is called and no cluster is touched\./);
  assert.match(words, /never permissions\.$/);
});

test("SREGym's grader is kept apart from RECAP's oracle, and Kind has none", () => {
  const pass = graderView("sregym", "pass", "SREGym 1.1 mitigation oracle (run-oracle.py), recorded by hand");
  assert.deepEqual([pass?.label, pass?.tone], ["SREGym's grader: pass", "ok"]);
  assert.match(pass?.detail ?? "", /run-oracle\.py.*kept apart from RECAP's independent oracle/);
  assert.equal(graderView("sregym", "fail")?.tone, "bad");
  assert.deepEqual(graderView("sregym", null), { label: "Not graded by SREGym", tone: "neutral", detail: "No SREGym grader verdict was recorded for this run." });
  assert.equal(graderView("kind", null), null);
});

test("every real-cluster run in one list per target, failures included, oldest first", () => {
  const groups = realClusterRows(VIEW);
  assert.deepEqual(
    groups.map((g) => g.target),
    ["kind", "sregym"],
  );
  const [kind, sregym] = groups;
  assert.equal(kind.rows.length, 7);
  assert.equal(sregym.rows.length, 5);
  for (const group of groups) {
    const starts = group.rows.map((row) => row.startedAt ?? "");
    assert.deepEqual(starts, [...starts].sort());
  }
  assert.deepEqual(
    kind.rows.filter((row) => row.traceOpens).map((row) => row.runId),
    KIND,
  );
  assert.deepEqual(
    sregym.rows.filter((row) => row.traceOpens).map((row) => row.runId),
    [SREGYM],
  );
  // A run known only from its record keeps its own failure category and detail.
  const insufficient = kind.rows.find((row) => row.runId === "run-9fea287988");
  assert.deepEqual([insufficient?.finalState, insufficient?.failure, insufficient?.verified], ["FAILED", "insufficient_evidence", false]);
  // SREGym's grader: pass for the published run, fail for the blocked one, none for the others.
  const grader = Object.fromEntries(sregym.rows.map((row) => [row.runId, row.grader?.label ?? null]));
  assert.equal(grader[SREGYM], "SREGym's grader: pass");
  assert.equal(grader["run-e08c17d46e"], "SREGym's grader: fail");
  assert.equal(grader["run-69e580846e"], "Not graded by SREGym");
  assert.ok(kind.rows.every((row) => row.grader === null));
});

test("the list adds up to counts, never a rate", () => {
  const [kind, sregym] = realClusterRows(VIEW);
  assert.deepEqual(rowCounts(kind.rows), { runs: 7, verified: 3, traces: 3 });
  assert.deepEqual(rowCounts(sregym.rows), { runs: 5, verified: 1, traces: 1 });
});

// --- the Safety page's approval layer, for a run decided elsewhere -----------------------------------

function entry(seq: number, category: string, payload: Record<string, unknown>): TimelineEntry {
  return { seq, category, kind: category === "approval.requested" ? "approval_requested" : "approval_decided", payload } as unknown as TimelineEntry;
}

test("a decision only the trace records still passes the approval layer, and says where it is", () => {
  const requests = [entry(43, "approval.requested", { proposal_id: "prop-4e9e45f1e0" })];
  const decided = [
    entry(44, "approval.decided", {
      decision: "approved",
      actor: "console-user",
      proposal_id: "prop-4e9e45f1e0",
      note: "interactive terminal; typed the target service's name; answered after 121.9 s",
    }),
  ];
  const decisions = traceDecisions(requests, decided);
  const base = { gateway: [], checks: [], lifecycles: [], writes: [], counts: { summarized: true, attempted: 0, blocked: 0, executed: 0, refusedByEnvironment: 0 } as never, live: true };
  const approval = runLayerOutcomes({ ...base, decisions })[2];
  assert.equal(approval.state, "passed");
  assert.equal(approval.headline, "Approved (in the run's trace)");
  assert.ok(approval.lines.includes("approved by console-user"));
  assert.ok(approval.lines.some((line) => line.includes("this server's approval log has no entry")));
  // Without a trace decision nothing is claimed.
  assert.equal(runLayerOutcomes(base)[2].headline, "No approval requested");
});
