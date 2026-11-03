/**
 * View helpers for the Evaluations page (GET /evals, GET /evals/{batch_id}; docs/evaluation.md).
 *
 * The four evaluation modes are shown apart and never share a number: every count here is
 * over ONE batch, and a batch holds runs of one mode only (evals/records.py refuses a mix).
 * Counts are written "k of N", never as percentages. A value a record does not hold reads as
 * not recorded; nothing is estimated in its place.
 *
 * Pure functions with type-only imports, so tests/evaluations.test.ts loads this file in Node.
 */

import type { BatchOutcomes, BatchSummary, EvalMode, EvalModeInfo, EvalRecord, EvalsView, StageTiming } from "../dto";
import type { Tone } from "../format";

export const NOT_RECORDED = "Not recorded";

// --- modes ----------------------------------------------------------------------------------------

/** The tabs, in the order docs/evaluation.md lists the modes. */
export const MODE_ORDER: readonly EvalMode[] = ["scripted_regression", "offline_replay", "real_llm", "real_kubernetes"];

export interface ModeGuide {
  /** The tab's label. */
  tab: string;
  /** What makes the decisions in this mode. */
  decisions: string;
  shows: string;
  doesNotShow: string;
  /** The script that makes a batch of this mode (null: nothing can yet). */
  madeBy: string | null;
  icon: "code" | "rewind" | "cpu" | "package";
}

/** Documentation of each mode (docs/evaluation.md, section 1): help text, not data. */
export const MODE_GUIDE: Readonly<Record<EvalMode, ModeGuide>> = {
  scripted_regression: {
    tab: "Scripted regression",
    decisions: "RECAP's scripted policy (fixed playbooks), not an LLM",
    shows: "That RECAP's own pipeline (routing, retrieval, memory, verifier, gateway, oracle) still behaves as before",
    doesNotShow: "Anything about language models",
    madeBy: "scripts/evaluate.py (evals/runner.py)",
    icon: "code",
  },
  offline_replay: {
    tab: "Offline replay",
    decisions: "A real model's answers, recorded in an earlier run and replayed offline",
    shows: "That the recorded decisions still pass RECAP's current code, offline, at $0",
    doesNotShow: "How a model would decide today, or how often",
    madeBy: "scripts/eval_replay.py",
    icon: "rewind",
  },
  real_llm: {
    tab: "Real LLM (exploratory)",
    decisions: "A real model now: local (Ollama) or a verified free plan (Groq Free)",
    shows: "What that model did on these incidents, one sample per run",
    doesNotShow: "A success rate, or the result on a real system",
    madeBy: "scripts/live_eval.py",
    icon: "cpu",
  },
  real_kubernetes: {
    tab: "Real Kubernetes",
    decisions: "A real model now, on a real Kubernetes cluster (the Kind demo cluster), with a person approving every write",
    shows: "What that model did on real faults in a real cluster, judged by the cluster's own health, one sample per run",
    doesNotShow: "A success rate, or how it would do under production traffic",
    madeBy: "scripts/live_eval.py --from-data",
    icon: "package",
  },
};

export function isMode(value: unknown): value is EvalMode {
  return typeof value === "string" && (MODE_ORDER as readonly string[]).includes(value);
}

/** The mode a route asks for (`?mode=`); the first tab when it names none or an unknown one. */
export function parseMode(raw: string | null | undefined): EvalMode {
  return isMode(raw) ? raw : MODE_ORDER[0];
}

/** The tab a key press moves to (arrow keys wrap; Home and End jump), or null for other keys. */
export function nextMode(current: EvalMode, key: string): EvalMode | null {
  const index = MODE_ORDER.indexOf(current);
  const last = MODE_ORDER.length - 1;
  switch (key) {
    case "ArrowRight":
    case "ArrowDown":
      return MODE_ORDER[index >= last ? 0 : index + 1];
    case "ArrowLeft":
    case "ArrowUp":
      return MODE_ORDER[index <= 0 ? last : index - 1];
    case "Home":
      return MODE_ORDER[0];
    case "End":
      return MODE_ORDER[last];
    default:
      return null;
  }
}

/** The server's own entry for a mode (status implemented / planned and its description). */
export function modeInfo(view: Pick<EvalsView, "modes">, mode: EvalMode): EvalModeInfo | null {
  return view.modes.find((m) => m.mode === mode) ?? null;
}

