/**
 * View helpers for Published Runs (pages/Published.tsx) and for a published run on any run
 * page (the shell's historical banner): the label every historical run carries, its target in
 * words, SREGym's grader kept apart from RECAP's oracle, and every real-cluster run in one list
 * (the published ones with their trace, the others known only from their evaluation record).
 *
 * Every value comes from GET /published or a run's summary (apps/api/dto.py PublishedView,
 * PublishedProvenance); a value the server did not report reads "Not recorded", never a guess.
 *
 * Pure functions with type-only imports, so tests/published.test.ts loads this file directly.
 */

import type { PublishedProvenance, PublishedView, RecordWithoutTraceView, RunSummary } from "../dto";
import type { Tone } from "../format";

/** The label every published run carries, on its own page and on every run page. */
export const HISTORICAL_LABEL = "Recorded real Kubernetes execution — historical, read-only";

const NOT_RECORDED = "Not recorded";

/** A real cluster's kind in words (RECAP_TARGET profiles: kind-demo, sregym-hotel). */
export function targetName(target: string | null | undefined): string {
  switch (target) {
    case "kind":
      return "Kind cluster";
    case "sregym":
      return "SREGym cluster"; // SREGym's own Kind cluster, which its harness deploys and breaks
    default:
      return target ? target : "Target not recorded";
  }
}

/** "2026-11-07" from an ISO timestamp; Not recorded without one. */
export function dayOf(iso: string | null | undefined): string {
  const match = /^(\d{4}-\d{2}-\d{2})/.exec(iso ?? "");
  return match ? match[1] : NOT_RECORDED;
}

type HistoricalFacts = Pick<RunSummary, "model" | "provider" | "started_at" | "environment_kind"> & {
  published?: PublishedProvenance | null;
};

/** What a published run is, in plain words: where and when it ran, who decided, and that nothing runs now. */
export function historicalWords(summary: HistoricalFacts): string {
  const target = targetName(summary.published?.target ?? summary.environment_kind);
  const model = summary.model ? `${summary.model}${summary.provider ? ` (${summary.provider})` : ""}` : "its model";
  return (
    `The record of a real run on the ${target}, on ${dayOf(summary.started_at)}: ${model} made its decisions then, ` +
    "and a person approved its fix. Nothing runs now: no model is called and no cluster is touched. " +
    "Its approvals are records of a past decision, never permissions."
  );
}

export interface GraderView {
  label: string;
  tone: Tone;
  detail: string;
}

/**
 * The benchmark's own grader, kept apart from RECAP's oracle: its verdict when one was
 * recorded; for a SREGym run without one, "not graded"; for Kind (no benchmark), nothing.
 */
export function graderView(target: string, verdict: "pass" | "fail" | null | undefined, benchmark?: string | null): GraderView | null {
  if (verdict) {
    return {
      label: `SREGym's grader: ${verdict}`,
      tone: verdict === "pass" ? "ok" : "bad",
      detail: `${benchmark ?? "The benchmark's own grader"}. Its verdict is kept apart from RECAP's independent oracle.`,
    };
  }
  if (target === "sregym") {
    return { label: "Not graded by SREGym", tone: "neutral", detail: "No SREGym grader verdict was recorded for this run." };
  }
  return null;
}

/** One row of the full list: a published run (with its trace) or an evaluation record only. */
export interface RealClusterRow {
  runId: string;
  target: string;
  scenarioId: string;
  startedAt: string | null;
  finalState: string;
  verified: boolean;
  failure: string;
  detail: string;
  approvals: string[];
  grader: GraderView | null;
  /** The run's full trace was published, so its run pages open. */
  traceOpens: boolean;
}

/** Every real-cluster run in the published batches, oldest first, grouped by target. */
export function realClusterRows(view: Pick<PublishedView, "runs" | "records_without_trace" | "benchmarks">): { target: string; rows: RealClusterRow[] }[] {
  const rows: RealClusterRow[] = [
    ...view.runs.map((run): RealClusterRow => {
      const provenance = run.published;
      const target = provenance?.target ?? run.environment_kind ?? "";
      return {
        runId: run.run_id,
        target,
        scenarioId: run.scenario_id,
        startedAt: run.started_at,
        finalState: run.state,
        verified: run.recovered,
        failure: "",
        detail: run.outcome_note,
        approvals: provenance?.approvals ?? [],
        grader: graderView(target, provenance?.benchmark_verdict, provenance?.benchmark),
        traceOpens: true,
      };
    }),
    ...view.records_without_trace.map(
      (record: RecordWithoutTraceView): RealClusterRow => ({
        runId: record.run_id,
        target: record.target,
        scenarioId: record.scenario_id,
        startedAt: record.started_at,
        finalState: record.final_state,
        verified: record.oracle_verified,
        failure: record.failure,
        detail: record.failure_detail,
        approvals: record.approvals,
        grader: graderView(record.target, record.benchmark_verdict, view.benchmarks[record.batch_id]),
        traceOpens: false,
      }),
    ),
  ];
  rows.sort((a, b) => (a.startedAt ?? "").localeCompare(b.startedAt ?? ""));
  const targets = [...new Set(rows.map((row) => row.target))].sort((a, b) => (a === "kind" ? -1 : b === "kind" ? 1 : a.localeCompare(b)));
  return targets.map((target) => ({ target, rows: rows.filter((row) => row.target === target) }));
}

/** How the full list adds up, per target: runs, verified recoveries, traces published. Counts, never a rate. */
export function rowCounts(rows: readonly RealClusterRow[]): { runs: number; verified: number; traces: number } {
  return {
    runs: rows.length,
    verified: rows.filter((row) => row.verified).length,
    traces: rows.filter((row) => row.traceOpens).length,
  };
}
