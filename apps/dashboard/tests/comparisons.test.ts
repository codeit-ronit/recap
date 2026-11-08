/**
 * M6 W4 helpers (src/view/evals.ts): what a configuration set its compared switch to, signed
 * differences, which measures moved, whether a comparison measured its switch alone, the
 * scenario sets' names, and the retrieval rows shown (every query of a kind).
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import type { EvalConfiguration, MetricDifference, RetrievalScore } from "../src/dto";
import {
  comparisonIntegrity,
  countedScenarios,
  metricLabel,
  movedDifferences,
  retrievalRows,
  setLabel,
  settingText,
  signedNumber,
  switchSetting,
} from "../src/view/evals.ts";

const CONFIGS: EvalConfiguration[] = [
  { name: "full_adaptive", suite: "test", description: "", switches: { strategy: "adaptive" } },
  { name: "tight_tools", suite: "test", description: "", switches: { budget: { max_steps: 30, max_tool_calls: 15 } } },
];

test("a switch a configuration leaves out reads 'default'; a nested one reads as key-value pairs", () => {
  assert.equal(switchSetting(CONFIGS, "full_adaptive", "strategy"), "adaptive");
  assert.equal(switchSetting(CONFIGS, "full_adaptive", "budget"), "default");
  assert.equal(switchSetting(CONFIGS, "tight_tools", "budget"), "max_steps 30, max_tool_calls 15");
  assert.equal(switchSetting(CONFIGS, "tight_tools", "budget.max_tool_calls"), "15");
  assert.equal(switchSetting(CONFIGS, "full_adaptive", "budget.max_tool_calls"), "default");
  assert.equal(switchSetting(undefined, "x", "strategy"), "default");
  assert.equal(settingText(false), "false");
});

test("differences carry their sign, and a missing one reads as a dash, never as zero", () => {
  assert.equal(signedNumber(1.5), "+1.50");
  assert.equal(signedNumber(-2.88), "−2.88");
  assert.equal(signedNumber(-3), "−3");
  assert.equal(signedNumber(0), "±0");
  assert.equal(signedNumber(null), "—");
  assert.equal(signedNumber(Number.NaN), "—");
});

test("only the measures that changed in some pair are listed, with words for each", () => {
  const differences: MetricDifference[] = [
    { metric: "tool_calls", n: 8, baseline_mean: 12, variant_mean: 9, mean_difference: -3, changed: 8 },
    { metric: "rounds", n: 8, baseline_mean: 1, variant_mean: 1, mean_difference: 0, changed: 0 },
    { metric: "context_dropped", n: 0, baseline_mean: null, variant_mean: null, mean_difference: null, changed: 0 },
  ];
  assert.deepEqual(
    movedDifferences({ differences }).map((d) => d.metric),
    ["tool_calls"],
  );
  assert.equal(metricLabel("steps"), "decisions");
  assert.equal(metricLabel("some_new_metric"), "some new metric");
});

test("a comparison measured its switch alone only with no other recorded difference and no unpaired run", () => {
  assert.deepEqual(comparisonIntegrity({ unexpected: [], unpaired: [] }), { ok: true, label: "only the switch", items: [] });
  const flagged = comparisonIntegrity({ unexpected: ["prompt_version: s1 (v1 vs v2)"], unpaired: ["s4"] });
  assert.equal(flagged.ok, false);
  assert.equal(flagged.label, "2 other differences");
  assert.deepEqual(flagged.items, ["prompt_version: s1 (v1 vs v2)", "unpaired: s4"]);
});

test("scenario sets have names for people, and the retrieval table shows every query of a kind", () => {
  assert.equal(setLabel("test"), "development");
  assert.equal(setLabel("held_out"), "held-out");
  assert.equal(setLabel(""), "unlabelled");
  const score = (partition: string): RetrievalScore => ({
    ranker: "bm25",
    kind: "alert",
    partition,
    n: 1,
    hit_at_1: 0,
    hit_at_2: 0,
    hit_at_4: 1,
    mrr: 0.25,
    remediation_at_2: 0,
    remediation_at_4: 0,
    remediation_n: 1,
    wrong_mechanism_at_1: 0,
    misses: [],
  });
  assert.deepEqual(
    retrievalRows({ scores: [score(""), score("test")] }).map((s) => s.partition),
    [""],
  );
  assert.deepEqual(retrievalRows(null), []);
});

test("a scenario repeated once per seed is shown once, with its count", () => {
  assert.equal(countedScenarios(["a", "b", "a", "a"]), "a ×3, b");
  assert.equal(countedScenarios([]), "");
});