/** The batches of one mode, in the server's order (newest first). */
export function batchesOf(view: Pick<EvalsView, "batches">, mode: EvalMode): BatchSummary[] {
  return view.batches.filter((b) => b.mode === mode);
}

/** The batch a route asks for when it is one of these, else the newest. */
export function pickBatch(batches: readonly BatchSummary[], requested: string | null | undefined): BatchSummary | null {
  return batches.find((b) => b.batch_id === requested) ?? batches[0] ?? null;
}

/** The tab's second line: how many batches the mode has, or that it is planned. */
export function modeTabNote(view: Pick<EvalsView, "modes" | "batches">, mode: EvalMode): string {
  const info = modeInfo(view, mode);
  if (info?.status === "planned") return "planned";
  const n = batchesOf(view, mode).length;
  return `${n} ${n === 1 ? "batch" : "batches"}`;
}

// --- numbers --------------------------------------------------------------------------------------

function grouped(value: number, digits = 0): string {
  return value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** A whole count with thousands separators ("—" when the value is missing). */
export function fmtCount(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? grouped(value) : "—";
}

/** A count over a batch: "1 of 2". Never a percentage. */
export function kOfN(k: number, n: number): string {
  return `${fmtCount(k)} of ${fmtCount(n)}`;
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${fmtCount(n)} ${n === 1 ? one : many}`;
}

/** Seconds as measured: "0.70 s", "567.2 s", "1,379.8 s" ("—" when missing). */
export function fmtSeconds(seconds: number | null | undefined): string {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "—";
  return `${grouped(seconds, seconds < 10 ? 2 : 1)} s`;
}

/** "≈ 23 min" for two minutes or more (a reading aid next to fmtSeconds), else null. */
export function aboutMinutes(seconds: number | null | undefined): string | null {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 120) return null;
  return `≈ ${grouped(Math.round(seconds / 60))} min`;
}

/** An ISO timestamp as "2026-10-25 11:09 UTC" (the same for every viewer); null when unreadable. */
export function fmtUtc(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const t = Date.parse(iso.replace(/(\.\d{3})\d+/, "$1"));
  if (!Number.isFinite(t)) return null;
  const d = new Date(t);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
}

// --- sample size ----------------------------------------------------------------------------------

export interface SampleNote {
  label: string;
  text: string;
}

/**
 * The small-sample warning shown with every batch. `groups`: how many configurations share
 * the batch's runs (the scripted benchmark), so each has fewer than N.
 */
export function sampleNote(n: number, groups = 0): SampleNote {
  if (n <= 0) return { label: "No runs", text: "This batch holds no records, so it supports no reading at all." };
  const split =
    groups > 1 ? ` These ${fmtCount(n)} runs are spread over ${fmtCount(groups)} configurations, so each configuration has even fewer.` : "";
  if (n === 1) {
    return {
      label: "Small sample · N = 1",
      text: `One run is one sample: it shows what happened once, not how often it would happen.${split}`,
    };
  }
  return {
    label: `Small sample · N = ${fmtCount(n)}`,
    text: `Read every count as runs out of ${fmtCount(n)}, never as a percentage: “1 of 2” is one run of two, not 50%. No success rate or comparison is claimed from a sample this small.${split}`,
  };
}

// --- outcomes and failure categories ----------------------------------------------------------------

export type OutcomeIcon = "check" | "x" | "stop" | "alert" | "minus";

export interface CategoryInfo {
  label: string;
  /** One plain sentence: when a run gets this category (docs/evaluation.md, section 4). */
  meaning: string;
  tone: Tone;
  icon: OutcomeIcon;
}

/** The failure categories (evals/categorize.py), in the order the rules are checked. */
export const FAILURE_INFO: Readonly<Record<string, CategoryInfo>> = {
  cancelled: { label: "Cancelled", meaning: "Someone stopped the run.", tone: "warn", icon: "stop" },
  quota_cap: {
    label: "Quota cap",
    meaning: "A free plan's quota, or RECAP's own cap below it, deferred the run.",
    tone: "warn",
    icon: "stop",
  },
  budget_exhausted: {
    label: "Budget exhausted",
    meaning: "A run limit (steps, tool calls, model tokens or time) stopped the run.",
    tone: "warn",
    icon: "stop",
  },
  approval_rejected: {
    label: "Approval rejected",
    meaning: "The approver said no, or the approval request expired.",
    tone: "warn",
    icon: "stop",
  },
  safety_blocked: {
    label: "Safety blocked",
    meaning: "The safety policy, the evidence gate or the namespace allowlist refused the proposed fix.",
    tone: "warn",
    icon: "stop",
  },
  oracle_not_recovered: {
    label: "Oracle: not recovered",
    meaning: "A fix ran, and the independent oracle found the system still unhealthy.",
    tone: "bad",
    icon: "x",
  },
  provider_failure: {
    label: "Provider failure",
    meaning: "The model server failed, or no usable answer or fix came back.",
    tone: "bad",
    icon: "x",
  },
  error: {
    label: "Error",
    meaning: "Another error ended the run (for a replay: it left its recording); the detail names it.",
    tone: "bad",
    icon: "x",
  },
  open_disagreement: {
    label: "Open disagreement",
    meaning: "Supported root causes still conflicted after the follow-up round: a person decides.",
    tone: "warn",
    icon: "alert",
  },
  missed_crucial_tool: {
    label: "Missed a crucial tool",
    meaning: "No tool call named the root service, and no recorded observation showed it.",
    tone: "bad",
    icon: "x",
  },
  wrong_mechanism: {
    label: "Wrong mechanism",
    meaning: "Claims named the right service, but none with the right failure mechanism.",
    tone: "bad",
    icon: "x",
  },
  wrong_service: {
    label: "Wrong service",
    meaning: "No claim named the root service, not even as the failing dependency of an uncontradicted link.",
    tone: "bad",
    icon: "x",
  },
  insufficient_evidence: {
    label: "Insufficient evidence",
    meaning: "The investigation ended without a supported root cause: the right claim stayed unsupported, or no usable claim was made.",
    tone: "bad",
    icon: "x",
  },
  uncategorized: {
    label: "Not categorized",
    meaning: "The run did not recover, and its record names no failure category.",
    tone: "neutral",
    icon: "minus",
  },
};

const CATEGORY_ORDER = Object.keys(FAILURE_INFO);

export function categoryInfo(category: string): CategoryInfo {
  return (
    FAILURE_INFO[category] ?? {
      label: category.replace(/_/g, " "),
      meaning: "This dashboard has no description of this category; the run's failure detail says what happened.",
      tone: "bad",
      icon: "x",
    }
  );
}

export interface OutcomeView extends CategoryInfo {
  /** "success", a failure category, or "uncategorized". */
  key: string;
}

/** An outcome by its key: "success" (a verified recovery) or a failure category. */
export function outcomeFromKey(key: string): OutcomeView {
  if (key === "success") {
    return {
      key,
      label: "Verified recovery",
      meaning: "Resolved, and the independent oracle verified the recovery.",
      tone: "ok",
      icon: "check",
    };
  }
  return { key, ...categoryInfo(key) };
}

/** The outcome a filter or a matrix cell names: "success", the failure category, or "uncategorized". */
export function outcomeKey(record: Pick<EvalRecord, "success" | "failure">): string {
  return record.success ? "success" : record.failure || "uncategorized";
}

/** How one run ended, for an evaluation: a verified recovery or one failure category. */
export function recordOutcome(record: Pick<EvalRecord, "success" | "failure">): OutcomeView {
  return outcomeFromKey(outcomeKey(record));
}

export function outcomeLabel(key: string): string {
  return key === "success" ? "Verified recovery" : categoryInfo(key).label;
}

function byCountThenOrder(a: [string, number], b: [string, number]): number {
  if (b[1] !== a[1]) return b[1] - a[1];
  const ia = CATEGORY_ORDER.indexOf(a[0]);
  const ib = CATEGORY_ORDER.indexOf(b[0]);
  if (ia !== ib) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  return a[0].localeCompare(b[0]);
}

/** The batch's final states with their counts, most frequent first. */
export function stateCounts(outcomes: Pick<BatchOutcomes, "by_final_state">): [string, number][] {
  return Object.entries(outcomes.by_final_state ?? {}).sort(byCountThenOrder);
}

export interface FailureGroup {
  category: string;
  info: CategoryInfo;
  /** The server's count (BatchOutcomes.failures_by_category). */
  count: number;
  /** The records in this category, in batch order. */
  records: EvalRecord[];
}

/** The failure categories of a batch (the server's counts), each with its runs. */
export function failureGroups(outcomes: Pick<BatchOutcomes, "failures_by_category">, records: readonly EvalRecord[]): FailureGroup[] {
  return Object.entries(outcomes.failures_by_category ?? {})
    .sort(byCountThenOrder)
    .map(([category, count]) => ({
      category,
      info: categoryInfo(category),
      count,
      records: records.filter((r) => !r.success && (r.failure || "uncategorized") === category),
    }));
}

// --- groups: configurations and scenarios --------------------------------------------------------

export const NO_LABEL = "(no label)";

function labelOf(record: Pick<EvalRecord, "label">): string {
  return record.label?.trim() ? record.label.trim() : NO_LABEL;
}

/** A red-team scenario (the record's own predeclared answer says so). */
export function isRedTeam(record: Pick<EvalRecord, "expected">): boolean {
  return record.expected?.redteam === true;
}

export interface MatrixRow {
  label: string;
  n: number;
  successes: number;
  /** Every run of this configuration is a red-team scenario. */
  redteam: boolean;
  /** The runs per scenario (usually one). */
  cells: Record<string, EvalRecord[]>;
}

export interface ConfigMatrix {
  scenarios: string[];
  rows: MatrixRow[];
}

/**
 * Configurations × scenarios, one mark per run, when the batch's runs share configuration
 * labels (the scripted benchmark: each label is a configuration). Null when every run has its
 * own label (a real-model plan labels each run), or all share one: then there is nothing to
 * tell apart.
 */
export function configurationMatrix(records: readonly EvalRecord[]): ConfigMatrix | null {
  const rows = new Map<string, MatrixRow>();
  const scenarios: string[] = [];
  for (const record of records) {
    const label = labelOf(record);
    let row = rows.get(label);
    if (!row) {
      row = { label, n: 0, successes: 0, redteam: true, cells: {} };
      rows.set(label, row);
    }
    row.n += 1;
    if (record.success) row.successes += 1;
    if (!isRedTeam(record)) row.redteam = false;
    const cell = row.cells[record.scenario_id] ?? [];
    cell.push(record);
    row.cells[record.scenario_id] = cell;
    if (!scenarios.includes(record.scenario_id)) scenarios.push(record.scenario_id);
  }
  if (rows.size <= 1 || rows.size === records.length) return null;
  return { scenarios, rows: [...rows.values()] };
}

export interface ScenarioGroup {
  scenario: string;
  family: string;
  n: number;
  successes: number;
  outcomes: [string, number][];
}

/** The runs of a batch per scenario (first appearance order), with their outcomes. */
export function scenarioGroups(records: readonly EvalRecord[]): ScenarioGroup[] {
  const groups = new Map<string, ScenarioGroup & { counts: Map<string, number> }>();
  for (const record of records) {
    let group = groups.get(record.scenario_id);
    if (!group) {
      group = { scenario: record.scenario_id, family: record.incident_family, n: 0, successes: 0, outcomes: [], counts: new Map() };
      groups.set(record.scenario_id, group);
    }
    group.n += 1;
    if (record.success) group.successes += 1;
    const key = outcomeKey(record);
    group.counts.set(key, (group.counts.get(key) ?? 0) + 1);
  }
  return [...groups.values()].map(({ counts, ...group }) => ({
    ...group,
    outcomes: [...counts.entries()].sort((a, b) => (a[0] === "success" ? -1 : b[0] === "success" ? 1 : byCountThenOrder(a, b))),
  }));
}

/** The decision sources of a batch's records (provider classes), in order of appearance. */
export function providerClasses(records: readonly Pick<EvalRecord, "provider_class" | "model">[]): { origin: string; model: string | null }[] {
  const seen = new Map<string, string | null>();
  for (const record of records) {
    const origin = record.provider_class || "";
    if (!seen.has(origin)) seen.set(origin, record.model ?? null);
  }
  return [...seen.entries()].map(([origin, model]) => ({ origin, model }));
}

// --- record cells ---------------------------------------------------------------------------------

/** "parallel: telemetry_investigator + infrastructure_investigator" → its routing and agents. */
export function parseTopology(entry: string): { mode: string; agents: string[] } {
  const colon = entry.indexOf(":");
  if (colon === -1) return { mode: entry.trim(), agents: [] };
  return {
    mode: entry.slice(0, colon).trim(),
    agents: entry
      .slice(colon + 1)
      .split("+")
      .map((a) => a.trim())
      .filter(Boolean),
  };
}

const SEARCH_MODES: Record<string, string> = { bm25: "BM25", hybrid: "hybrid", off: "off" };

/** The runbook search mode a record names ("BM25", "hybrid", "off"), or not recorded. */
export function retrievalLabel(mode: string | null | undefined): string {
  if (!mode) return NOT_RECORDED;
  return SEARCH_MODES[mode] ?? mode;
}

export interface TokenView {
  /** What the model server reported ("74,935 in · 2,182 out"), or null when it reported none. */
  reported: string | null;
  /** Why there is no reported count. */
  missing: string | null;
  /** RECAP's own estimate of the context it built, always labelled as one. */
  estimate: string;
}

export function tokenView(record: Pick<EvalRecord, "input_tokens" | "output_tokens" | "estimated_tokens" | "model_requests">): TokenView {
  const none = record.input_tokens === null && record.output_tokens === null;
  const side = (value: number | null) => (value === null ? "not reported" : fmtCount(value));
  return {
    reported: none ? null : `${side(record.input_tokens)} in · ${side(record.output_tokens)} out`,
    missing: none ? (record.model_requests > 0 ? "Not reported by the model server" : "No model request was sent") : null,
    estimate: fmtCount(record.estimated_tokens),
  };
}

/** Seconds the run waited at AWAITING_APPROVAL (the approver's time), or null. */
export function approvalSeconds(stages: readonly StageTiming[] | null | undefined): number | null {
  const stage = (stages ?? []).find((s) => s.stage === "AWAITING_APPROVAL");
  return stage ? stage.seconds : null;
}

export interface TimeView {
  run: string;
  about: string | null;
  /** Summed model request time, or why there is none. */
  model: string | null;
  /** Time a request waited inside the model server, when recorded. */
  wait: string | null;
  /** The approver's time, when it took a second or more (every stage is in the record). */
  approval: string | null;
}

export function timeView(
  record: Pick<EvalRecord, "seconds" | "model_seconds" | "model_wait_seconds" | "model_requests" | "stages">,
): TimeView {
  const asked = record.model_requests > 0;
  const approval = approvalSeconds(record.stages);
  return {
    run: fmtSeconds(record.seconds),
    about: aboutMinutes(record.seconds),
    model:
      record.model_seconds !== null
        ? `model requests ${fmtSeconds(record.model_seconds)} (summed)`
        : asked
          ? `model request time: ${NOT_RECORDED.toLowerCase()}`
          : null,
    wait:
      record.model_wait_seconds !== null
        ? `waited in the model server ${fmtSeconds(record.model_wait_seconds)}`
        : asked
          ? `model wait: ${NOT_RECORDED.toLowerCase()}`
          : null,
    approval: approval !== null && approval >= 1 ? `approval wait ${fmtSeconds(approval)}` : null,
  };
}

/** A stage's meaning next to its time (only AWAITING_APPROVAL needs one). */
export function stageNote(stage: string): string | null {
  return stage === "AWAITING_APPROVAL" ? "the approver's time, kept apart from RECAP's own work" : null;
}

/** A value of the predeclared answer, as text. */
export function fmtValue(value: unknown): string {
  if (value === null || value === undefined) return NOT_RECORDED;
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "string") return value || "(empty)";
  if (typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.length ? value.map(fmtValue).join(", ") : "none";
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    return entries.length ? entries.map(([k, v]) => `${k}=${fmtValue(v)}`).join(", ") : "none";
  }
  return String(value);
}

const EXPECTED_ORDER = ["root_cause_service", "mechanism", "fix", "fix_params", "redteam", "evades_scanner"];

/** The predeclared answer's fields, the known ones first. */
export function expectedEntries(expected: Record<string, unknown> | null | undefined): [string, string][] {
  const entries = Object.entries(expected ?? {});
  const rank = (key: string) => {
    const i = EXPECTED_ORDER.indexOf(key);
    return i === -1 ? EXPECTED_ORDER.length : i;
  };
  return entries
    .sort((a, b) => rank(a[0]) - rank(b[0]) || a[0].localeCompare(b[0]))
    .map(([key, value]) => [key, fmtValue(value)]);
}

/** A memory hit's fields, in a fixed order (episode, partition, similarity, verdicts, reason). */
export function memoryHitFields(hit: Record<string, unknown>): [string, string][] {
  const order = ["episode_id", "partition", "similarity", "verified", "safe_to_reuse", "reason"];
  const keys = [...order.filter((k) => k in hit), ...Object.keys(hit).filter((k) => !order.includes(k)).sort()];
  return keys.map((key) => [key, fmtValue(hit[key])]);
}

/** Tool calls per caller, most first ("controller" = the writes sent to the target). */
export function toolCallRows(byAgent: Record<string, number> | null | undefined): [string, number][] {
  return Object.entries(byAgent ?? {}).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

// --- commands (docs/evaluation.md, section 6) -------------------------------------------------------

export interface CliCommand {
  title: string;
  command: string;
  note: string;
}

/** The exact commands that make each mode's batches, run from the repository root. */
export const CLI_COMMANDS: Readonly<Record<EvalMode, readonly CliCommand[]>> = {
  scripted_regression: [
    {
      title: "Every configuration",
      command: ".venv/bin/python scripts/evaluate.py",
      note: "Seconds; the scripted policy, no model. Also copies the report to evals/reports/latest/ and publishes its batch.",
    },
    {
      title: "Keep the committed report unchanged",
      command: ".venv/bin/python scripts/evaluate.py --no-latest",
      note: "Writes only evals/reports/runs/<report id>/ (git-ignored).",
    },
  ],
  offline_replay: [
    {
      title: "Every committed recording",
      command: ".venv/bin/python scripts/eval_replay.py --out /tmp/recap-replay",
      note: "Seconds; no model, no network. Each recording is checked against its pinned SHA-256 first.",
    },
    {
      title: "One recording",
      command: ".venv/bin/python scripts/eval_replay.py --out /tmp/recap-replay --only qwen3-8b-bad-deployment",
      note: "Exits 1 when a recording is refused or a replay ends in an error (for example a ReplayMismatch after a prompt change).",
    },
  ],
  real_llm: [
    {
      title: "Local model (Ollama, qwen3:8b installed)",
      command: ".venv/bin/python scripts/live_eval.py evals/plans/m3-real-models.yaml --out /tmp/recap-live",
      note: "Minutes to hours on a laptop; $0. Every plan run is one real_llm batch.",
    },
    {
      title: "Groq free plan: consent for this batch",
      command: ".venv/bin/python scripts/live_eval.py evals/plans/m3-groq-free.yaml --out /tmp/recap-groq --confirm-free-plan",
      note: "--confirm-free-plan is needed on every run of the script, on top of the free-tier guard's own checks.",
    },
    {
      title: "One run of a plan",
      command: ".venv/bin/python scripts/live_eval.py evals/plans/m4-comparisons.yaml --out /tmp/recap-m4 --only unfamiliar-single",
      note: "--only runs just the named runs of the plan.",
    },
    {
      title: "Build a batch from runs already made",
      command: ".venv/bin/python scripts/live_eval.py --from-data --data-dir DATA --out OUT [PLAN]",
      note: "No model is called: the batch is rebuilt from the stored records and traces.",
    },
  ],
  real_kubernetes: [
    {
      title: "One run on the Kind cluster, approved by you",
      command:
        "RECAP_TARGET=kind-demo RECAP_DATA_DIR=$HOME/recap-kind-runs .venv/bin/python scripts/demo.py --scenario kind-bad-release --provider ollama-vm --approve ask",
      note: "About half an hour with the model on a VM without a GPU. Needs the cluster and its tunnel (infra/aws/ec2.sh up, then tunnel). The fix waits until you type the target service's name.",
    },
    {
      title: "Build the batch from those runs",
      command:
        "RECAP_TARGET=kind-demo .venv/bin/python scripts/live_eval.py --from-data --data-dir $HOME/recap-kind-runs --out /tmp/recap-kind-batch",
      note: "No model is called: every finished real-model run in that folder, read with the Kind target's profile. A folder that mixes the simulator with a cluster is refused.",
    },
  ],
};
