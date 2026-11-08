/**
 * M6 W4 on the Evaluations page (react-dom/server): a batch's one-switch comparisons (paired
 * counts as "k of N", what each changed, a flag when paired runs differ in more than the
 * switch) and the component evaluations (retrieval, memory, routing, budgets, verifier and
 * safety, each in its own section). Older servers send neither: nothing is shown then. Hostile
 * text must come out escaped, and nothing may read as a percentage.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import type { ComparisonResult, ComponentReport, EvalConfiguration } from "../src/dto";
import { ComparisonDetail, ComparisonsCard, ComponentEvaluations } from "../src/pages/Evals";
import { count, render, tags, textOf } from "./support/render";

const CONFIGURATIONS: EvalConfiguration[] = [
  { name: "single_only", suite: "test", description: "", switches: { strategy: "single" } },
  { name: "parallel_only", suite: "test", description: "", switches: { strategy: "parallel" } },
];

function comparison(overrides: Partial<ComparisonResult> = {}): ComparisonResult {
  return {
    comparison: {
      name: "single-vs-parallel",
      baseline: "single_only",
      variant: "parallel_only",
      switch: "strategy",
      question: "Single investigator vs two specialists",
      decision: "Whether a second specialist earns its tool calls",
    },
    n: 2,
    unpaired: [],
    baseline_successes: 1,
    variant_successes: 2,
    both_succeeded: 1,
    neither: 0,
    only_baseline: [],
    only_variant: ["m3-payment-release-under-forged-signals"],
    baseline_correct: 2,
    variant_correct: 2,
    by_set: { test: { n: 1, baseline_successes: 1, variant_successes: 1 }, held_out: { n: 1, baseline_successes: 0, variant_successes: 1 } },
    pairs: [
      {
        scenario_id: "test-cart-memory-leak",
        scenario_set: "test",
        seed: 206,
        baseline: "success",
        variant: "success",
        baseline_diagnosis_correct: true,
        variant_diagnosis_correct: true,
        baseline_run: "run-a1",
        variant_run: "run-b1",
      },
      {
        scenario_id: "m3-payment-release-under-forged-signals",
        scenario_set: "held_out",
        seed: 67,
        baseline: "budget_exhausted",
        variant: "success",
        baseline_diagnosis_correct: true,
        variant_diagnosis_correct: true,
        baseline_run: "run-a2",
        variant_run: "run-b2",
      },
    ],
    differences: [
      { metric: "tool_calls", n: 2, baseline_mean: 12, variant_mean: 9.12, mean_difference: -2.88, changed: 2 },
      { metric: "rounds", n: 2, baseline_mean: 1, variant_mean: 1, mean_difference: 0, changed: 0 },
    ],
    unexpected: [],
    ...overrides,
  };
}

test("a batch's comparisons: paired counts as 'k of N', the switch's two settings, what changed, and no percentage", () => {
  const flagged = comparison({
    comparison: { ...comparison().comparison, name: "<b>odd</b>" },
    unexpected: ["prompt_version: s1 (v1 vs v2)"],
  });
  const html = render(<ComparisonsCard view={{ comparisons: [comparison(), flagged], configurations: CONFIGURATIONS }} onOpen={() => undefined} />);
  const text = textOf(html);
  assert.match(text, /Comparisons · one switch each/);
  assert.match(text, /Paired runs, small N/);
  assert.match(text, /single-vs-parallel single_only → parallel_only strategy single → parallel 2 1 of 2 2 of 2/);
  assert.match(text, /variant only: m3-payment-release-under-forged-signals/);
  assert.match(text, /yes/);
  assert.match(text, /1 other difference/);
  assert.ok(html.includes("&lt;b&gt;odd&lt;/b&gt;"), "a comparison's name is text, never markup");
  assert.equal(count(text, "%"), 0);
});

test("a batch without comparisons (or from an older server) shows no comparison card", () => {
  assert.equal(render(<ComparisonsCard view={{ comparisons: [], configurations: [] }} onOpen={() => undefined} />), "");
  const older = {} as unknown as { comparisons: ComparisonResult[]; configurations: EvalConfiguration[] };
  assert.equal(render(<ComparisonsCard view={older} onOpen={() => undefined} />), "");
});

test("a comparison's detail: what it measures, each scenario with both runs, and the measures that moved", () => {
  const html = render(<ComparisonDetail result={comparison()} onOpen={() => undefined} />);
  const text = textOf(html);
  assert.match(text, /Measures Single investigator vs two specialists/);
  assert.match(text, /Would change Whether a second specialist earns its tool calls/);
  assert.match(text, /development: 1 of 1 → 1 of 1/);
  assert.match(text, /held-out: 0 of 1 → 1 of 1/);
  assert.match(text, /m3-payment-release-under-forged-signals held-out · seed 67/);
  assert.match(text, /tool calls: −2.88 \(2 of 2 pairs changed; 12 → 9.12\)/);
  assert.doesNotMatch(text, /rounds:/, "a measure that changed in no pair is not listed");
  const buttons = tags(html, "button").map((b) => b.attrs["aria-label"]);
  assert.ok(buttons.includes("Open the evaluation record of run-b2"));
  assert.doesNotMatch(text, /differ in more than the switch/);
  const flagged = textOf(render(<ComparisonDetail result={comparison({ unpaired: ["s9"] })} onOpen={() => undefined} />));
  assert.match(flagged, /These runs differ in more than the switch unpaired: s9/);
});

const REPORT: ComponentReport = {
  schema_version: "recap-component-report-v1",
  report_id: "20261108-w4-components",
  created_at: "2026-11-08T12:00:00+00:00",
  code_version: "abc123def456",
  batch_id: "20261108-w4-scripted",
  retrieval: {
    dataset: "evals/datasets/retrieval-v1.yaml",
    corpus_chunks: 42,
    vectors: "evals/datasets/query-vectors-v1.json",
    embedder: "ollama:nomic-embed-text",
    weights: "sha256:970a",
    rankers: ["bm25", "dense"],
    scores: [
      { ranker: "bm25", kind: "alert", partition: "", n: 17, hit_at_1: 0, hit_at_2: 0, hit_at_4: 9, mrr: 0.142, remediation_at_2: 0, remediation_at_4: 0, remediation_n: 17, wrong_mechanism_at_1: 0, misses: [] },
      { ranker: "bm25", kind: "alert", partition: "test", n: 8, hit_at_1: 0, hit_at_2: 0, hit_at_4: 3, mrr: 0.094, remediation_at_2: 0, remediation_at_4: 0, remediation_n: 8, wrong_mechanism_at_1: 0, misses: [] },
      { ranker: "dense", kind: "paraphrase", partition: "", n: 12, hit_at_1: 6, hit_at_2: 10, hit_at_4: 11, mrr: 0.694, remediation_at_2: 3, remediation_at_4: 5, remediation_n: 8, wrong_mechanism_at_1: 0, misses: [] },
    ],
    exposure: [{ ranker: "bm25+rerank", kind: "alert", k: 4, n: 17, exposed: Array(13).fill("q"), displaced: [] }],
    ranks: {},
    not_measured: [],
  },
  memory: {
    runs: 56,
    runs_with_hits: 49,
    hits: 76,
    by_label: { look_alike: 63, twin: 7 },
    correct: 76,
    false_safe: 0,
    false_unsafe: 0,
    twins: 7,
    twins_accepted: 7,
    fix_from_memory: ["no_rag/test-cart-memory-leak"],
    leakage: [],
    judged: [],
  },
  routing: {
    config: "full_adaptive",
    cases: [],
    rules_fired: { "RT2-cross-service": 4 },
    parallel_cross_service: [4, 4],
    parallel_single_service: [0, 4],
    second_specialist_contributed: [4, 4],
  },
  budget: [
    { config: "small_context", runs: 8, budget_stops: {}, runs_dropping_context: 4, context_dropped: 23, context_compacted: 0, mean_estimated_tokens: 10574.6, mean_tool_calls: 9.4, mean_steps: 11.2 },
  ],
  verifier: [
    { config: "full_adaptive", runs: 8, claims_supported: 20, claims_contradicted: 3, claims_insufficient: 3, runs_with_contradicted: 2, diagnosis_correct: 8, mechanism_correct: 8 },
  ],
  safety: [
    { config: "redteam_safety_on", suite: "redteam", runs: 1, false_blocks: 0, unsafe_attempts: 1, unsafe_blocked: 1, unsafe_executed: 0, unsafe_refused: 0, wrong_target_writes: 0, runs_flagging_injection: 1 },
  ],
  methods: ["Retrieval: evals/datasets/retrieval-v1.yaml"],
  limitations: ["<script>alert(1)</script> small N"],
};

test("the component evaluations: each part on its own, counts as 'k of N', the runs it read linked, no percentage", () => {
  const html = render(<ComponentEvaluations view={{ reports: [REPORT] }} />);
  const text = textOf(html);
  assert.match(text, /Component evaluations/);
  assert.match(text, /never merged into an end-to-end success rate/);
  assert.match(text, /Retrieval · 42 runbook chunks · bm25, dense/);
  assert.match(text, /bm25 alert 0 of 17 9 of 17 0.142 0 of 17 0/);
  assert.match(text, /dense paraphrase 6 of 12 11 of 12 0.694 3 of 8 0/);
  assert.doesNotMatch(text, /0.094/, "per-partition rows stay in the report file");
  assert.match(text, /in the results of 13 of 17; displacing a genuine remediation in 0 of 17/);
  assert.match(text, /Memory · 76 hits · unsafe reuse 0/);
  assert.match(text, /Fix taken from memory no_rag\/test-cart-memory-leak/);
  assert.match(text, /Parallel when the cause sits behind the alert 4 of 4/);
  assert.match(text, /small_context 8 none 23 10,575 9.4/);
  assert.match(text, /redteam_safety_on red-team 1 — — 1 1 0 0/, "a red-team run has no false or other block: its block is the safe outcome");
  assert.match(text, /Never assessed \(the run stopped first\) 0/);
  const link = tags(html, "a").find((a) => (a.attrs.href ?? "").includes("batch="));
  assert.equal(link?.attrs.href, "#/evals?mode=scripted_regression&amp;batch=20261108-w4-scripted"); // as markup
  assert.ok(html.includes("&lt;script&gt;"), "report text is escaped");
  assert.equal(count(text, "%"), 0);
  // Progressive disclosure: retrieval opens by itself, the other sections on demand.
  assert.equal(tags(html, "details").filter((d) => "open" in d.attrs).length, 1);
});

test("no published component evaluation says how to make one", () => {
  const text = textOf(render(<ComponentEvaluations view={{ reports: [] }} />));
  assert.match(text, /No component evaluation published/);
  assert.match(text, /python -m evals.components/);
});
