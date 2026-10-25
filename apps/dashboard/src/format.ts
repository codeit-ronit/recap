/**
 * Formatting helpers: tones (colors) for lifecycle states and verdicts, number/time
 * formatting, honest labels for the environment, provider and cost, `describeEvent`,
 * which turns one TraceEvent into a readable one-line description for the live feed, and
 * the M3 run facts (search mode, memory partitions, disagreements, budget use).
 * Payload keys are read defensively (see types.ts).
 *
 * This module imports types only, so the Node tests in ../tests can load it directly.
 */

import type { IconName } from "./components/Icon";
import type {
  AgentDecisionPayload,
  AgentFinishPayload,
  AgentStartPayload,
  ApprovalDecidedPayload,
  ApprovalRequestedPayload,
  BudgetPayload,
  ContextPackPayload,
  DiscoveryPayload,
  DocRef,
  EnvironmentInfo,
  EvidenceCheckPayload,
  InjectionPayload,
  InvestigationRoundPayload,
  LlmCallPayload,
  MemoryAssessHit,
  MemoryAssessPayload,
  MemorySearchHit,
  MemorySearchPayload,
  MemoryWritePayload,
  MitigationExecutedPayload,
  OrchestrationPayload,
  PolicyPayload,
  ProposalPayload,
  ProviderInfo,
  RecoveryPayload,
  RetrievalPayload,
  RoutingPayload,
  RunConfig,
  RunSummaryPayload,
  StatePayload,
  SystemSummary,
  ToolCallPayload,
  ToolResultPayload,
  TraceEvent,
  Usage,
  VerificationMergePayload,
} from "./types";

/** Semantic color roles (see styles.css): accent = normal flow, ok/warn/bad = status. */
export type Tone = "neutral" | "accent" | "ok" | "warn" | "bad" | "info";

// --- lifecycle ------------------------------------------------------------------------

/** The happy path of the lifecycle state machine (recap/core/lifecycle.py). */
export const MAIN_FLOW = [
  "CREATED",
  "RETRIEVING",
  "INVESTIGATING",
  "PROPOSING",
  "AWAITING_APPROVAL",
  "MITIGATING",
  "VERIFYING",
  "RESOLVED",
] as const;

export const TERMINAL_STATES: ReadonlySet<string> = new Set([
  "RESOLVED",
  "FAILED",
  "BLOCKED",
  "BUDGET_EXHAUSTED",
  "QUOTA_EXHAUSTED",
]);

const STATE_LABELS: Record<string, string> = {
  CREATED: "Created",
  RETRIEVING: "Retrieving",
  INVESTIGATING: "Investigating",
  PROPOSING: "Proposing",
  AWAITING_APPROVAL: "Awaiting approval",
  MITIGATING: "Mitigating",
  VERIFYING: "Verifying",
  RESOLVED: "Resolved",
  FAILED: "Failed",
  BLOCKED: "Blocked",
  BUDGET_EXHAUSTED: "Budget exhausted",
  QUOTA_EXHAUSTED: "Quota exhausted (deferred)",
};

export function stateLabel(state: string | null | undefined): string {
  if (!state) return "—";
  return STATE_LABELS[state] ?? humanize(state.toLowerCase());
}

export function isTerminal(state: string | null | undefined): boolean {
  return !!state && TERMINAL_STATES.has(state);
}

export function stateTone(state: string | null | undefined): Tone {
  switch (state) {
    case "RESOLVED":
      return "ok";
    case "FAILED":
    case "BLOCKED":
      return "bad";
    case "BUDGET_EXHAUSTED":
    case "QUOTA_EXHAUSTED":
    case "AWAITING_APPROVAL":
      return "warn";
    case "CREATED":
    case null:
    case undefined:
    case "":
      return "neutral";
    default:
      return "accent";
  }
}

export function verdictTone(verdict: string | null | undefined): Tone {
  if (verdict === "ALLOW") return "ok";
  if (verdict === "APPROVAL_REQUIRED") return "warn";
  if (verdict === "DENY") return "bad";
  return "neutral";
}

export function verdictLabel(verdict: string | null | undefined): string {
  if (verdict === "ALLOW") return "Allow";
  if (verdict === "APPROVAL_REQUIRED") return "Approval required";
  if (verdict === "DENY") return "Deny";
  return verdict ?? "—";
}

export function outcomeTone(outcome: string | null | undefined): Tone {
  if (outcome === "success") return "ok";
  if (outcome === "failed" || outcome === "blocked") return "bad";
  if (outcome === "budget_exhausted") return "warn";
  return "neutral";
}

export function hypothesisTone(status: string | null | undefined): Tone {
  if (status === "supported") return "ok";
  if (status === "contradicted") return "bad";
  if (status === "insufficient") return "warn";
  return "neutral";
}

/** Trusted risk tiers from configs/policies.yaml; unknown tools count as unsafe. */
export function riskTone(risk: string | null | undefined): Tone {
  if (risk === "read") return "neutral";
  if (risk === "reversible_write") return "warn";
  return "bad"; // high_risk, destructive, unknown
}

export function actionResultTone(result: string | null | undefined): Tone {
  if (result === "recovered") return "ok";
  if (result === "not_recovered") return "warn";
  return "bad"; // denied, rejected, expired, error
}

export function flagTone(flag: string): Tone {
  return flag === "prompt_injection_suspected" ? "warn" : "neutral";
}

// --- defensive value readers (payloads come from several backend modules) -------------

export function arr<T = unknown>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

export function str(value: unknown, fallback = ""): string {
  if (value === null || value === undefined) return fallback;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return fallback;
  }
}

export function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function obj(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function humanize(text: string): string {
  return text.replace(/_/g, " ");
}

/** "telemetry_investigator" -> "Telemetry investigator" */
export function roleLabel(role: string | null | undefined): string {
  if (!role) return "Controller";
  const words = humanize(role);
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// --- numbers ---------------------------------------------------------------------------

const intFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

export function fmtInt(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? "—"
    : intFormat.format(value);
}

export function fmtNum(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toLocaleString("en-US", {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

export function fmtScore(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value) ? "—" : value.toFixed(2);
}

export function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "—";
  if (ms < 1) return "<1 ms";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes} min ${seconds} s`;
}

// --- time ------------------------------------------------------------------------------

/** Parse a Python ISO timestamp (microseconds are trimmed to milliseconds first). */
export function tsMs(iso: string | null | undefined): number {
  if (!iso) return Number.NaN;
  return Date.parse(iso.replace(/(\.\d{3})\d+/, "$1"));
}

export function fmtTime(iso: string | null | undefined): string {
  const t = tsMs(iso);
  if (!Number.isFinite(t)) return "—";
  return new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function fmtDateTime(iso: string | null | undefined): string {
  const t = tsMs(iso);
  if (!Number.isFinite(t)) return "—";
  return new Date(t).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function fmtRelative(iso: string | null | undefined, now = Date.now()): string {
  const t = tsMs(iso);
  if (!Number.isFinite(t)) return "—";
  const s = Math.round((now - t) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86_400) return `${Math.floor(s / 3600)} h ago`;
  return fmtDateTime(iso);
}

/** "14:52 left" until an ISO deadline, or "expired". */
export function fmtCountdown(iso: string | null | undefined, now = Date.now()): string {
  const t = tsMs(iso);
  if (!Number.isFinite(t)) return "—";
  const left = Math.floor((t - now) / 1000);
  if (left <= 0) return "expired";
  const m = Math.floor(left / 60);
  const s = left % 60;
  return `${m}:${String(s).padStart(2, "0")} left`;
}

// --- misc text -------------------------------------------------------------------------

export function shortDigest(digest: string | null | undefined, n = 16): string {
  return digest ? digest.slice(0, n) : "—";
}

/** {service: "checkout", limit: 20} -> "service=checkout, limit=20" */
export function fmtArgs(args: unknown): string {
  return Object.entries(obj(args))
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : str(v)}`)
    .join(", ");
}

export function fmtCall(tool: string, args: unknown): string {
  return `${tool}(${fmtArgs(args)})`;
}

export function yesNo(value: boolean | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value ? "yes" : "no";
}

// --- honest labels: environment, provider, cost ------------------------------------------

export const SIMULATED_LABEL = "SIMULATED LOCAL ENVIRONMENT · V1";

export interface EnvironmentBadge {
  label: string;
  /** warn (amber) = the simulator · bad (red) = a live system · neutral (muted) = not known yet */
  tone: Tone;
  title: string;
}

/**
 * The top-bar environment badge. Its color comes only from GET /system
 * (`environment.is_simulated`), never from an assumption. Until /system answers, the badge
 * is muted and shows the /health label (or the simulator label before anything loads).
 */
export function environmentBadge(
  env: Pick<EnvironmentInfo, "kind" | "is_simulated"> | null | undefined,
  healthLabel?: string,
): EnvironmentBadge {
  if (!env) {
    return {
      label: healthLabel || SIMULATED_LABEL,
      tone: "neutral",
      title: "Checking which environment is plugged in…",
    };
  }
  if (env.is_simulated) {
    return {
      label: SIMULATED_LABEL,
      tone: "warn",
      title: "Incidents run against a deterministic local simulator. No real system is changed.",
    };
  }
  return {
    label: `LIVE ENVIRONMENT (${env.kind}) · V1`,
    tone: "bad",
    title: "A live environment adapter is configured: approved mitigations change a real system.",
  };
}

export interface OriginBadge {
  label: string;
  tone: Tone;
  title: string;
}

/**
 * The run-origin badge: where a run's decisions came from. A scripted or replayed run must
 * never look like live model reasoning, so the label comes only from ProviderInfo.origin
 * (or, for runs recorded before origins existed, from its kind).
 */
export function originBadge(provider: Partial<ProviderInfo> | null | undefined): OriginBadge {
  const origin = provider?.origin ?? (provider?.kind === "scripted" ? "scripted" : provider?.kind);
  switch (origin) {
    case "scripted":
      return { label: "SCRIPTED", tone: "neutral", title: "Scripted baseline policy: fixed playbooks, NOT an LLM." };
    case "local_llm":
      return { label: "LOCAL LLM", tone: "accent", title: "Real inference by a model on this machine (Ollama). No paid or remote API, $0." };
    case "free_hosted_llm":
      return { label: "FREE HOSTED LLM", tone: "accent", title: "Real inference on a free plan, checked before every request." };
    case "replay":
      return { label: "REPLAY", tone: "warn", title: "Recorded answers of an earlier real-model run. No model was called." };
    case "paid_llm":
      return { label: "PAID LLM", tone: "bad", title: "A billed API (disabled for this $0 project)." };
    default:
      return { label: "ORIGIN UNKNOWN", tone: "neutral", title: "This run did not record where its decisions came from." };
  }
}

export function providerLabel(provider: Partial<ProviderInfo> | null | undefined): string {
  if (!provider?.name) return "provider unknown";
  const badge = originBadge(provider);
  if (badge.label === "SCRIPTED") return `${provider.name} — scripted, not an LLM`;
  const model = provider.model ? ` (${provider.model})` : "";
  return `${provider.name} — ${badge.label.toLowerCase()}${model}`;
}

/**
 * One line about what a run cost. The backend's own line (run.summary `cost`) always wins.
 * Without it (e.g. a run that stopped before its summary), only providers that cannot be
 * billed are known to cost nothing; for any other provider this page cannot tell.
 */
export function costLabel(summaryCost: string | null | undefined, provider: Partial<ProviderInfo> | null | undefined): string {
  if (summaryCost) return summaryCost;
  switch (originBadge(provider).label) {
    case "SCRIPTED":
      return "$0.00 external API charged (deterministic mode)";
    case "LOCAL LLM":
      return "$0.00 - local model on this machine";
    case "REPLAY":
      return "$0.00 - replayed recording: no model was called";
    default:
      return "cost unknown (estimated tokens only)";
  }
}

// --- trace events -> one readable line --------------------------------------------------

/**
 * A text fragment. `code` fragments render in monospace (ids, tool names, digests); `quote`
 * fragments are untrusted text an agent's provider wrote (a search query, a claim, a summary):
 * shown quoted, as data, never as RECAP's words and never as markup. `mixed`: the quote may
 * still hold a note RECAP added that could not be told apart (see splitSummary).
 */
export type Part = string | { code: string } | { quote: string; mixed?: boolean };

/** What describeEvent may know beyond the event itself (the live feed passes the whole trace). */
export interface FeedContext {
  /** RECAP's notes on each agent's model requests (llm.call `notes`), see modelNotesByAgent. */
  notes?: ReadonlyMap<string, readonly string[]>;
  /** Whether a runbook file is red-team overlay content (default: under fixtures/knowledge-overlays/). */
  isOverlay?: (docPath: string) => boolean;
}

// Mirrors OVERLAY_ROOT in view/retrieval.ts (this module imports no runtime values).
const OVERLAY_ROOT = "fixtures/knowledge-overlays/";
// A release version as the verifier reads it: only such a value is shown inside RECAP's text.
const VERSION_SHAPE = /^[vV]?[0-9]+\.[0-9]+\.[0-9]+$/;

export interface EventLine {
  icon: IconName;
  tone: Tone;
  tag: string;
  parts: Part[];
  chips?: { label: string; tone: Tone }[];
  /** Routine internals (context packs, agent decisions, read ALLOWs) hidden in "key events". */
  internal?: boolean;
}

function code(text: string): Part {
  return { code: text };
}

function quote(text: string, mixed = false): Part {
  return mixed ? { quote: text, mixed } : { quote: text };
}

/** An agent's summary: the provider's words quoted, RECAP's own notes as plain text after them. */
function summaryParts(summary: string, outcome: string, notes: readonly string[]): Part[] {
  const split = splitSummary(summary, outcome, notes);
  return [
    ...(split.said ? [" — ", quote(split.said, !split.sure)] : []),
    ...(split.notes.length ? [`${split.said ? " · " : " — "}RECAP's note: ${split.notes.join("; ")}`] : []),
  ];
}

/** "1 tool call", "2 tool calls". */
export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** ["bad_deployment", "bad_deployment", "resource_pressure"] -> "bad deployment ×2, resource pressure" */
function countList(values: string[]): string {
  const counts = new Map<string, number>();
  for (const v of values) counts.set(v, (counts.get(v) ?? 0) + 1);
  return [...counts].map(([v, n]) => `${humanize(v)}${n > 1 ? ` ×${n}` : ""}`).join(", ");
}

/** "telemetry_investigator (4) and infrastructure_investigator (5)" with the roles as code. */
function agentCounts(made: [string, number | null][]): Part[] {
  if (!made.length) return ["agents not recorded"];
  if (made.length === 1) return [code(made[0][0])];
  const parts: Part[] = [];
  made.forEach(([role, n], i) => {
    if (i) parts.push(i === made.length - 1 ? " and " : ", ");
    parts.push(code(role), ` (${n ?? "?"})`);
  });
  return parts;
}

export function describeEvent(e: TraceEvent, ctx: FeedContext = {}): EventLine {
  const p = e.payload ?? {};
  const who = e.agent ?? "";
  const isOverlay = ctx.isOverlay ?? ((path: string) => path.replace(/^\.\//, "").startsWith(OVERLAY_ROOT));
  switch (e.category) {
    case "state.transition": {
      const q = p as StatePayload;
      const to = str(q.to, "?");
      if (!q.from) {
        const symptoms = arr<string>(q.incident?.symptoms);
        const service = str(q.incident?.service);
        return {
          icon: "flag",
          tone: "neutral",
          tag: "STATE",
          parts: [
            "Incident created",
            ...(service ? [" — alert on ", code(service)] : []),
            ...(symptoms.length ? [`: ${symptoms.slice(0, 2).join("; ")}`] : []),
          ],
        };
      }
      return {
        icon: "arrowRight",
        tone: stateTone(to),
        tag: "STATE",
        parts: [code(`${str(q.from)} → ${to}`), q.reason ? ` · ${q.reason}` : ""],
      };
    }

    case "routing.decision": {
      const q = p as RoutingPayload;
      const line: EventLine = {
        icon: "route",
        tone: "accent",
        tag: "ROUTE",
        parts: [
          `Round ${str(q.round, "?")}: `,
          code(str(q.mode, "?")),
          ` [${arr<string>(q.agents).join(", ")}] · `,
          code(str(q.rule_id, "?")),
          q.reason ? ` · ${q.reason}` : "",
        ],
      };
      // The last round left a conflict no evidence rule settled (M3). Whichever rule fired: a
      // multi-service incident routes by RT2-cross-service before RT3-disagreement is checked.
      if (obj(q.signals).disagreement === true) line.chips = [{ label: "follow-up: open disagreement", tone: "warn" }];
      return line;
    }

    case "investigation.round": {
      const q = p as InvestigationRoundPayload;
      const focus = arr<string>(q.focus_services);
      // The verifier's open questions and leads (trusted text RECAP built) go to this round.
      const questions = arr<string>(q.open_questions).filter((x) => typeof x === "string" && x);
      const leads = arr<string>(q.leads).filter((x) => typeof x === "string" && x);
      if (questions.length) {
        return {
          icon: "search",
          tone: "warn",
          tag: "ROUND",
          parts: [
            `Follow-up round ${str(q.round, "?")}: ${plural(questions.length, "open question")}`,
            focus.length ? ` · focus: ${focus.join(", ")}` : "",
            ` — ${questions[0]}`,
            questions.length > 1 ? ` (+${questions.length - 1} more)` : "",
            leads.length ? ` · ${plural(leads.length, "lead")} from RECAP's verifier` : "",
          ],
        };
      }
      return {
        icon: "search",
        tone: leads.length ? "info" : "neutral",
        tag: "ROUND",
        parts: [
          `Investigation round ${str(q.round, "?")}`,
          q.escalation ? " (escalated)" : "",
          focus.length ? ` · focus: ${focus.join(", ")}` : "",
          leads.length ? ` — RECAP's lead: ${leads[0]}` : "",
          leads.length > 1 ? ` (+${leads.length - 1} more)` : "",
        ],
      };
    }

    case "retrieval.docs": {
      const q = p as RetrievalPayload;
      const results = arr<DocRef>(q.results);
      // Sections from a red-team knowledge overlay may be deliberately wrong: name them.
      const overlay = results.filter((r) => isOverlay(str(r.doc_path))).map((r) => str(r.chunk_id, "?"));
      const overlayChips = overlay.length ? [{ label: `red-team overlay: ${overlay.join(", ")}`, tone: "warn" as Tone }] : [];
      if (q.stage === "agent") {
        // search_runbooks (M3): the agent's provider wrote the query, so it is quoted data.
        const query = str(q.query);
        return {
          icon: "book",
          tone: "info",
          tag: "RAG",
          parts: [
            code(who || "agent"),
            ` searched the runbooks (${searchModeLabel(q.mode)}): ${plural(results.length, "section")}`,
            results.length ? " — " : "",
            results.length ? code(results.slice(0, 3).map((r) => str(r.chunk_id, "?")).join(", ")) : "",
            results.length > 3 ? ` +${results.length - 3} more` : "",
            query ? " · query " : "",
            query ? quote(query) : "",
          ],
          ...withChips([
            ...(obj(q.embedding).replayed === true ? [{ label: "replayed embedding", tone: "warn" as Tone }] : []),
            ...overlayChips,
          ]),
        };
      }
      const refs = results
        .slice(0, 3)
        .map((r) => `${str(r.chunk_id, "?")} (${fmtScore(num(r.score))})`)
        .join(", ");
      return {
        icon: "book",
        tone: "info",
        tag: "RAG",
        parts: [
          `[${str(q.stage, "?")}] `,
          q.reason ? `${q.reason}: ` : "",
          results.length ? code(refs) : "no results",
          results.length > 3 ? ` +${results.length - 3} more` : "",
        ],
        ...withChips(overlayChips),
      };
    }

    case "retrieval.skip": {
      const q = p as RetrievalPayload;
      return {
        icon: "book",
        tone: "neutral",
        tag: "RAG",
        parts: [`[${str(q.stage, "?")}] skipped — ${str(q.reason, "no reason given")}`],
      };
    }

    case "memory.search": {
      const q = p as MemorySearchPayload;
      const hits = arr<MemorySearchHit>(q.hits);
      if (!hits.length) {
        const asOf = q.created_before ? ` (memory as of ${fmtDateTime(q.created_before)})` : "";
        return { icon: "memory", tone: "neutral", tag: "MEMORY", parts: [`No similar past episodes${asOf}`] };
      }
      const best = hits[0];
      // A replay reads memory as it was when its recording began.
      const asOf = q.created_before ? ` · memory as of ${fmtDateTime(q.created_before)}` : "";
      return {
        icon: "memory",
        tone: "info",
        tag: "MEMORY",
        parts: [
          `${plural(hits.length, "similar episode")}; best `,
          code(str(best.episode_id, "?")),
          ` similarity ${fmtScore(num(best.similarity))} (${str(best.outcome, "?")}${best.verified ? ", verified" : ""})`,
          asOf,
        ],
      };
    }

    case "memory.assessment": {
      const q = p as MemoryAssessPayload;
      const hits = arr<MemoryAssessHit>(q.hits);
      const anySafe = hits.some((h) => h.safe_to_reuse);
      const parts: Part[] = [];
      hits.slice(0, 2).forEach((h, i) => {
        if (i) parts.push(" | ");
        parts.push(code(str(h.episode_id, "?")));
        // Since M3 a hit lists the conditions behind RECAP's verdict: in short, what argues
        // against reuse (or, for a safe hit, what held). Older runs have the sentence only.
        const why = arr<string>(h.safe_to_reuse ? h.supporting : h.contradicting).filter((x) => typeof x === "string" && x);
        const reason = why.length ? why.join("; ") : h.reason;
        parts.push(`: safe to reuse ${h.safe_to_reuse ? "YES" : "NO"}${reason ? ` — ${reason}` : ""}`);
      });
      if (hits.length > 2) parts.push(` (+${hits.length - 2} more)`);
      return { icon: "memory", tone: anySafe ? "ok" : "warn", tag: "MEMORY", parts };
    }

    case "memory.consolidated": {
      const q = p as MemoryWritePayload;
      const good = q.outcome === "success" && q.verified === true;
      return {
        icon: "save",
        tone: good ? "ok" : "neutral",
        tag: "MEMORY",
        parts: [
          "Episode ",
          code(str(q.episode_id, "?")),
          ` written · outcome ${str(q.outcome, "?")} · ${q.verified ? "oracle-verified" : "not verified"}`,
          q.partition ? ` · partition ${q.partition}` : "",
        ],
      };
    }

    case "memory.skip":
      return {
        icon: "memory",
        tone: "neutral",
        tag: "MEMORY",
        parts: [`Memory skipped — ${str((p as { reason?: unknown }).reason, "no reason given")}`],
      };

    case "context.pack": {
      const q = p as ContextPackPayload;
      return {
        icon: "package",
        tone: "neutral",
        tag: "CONTEXT",
        internal: true,
        parts: [
          who ? `${who} ` : "",
          `step ${str(q.step, "?")}: packed ${fmtInt(num(q.tokens_after))}/${fmtInt(num(q.budget_tokens))} est. tokens`,
          ` from ${fmtInt(num(q.tokens_before))} · ${plural(arr(q.dropped).length, "item")} dropped`,
        ],
      };
    }

    case "orchestration.summary": {
      const q = p as OrchestrationPayload;
      if (!q.mode && Array.isArray(q.partial_findings_kept)) {
        // A budget stop cut the round short: the findings finished before it are kept, unverified.
        const kept = arr<string>(q.partial_findings_kept).filter((x) => typeof x === "string" && x);
        return {
          icon: "stop",
          tone: "warn",
          tag: "AGENTS",
          parts: kept.length
            ? ["Budget stop: kept the finished findings of ", kept.join(", "), " (partial: never verified)"]
            : ["Budget stop: no finished finding to keep"],
        };
      }
      const parallel = q.mode === "parallel";
      const overlap = num(q.overlap_ms);
      // Measured: a parallel round whose agents ran back to back did not run concurrently.
      const how =
        overlap === null
          ? "Parallel round (overlap not recorded)"
          : overlap > 0
            ? `Agents ran concurrently for ${fmtMs(overlap)}`
            : "Parallel round, but the agents did not overlap";
      return {
        icon: "users",
        tone: "accent",
        tag: "AGENTS",
        parts: parallel
          ? [how, ` (wall ${fmtMs(num(q.wall_ms))}, sum of agent time ${fmtMs(num(q.sum_ms))})`]
          : [`${str(q.mode, "single")} agent finished in ${fmtMs(num(q.wall_ms))}`],
      };
    }

    case "agent.start": {
      const q = p as AgentStartPayload;
      const focus = arr<string>(q.focus_services);
      return {
        icon: "cpu",
        tone: "neutral",
        tag: "AGENT",
        parts: [
          code(str(q.role, who || "agent")),
          ` started · round ${str(q.round, "?")} · up to ${str(q.max_steps, "?")} steps`,
          focus.length ? ` · focus: ${focus.join(", ")}` : "",
        ],
      };
    }

    case "llm.call": {
      const q = p as LlmCallPayload;
      const seconds = num(q.duration_ms);
      const chips: { label: string; tone: Tone }[] = [];
      if (q.replayed) chips.push({ label: "replayed", tone: "warn" });
      if (q.ok === false) chips.push({ label: "unusable answer", tone: "bad" });
      if ((num(q.attempt) ?? 1) > 1) chips.push({ label: `attempt ${str(q.attempt)}`, tone: "neutral" });
      return {
        icon: "zap",
        tone: q.ok === false ? "warn" : "info",
        tag: "LLM",
        chips,
        parts: [
          code(who || "controller"),
          " → ",
          code(str(q.model, "model")),
          ` · ${str(q.mode, "?")} · ${str(q.response_kind, "?")} · in ${fmtInt(num(q.input_tokens))} / out ${fmtInt(num(q.output_tokens))} tokens`,
          seconds !== null ? ` · ${fmtMs(seconds)}` : "",
        ],
      };
    }

    case "agent.decision": {
      const q = p as AgentDecisionPayload;
      const calls = arr<{ tool_name?: string }>(q.tool_calls).map((c) => str(c.tool_name, "?"));
      // The provider wrote the summary (untrusted): quoted, with any note RECAP added kept apart.
      const summary = q.summary && !calls.length ? summaryParts(str(q.summary), "", ctx.notes?.get(who) ?? []) : [];
      return {
        icon: "cpu",
        tone: "neutral",
        tag: "AGENT",
        internal: true,
        parts: [
          code(who || "agent"),
          ` step ${str(q.step, "?")}: ${str(q.action, "?")}`,
          calls.length ? ` → ${calls.join(", ")}` : "",
          ...summary,
        ],
      };
    }

    case "agent.finish": {
      const q = p as AgentFinishPayload;
      const hyps = arr<string>(q.hypotheses);
      const outcome = str(q.outcome); // "" on runs recorded before M3
      const role = str(q.role, who || "agent");
      // The role's contract: claims about mechanisms this role may not conclude are dropped.
      const dropped = arr<string>(q.dropped_out_of_role).filter((x) => typeof x === "string" && x);
      return {
        icon: "cpu",
        tone: findingOutcomeTone(outcome),
        tag: "AGENT",
        parts: [
          code(role),
          ` finished${outcome ? ` ${findingOutcomeLabel(outcome)}` : ""}`,
          ` · ${plural(num(q.tool_calls) ?? 0, "tool call")} · ${plural(hyps.length, "hypothesis", "hypotheses")}`,
          ...(q.summary ? summaryParts(str(q.summary), outcome, ctx.notes?.get(role) ?? []) : []),
        ],
        ...(dropped.length ? { chips: [{ label: `dropped out of role: ${countList(dropped)}`, tone: "warn" as Tone }] } : {}),
      };
    }

    case "tool.discovery": {
      // DiscoveryReport: what the MCP server advertised vs the trusted registry.
      const q = p as DiscoveryPayload;
      const unknown = arr<string>(q.unknown_tools);
      const missing = arr<string>(q.missing_tools);
      const mismatches = arr<string>(q.annotation_mismatches);
      return {
        icon: "compass",
        tone: unknown.length || mismatches.length ? "warn" : "neutral",
        tag: "MCP",
        parts: [
          "MCP discovery",
          q.transport ? " over " : "",
          q.transport ? code(q.transport) : "",
          `: ${plural(arr(q.advertised).length, "tool")} advertised · ${arr(q.usable).length} usable (trusted registry)`,
          unknown.length ? ` · not trusted, never callable: ${unknown.join(", ")}` : "",
          missing.length ? ` · not offered by this server: ${missing.join(", ")}` : "",
          mismatches.length ? ` · server hints ignored: ${mismatches.join("; ")}` : "",
        ],
      };
    }

    case "tool.call": {
      const q = p as ToolCallPayload;
      // A free-text argument (search_runbooks' query) is the agent's own text: quoted, never
      // inside RECAP's call syntax, where it could pass for more calls or arguments.
      const { query, ...rest } = obj(q.args);
      const free = typeof query === "string" ? query : "";
      return {
        icon: "tool",
        tone: "neutral",
        tag: "TOOL",
        parts: [
          `${str(q.caller, who || "controller")} → `,
          code(fmtCall(str(q.tool, "?"), free ? rest : q.args)),
          free ? " · query " : "",
          free ? quote(free) : "",
        ],
      };
    }

    case "tool.result": {
      const q = p as ToolResultPayload;
      const flags = [...arr<string>(q.flags), ...(q.truncated && !arr(q.flags).includes("truncated") ? ["truncated"] : [])];
      const chips = flags.map((f) => ({ label: f, tone: flagTone(f) }));
      if (q.ok === false) {
        return {
          icon: "x",
          tone: "bad",
          tag: "RESULT",
          parts: [code(str(q.tool, "?")), ` failed: ${str(q.error, "unknown error")}`],
          chips,
        };
      }
      return {
        icon: "check",
        tone: "neutral",
        tag: "RESULT",
        parts: [
          code(str(q.tool, "?")),
          " ok",
          q.evidence_id ? " → " : "",
          q.evidence_id ? code(q.evidence_id) : "",
          ` · ${fmtMs(num(q.duration_ms) ?? e.duration_ms)}`,
        ],
        chips,
      };
    }

    case "policy.decision": {
      const q = p as PolicyPayload;
      const rules = arr<string>(q.rule_ids);
      const caller = str(q.caller, who);
      const verdict = str(q.verdict, "?");
      return {
        icon: "shield",
        tone: verdictTone(verdict),
        tag: "POLICY",
        // A worker's read being allowed (R8) is routine; denials and approvals are not.
        internal: verdict === "ALLOW" && caller !== "controller",
        parts: [
          `${verdictLabel(verdict)} `,
          code(str(q.tool, "?")),
          caller ? ` for ${caller}` : "",
          rules.length ? " · decided by " : "",
          rules.length ? code(rules[rules.length - 1]) : "",
          verdict !== "ALLOW" && arr(q.reasons).length ? ` — ${arr<string>(q.reasons).join("; ")}` : "",
        ],
      };
    }

    case "safety.injection_flag": {
      const q = p as InjectionPayload;
      const patterns = arr<string>(q.patterns);
      return {
        icon: "alert",
        tone: "warn",
        tag: "INJECTION",
        parts: [
          "Injection-like text flagged in ",
          code(str(q.evidence_id, "?")),
          q.tool ? ` (from ${q.tool})` : "",
          patterns.length ? ` · patterns: ${patterns.join(", ")}` : "",
          " · treated as untrusted data",
        ],
      };
    }

    case "mitigation.proposed": {
      const q = p as ProposalPayload;
      const proposal = q.proposal;
      if (!proposal) {
        return {
          icon: "bulb",
          tone: "bad",
          tag: "PROPOSAL",
          parts: [`No remediation could be drafted${arr(q.notes).length ? ` — ${arr<string>(q.notes).join("; ")}` : ""}`],
        };
      }
      const risk = str(q.trusted_risk, "unknown");
      return {
        icon: "bulb",
        tone: "accent",
        tag: "PROPOSAL",
        parts: [
          "Proposed ",
          code(`${str(proposal.action, "?")} on ${str(proposal.target, "?")}`),
          Object.keys(obj(proposal.params)).length ? ` (${fmtArgs(proposal.params)})` : "",
          ` · source ${str(proposal.source, "?")}`,
        ],
        chips: [{ label: `trusted risk: ${risk}`, tone: riskTone(risk) }],
      };
    }

    case "approval.requested": {
      const q = p as ApprovalRequestedPayload;
      return {
        icon: "key",
        tone: "warn",
        tag: "APPROVAL",
        parts: [
          "Approval requested via ",
          code(str(q.channel, "?")),
          " · digest ",
          code(`${shortDigest(q.digest, 12)}…`),
          q.expires_at ? ` · expires ${fmtTime(q.expires_at)}` : "",
        ],
      };
    }

    case "approval.decided": {
      const q = p as ApprovalDecidedPayload;
      const decision = str(q.decision, "?");
      return {
        icon: "key",
        tone: decision === "approved" ? "ok" : "bad",
        tag: "APPROVAL",
        parts: [
          `Approval ${decision}`,
          q.actor ? ` by ${q.actor}` : "",
          q.approval_id ? " · " : "",
          q.approval_id ? code(q.approval_id) : "",
        ],
      };
    }

    case "mitigation.started": {
      // Written before the write is sent, so a write is never invisible, even if the run stops.
      const q = p as MitigationExecutedPayload;
      return {
        icon: "zap",
        tone: "accent",
        tag: "MITIGATE",
        parts: [
          "Sending ",
          code(`${str(q.tool, "?")} on ${str(q.target, "?")}`),
          Object.keys(obj(q.params)).length ? ` (${fmtArgs(q.params)})` : "",
          q.approval_id ? " · approval " : "",
          q.approval_id ? code(q.approval_id) : "",
        ],
      };
    }

    case "mitigation.executed": {
      const q = p as MitigationExecutedPayload;
      const effects = arr<string>(q.side_effects);
      return {
        icon: "zap",
        tone: q.ok === false ? "bad" : "ok",
        tag: "MITIGATE",
        parts: [
          q.ok === false ? "Mitigation failed: " : "Executed ",
          code(`${str(q.tool, "?")} on ${str(q.target, "?")}`),
          Object.keys(obj(q.params)).length ? ` (${fmtArgs(q.params)})` : "",
          effects.length ? ` · ${effects.join("; ")}` : "",
          q.error ? ` · ${q.error}` : "",
          q.run_cancelled ? " · the run was cancelled while this write was in flight" : "",
        ],
      };
    }

    case "verification.merge": {
      // The merger pooled this epoch's claims (M3). Disagreement claims are RECAP's own text,
      // built from checked values; rules settle them, never how many agents agree.
      const q = p as VerificationMergePayload;
      const made = Object.entries(obj(q.claims_by_agent)).map(([role, n]): [string, number | null] => [role, num(n)]);
      // A count the payload lacks stays unknown ("?"), and so does the total.
      const total = made.every(([, n]) => n !== null) ? made.reduce((sum, [, n]) => sum + (n ?? 0), 0) : null;
      const merged = num(q.merged_claims);
      const shared = arr(q.shared_claims).length;
      const disagreements = arr<NonNullable<VerificationMergePayload["disagreements"]>[number]>(q.disagreements);
      const counts = disagreementCounts(disagreements);
      const openClaims = disagreements
        .filter((d) => d.resolved === false)
        .map((d) => arr<string>(d.claims).join(" vs "))
        .filter(Boolean);
      const leads = arr<string>(q.leads).filter((x) => typeof x === "string" && x);
      return {
        icon: "users",
        tone: counts?.open ? "warn" : "neutral",
        tag: "MERGE",
        parts: [
          made.length && total !== null ? `Merged ${plural(total, "claim")} from ` : "Merged the claims of ",
          ...agentCounts(made),
          merged !== null ? ` into ${merged}` : "",
          shared ? ` · ${shared} shared by several agents` : "",
          counts?.total ? ` · ${plural(counts.total, "disagreement")}, ${counts.open} open` : " · no disagreements",
          openClaims.length ? ` — ${openClaims.join("; ")}` : "",
          leads.length ? ` · ${plural(leads.length, "lead")} for the next round` : "",
        ],
        ...(disagreements.length
          ? {
              chips: disagreements.map((d) => {
                const rules = splitRuleIds(str(d.rule_id));
                return {
                  label: `${humanize(str(d.kind, "disagreement"))}: ${
                    d.resolved === true ? (rules.length ? `settled by ${rules.join(", ")}` : "settled") : d.resolved === false ? "open" : "not recorded"
                  }`,
                  tone: (d.resolved === true ? "ok" : "warn") as Tone,
                };
              }),
            }
          : {}),
      };
    }

    case "verification.evidence": {
      const q = p as EvidenceCheckPayload;
      if (q.proposal_check) {
        const ok = q.proposal_check.ok === true;
        const problems = arr<string>(q.proposal_check.problems);
        return {
          icon: "search",
          tone: ok ? "ok" : "bad",
          tag: "VERIFY",
          parts: [`Proposal check ${ok ? "passed" : "failed"}`, problems.length ? `: ${problems.join("; ")}` : ""],
        };
      }
      const status = str(q.status, "?");
      const best = q.best;
      const leads = arr<string>(q.leads).filter((x) => typeof x === "string" && x);
      const leadChips = leads.length ? [{ label: `${plural(leads.length, "lead")} for the next round`, tone: "info" as Tone }] : [];
      if (best && best.mechanism) {
        // RECAP's checked fields first; the claim sentence is the agent's own text, quoted. A
        // version enters RECAP's text only when it is shaped like one (the claim names it).
        const version = str(obj(best.details).version);
        const claim = str(best.claim);
        return {
          icon: "search",
          tone: hypothesisTone(status),
          tag: "VERIFY",
          parts: [
            `Evidence ${status}: `,
            code(`${str(best.mechanism)}@${str(best.target_service, "?")}${VERSION_SHAPE.test(version) ? ` ${version}` : ""}`),
            claim ? " · claim " : "",
            claim ? quote(claim) : "",
          ],
          ...withChips(leadChips),
        };
      }
      // No diagnosis: the verifier's own summary (RECAP's text, built from checked values).
      return {
        icon: "search",
        tone: hypothesisTone(status),
        tag: "VERIFY",
        parts: [`Evidence ${status}`, q.summary ? `: ${str(q.summary)}` : ""],
        ...withChips(leadChips),
      };
    }

    case "verification.recovery": {
      const q = p as RecoveryPayload;
      const still = arr<string>(q.unhealthy_after);
      return {
        icon: "heart",
        tone: q.verified ? "ok" : "bad",
        tag: "ORACLE",
        parts: [
          q.verified ? "Recovery verified" : "NOT recovered",
          " by ",
          code(str(q.oracle, "oracle")),
          ` · healthy before ${yesNo(q.healthy_before)} → after ${yesNo(q.healthy_after)}`,
          still.length ? ` · still unhealthy: ${still.join(", ")}` : "",
          q.simulated ? " · simulated" : "",
        ],
      };
    }

    case "budget.exhausted": {
      const q = p as BudgetPayload;
      if (q.limit === "max_model_tokens") {
        // Reserved before each model request (ADR 0024): the request that would cross the
        // limit was never sent.
        return {
          icon: "stop",
          tone: "warn",
          tag: "BUDGET",
          parts: [
            "Model-token budget exhausted: ",
            code("max_model_tokens"),
            q.detail ? ` (${q.detail})` : "",
            " · that model request was not sent",
          ],
        };
      }
      return {
        icon: "stop",
        tone: "warn",
        tag: "BUDGET",
        parts: ["Budget exhausted: ", code(str(q.limit, "?")), q.detail ? ` (${q.detail})` : ""],
      };
    }

    case "run.error":
      return {
        icon: "x",
        tone: "bad",
        tag: "ERROR",
        parts: [`Error: ${str((p as { error?: unknown }).error, "unknown")}`],
      };

    case "run.summary": {
      const q = p as RunSummaryPayload;
      const state = str(q.state, "?");
      return {
        icon: "flag",
        tone: stateTone(state),
        tag: "SUMMARY",
        parts: [
          "Run finished: ",
          code(state),
          q.outcome_note ? ` — ${q.outcome_note}` : "",
          q.cost ? ` · ${q.cost}` : "",
        ],
      };
    }

    default: {
      const preview = str(p);
      return {
        icon: "dot",
        tone: "neutral",
        tag: e.category.split(".")[0].toUpperCase().slice(0, 9),
        parts: [code(e.category), preview && preview !== "{}" ? ` ${preview.slice(0, 140)}` : ""],
      };
    }
  }
}

/** `chips` only when there are some (a line without chips has none at all). */
function withChips(chips: { label: string; tone: Tone }[]): { chips?: { label: string; tone: Tone }[] } {
  return chips.length ? { chips } : {};
}

/** A disagreement's rule_id may list several rules, comma-separated. */
function splitRuleIds(ruleId: string): string[] {
  return [
    ...new Set(
      ruleId
        .split(",")
        .map((id) => id.trim())
        .filter(Boolean),
    ),
  ];
}

// --- an agent's summary: the provider's words and the notes RECAP added ---------------------

export interface SummaryParts {
  /** What the agent's provider wrote: untrusted text ("" when RECAP wrote all of it). */
  said: string;
  /** Notes RECAP added, in reading order: its own text (a correction may quote a value the model wrote). */
  notes: string[];
  /** False when `said` may still hold a note RECAP added that could not be told apart (shown as such). */
  sure: boolean;
}

const UNUSABLE = "unusable: ";
const STEP_LIMIT = "(step limit reached before the agent finished)";
const CORRECTIONS = "[RECAP corrections: ";
const MODEL_PREFIX = /^The model (needs more evidence|stopped):\s*/;
// What RECAP adds to a summary (protocol.py, http_llm.py, loop.py): text still matching one of
// these after the split may be RECAP's, so it is not presented as purely the provider's.
const RECAP_ADDITIONS = [/^no usable answer from /, MODEL_PREFIX, /\(step limit reached before the agent finished\)$/, /\[RECAP corrections: [\s\S]*\]$/];

/** True when `inner` is only notes RECAP recorded (joined by "; "), in any order. */
function onlyNotes(inner: string, notes: readonly string[]): boolean {
  if (!notes.length || !inner.trim()) return false;
  let rest = inner;
  for (const note of [...new Set(notes)].sort((a, b) => b.length - a.length)) rest = rest.split(note).join("");
  return rest.replace(/;/g, "").trim() === "";
}

/**
 * An agent's summary split into the provider's own words and the notes RECAP added to it:
 *   no_usable_answer     all of it is RECAP's ("no usable answer from <provider>: <problem>");
 *                        a model cannot choose that outcome. Recognized on a run before M3 when
 *                        a model request recorded the same problem ("unusable: <problem>").
 *   need_more_evidence,  RECAP's leading "The model needs more evidence: " or
 *   stop                 "The model stopped: "
 *   step_limit           RECAP's trailing "(step limit reached before the agent finished)"
 *   any                  a trailing "[RECAP corrections: ...]", only when it holds exactly the
 *                        notes recorded on the agent's model requests (llm.call `notes`): a
 *                        model could write such a block itself.
 * `outcome`: AgentFinding.outcome ("" when not recorded); `notes`: the agent's llm.call notes.
 */
export function splitSummary(summary: string, outcome: string | null | undefined, notes: readonly string[] = []): SummaryParts {
  let said = summary.trim();
  if (!said) return { said: "", notes: [], sure: true };
  const problems = notes.filter((n) => n.startsWith(UNUSABLE)).map((n) => n.slice(UNUSABLE.length));
  if (outcome === "no_usable_answer" || (said.startsWith("no usable answer from ") && problems.some((x) => x && said.endsWith(`: ${x}`)))) {
    return { said: "", notes: [said], sure: true };
  }
  const tail: string[] = [];
  if (outcome === "step_limit" && said.endsWith(STEP_LIMIT)) {
    said = said.slice(0, -STEP_LIMIT.length).trimEnd();
    tail.unshift(STEP_LIMIT.slice(1, -1));
  }
  const at = said.lastIndexOf(CORRECTIONS);
  if (at >= 0 && said.endsWith("]") && onlyNotes(said.slice(at + CORRECTIONS.length, -1), notes)) {
    tail.unshift(said.slice(at + 1, -1));
    said = said.slice(0, at).trimEnd();
  }
  const head: string[] = [];
  const prefix = outcome === "need_more_evidence" || outcome === "stop" ? MODEL_PREFIX.exec(said) : null;
  if (prefix) {
    head.push(`The model ${prefix[1]}`);
    said = said.slice(prefix[0].length);
  }
  return { said, notes: [...head, ...tail], sure: !RECAP_ADDITIONS.some((re) => re.test(said)) };
}

/** RECAP's notes on each agent's model requests (llm.call `notes`), for splitSummary. */
export function modelNotesByAgent(events: readonly TraceEvent[]): Map<string, string[]> {
  const notes = new Map<string, string[]>();
  for (const e of events) {
    if (e.category !== "llm.call" || !e.agent) continue;
    const mine = notes.get(e.agent) ?? [];
    for (const note of arr<string>((e.payload as LlmCallPayload).notes)) {
      if (typeof note === "string" && note && !mine.includes(note)) mine.push(note);
    }
    notes.set(e.agent, mine);
  }
  return notes;
}

/** Tone of an approval decision-log entry: a "no", a revocation or a refusal stands out. */
export function approvalEventTone(kind: string): Tone {
  switch (kind) {
    case "approved":
      return "ok";
    case "consumed":
      return "accent";
    case "rejected":
    case "revoked":
      return "bad";
    case "expired":
    case "refused":
      return "warn";
    default:
      return "neutral";
  }
}

// --- M3 run facts: search mode, memory partitions, disagreements, agent outcomes ------------
// A value a run did not record reads "not recorded" (runs from before M3 lack these fields).

const SEARCH_MODES: Record<string, [short: string, long: string]> = {
  bm25: ["BM25", "keywords (BM25)"],
  hybrid: ["hybrid", "keywords + meaning (hybrid)"],
};

/** How runbooks were searched (RetrievalInfo.mode, RunConfig.retrieval): "BM25" / "hybrid", or the long form. */
export function searchModeLabel(mode: unknown, long = false): string {
  const known = typeof mode === "string" ? SEARCH_MODES[mode] : undefined;
  if (known) return long ? known[1] : known[0];
  return typeof mode === "string" && mode ? mode : "not recorded";
}

/**
 * Whether this server can run a hybrid search now (GET /system, M3): `hybrid_ready`, true when
 * stored vectors cover every runbook chunk. Ollama must still be running for the query
 * embedding. A server from before that field named the capability in `retriever` instead
 * (hybrid-bm25-embeddings only with every vector); "unknown": a server before M3.
 */
export function hybridReadiness(
  system: Partial<Pick<SystemSummary, "retriever" | "retrieval_mode" | "hybrid_ready">> | null | undefined,
): "ready" | "unavailable" | "unknown" {
  if (typeof system?.hybrid_ready === "boolean") return system.hybrid_ready ? "ready" : "unavailable";
  if (!system?.retrieval_mode) return "unknown";
  return system.retriever === "hybrid-bm25-embeddings" ? "ready" : "unavailable";
}

/**
 * The memory partitions a run read and wrote. The controller fills them in from the run's
 * origin (scripted: TRAIN + DEMO, writes DEMO; a real model: TRAIN + LIVE, writes LIVE; a
 * replay writes nothing), so a run that left them null predates that record. `wrote`: whether
 * a finished run wrote its episode (false: it did not, e.g. a held-out test scenario; leave
 * it out while the run is going).
 */
export function memoryAccessLabel(
  config:
    | Partial<Pick<RunConfig, "use_memory" | "memory_write" | "memory_read_partitions" | "memory_write_partition">>
    | null
    | undefined,
  wrote?: boolean,
): string {
  if (!config) return "not recorded";
  const partitions = config.memory_read_partitions;
  const read =
    config.use_memory === false
      ? "memory off: nothing read"
      : Array.isArray(partitions)
        ? partitions.length
          ? `reads ${partitions.join(" + ")}`
          : "reads no partition"
        : "reads: not recorded";
  const write =
    config.memory_write === false
      ? "writes nothing"
      : config.memory_write_partition
        ? wrote === false
          ? `no episode written (write partition ${config.memory_write_partition})`
          : `writes ${config.memory_write_partition}`
        : "writes: not recorded";
  return `${read} · ${write}`;
}

export interface DisagreementCounts {
  total: number;
  /** Not settled by any evidence rule: the next round gets its question, then a person decides. */
  open: number;
  settled: number;
  /** Settled ones a named evidence rule settled (a missing view is settled by none). */
  byRule: number;
}

/** Counts of a VerificationReport's (or a verification.merge payload's) disagreements; null: not recorded. */
export function disagreementCounts(list: unknown): DisagreementCounts | null {
  if (!Array.isArray(list)) return null;
  const items = list.map(obj);
  return {
    total: list.length,
    open: items.filter((d) => d.resolved === false).length,
    settled: items.filter((d) => d.resolved === true).length,
    byRule: items.filter((d) => d.resolved === true && splitRuleIds(str(d.rule_id)).length > 0).length,
  };
}

/**
 * "none", "1 disagreement, settled by a rule", "2 disagreements, all settled (1 by a rule)",
 * "2 disagreements, 1 open"; "not recorded" before M3.
 */
export function disagreementLabel(counts: DisagreementCounts | null): string {
  if (!counts) return "not recorded";
  if (!counts.total) return "none";
  const all = plural(counts.total, "disagreement");
  if (counts.settled !== counts.total) return `${all}, ${counts.open} open`;
  if (counts.byRule === counts.total) return `${all}, ${counts.total === 1 ? "settled by a rule" : "all settled by rules"}`;
  const some = counts.byRule ? ` (${counts.byRule} by a rule)` : "";
  return `${all}, ${counts.total === 1 ? "settled" : "all settled"}${some}`;
}

// How an agent ended its turn (AgentFinding.outcome), phrased to follow "finished". The tones
// are the Agent Timeline's (view/findings.ts outcomeView; a test keeps them equal): no usable
// answer is a missing view, like an error.
const FINDING_OUTCOMES: Record<string, [label: string, tone: Tone]> = {
  hypothesis: ["with hypotheses for the verifier", "accent"],
  need_more_evidence: ["asking for more evidence", "info"],
  stop: ["by stopping, without a diagnosis", "warn"],
  step_limit: ["at its step limit", "warn"],
  no_usable_answer: ["without a usable model answer", "bad"],
  error: ["with an error", "bad"],
};

/** AgentFinding.outcome as words ("" on runs recorded before M3 -> "not recorded"). */
export function findingOutcomeLabel(outcome: string | null | undefined): string {
  if (!outcome) return "not recorded";
  return FINDING_OUTCOMES[outcome]?.[0] ?? humanize(outcome);
}

export function findingOutcomeTone(outcome: string | null | undefined): Tone {
  return (outcome ? FINDING_OUTCOMES[outcome]?.[1] : undefined) ?? "neutral";
}

// --- M3 budget use: real counts against the run's limits --------------------------------------

/** "8,960 / 200,000": a count against its limit; "—" when either was not recorded. */
export function fmtOf(used: unknown, limit: unknown, digits = 0): string {
  const u = num(used);
  const l = num(limit);
  if (u === null || l === null) return "—";
  return digits ? `${fmtNum(u, digits)} / ${fmtNum(l, digits)}` : `${fmtInt(u)} / ${fmtInt(l)}`;
}

export interface BudgetUse {
  key: string;
  label: string;
  used: number;
  limit: number;
  /** "15 / 30 steps" */
  text: string;
}

const BUDGET_COUNTERS: [used: string, limit: string, label: string, digits: number][] = [
  ["steps", "max_steps", "steps", 0],
  ["tool_calls", "max_tool_calls", "tool calls", 0],
  ["estimated_tokens", "max_estimated_tokens", "estimated tokens", 0],
  ["model_tokens", "max_model_tokens", "model tokens", 0],
  // BudgetTracker pauses this clock while a run waits for approval (RunBudget docs).
  ["elapsed_seconds", "max_elapsed_seconds", "s run clock (approval waits excluded)", 2],
];

/** The counters a run.summary budget snapshot recorded (recap/harness/budget.py), each with its limit. */
export function budgetUses(budget: unknown): BudgetUse[] {
  const b = obj(budget);
  const uses: BudgetUse[] = [];
  for (const [key, max, label, digits] of BUDGET_COUNTERS) {
    const used = num(b[key]);
    const limit = num(b[max]);
    if (used === null || limit === null) continue;
    uses.push({ key, label, used, limit, text: `${fmtOf(used, limit, digits)} ${label}` });
  }
  return uses;
}

export interface ModelTokenUse {
  /** "8,960 / 200,000", or "not recorded" */
  value: string;
  /** What kind of count it is (reported, recorded, partly estimated, or no model at all). */
  note: string;
  tone: Tone;
}

/**
 * Model tokens used against the run's limit, from run.summary's budget (model_tokens and
 * max_model_tokens, M3). The budget counts what model servers reported (a replay: the
 * recorded counts) and, for a request that reported none, the prompt estimate it charged.
 */
export function modelTokenUse(
  budget: unknown,
  usage: Partial<Pick<Usage, "model_requests" | "unreported_calls">> | null | undefined,
  provider: Partial<ProviderInfo> | null | undefined,
): ModelTokenUse {
  const b = obj(budget);
  const used = num(b.model_tokens);
  const limit = num(b.max_model_tokens);
  if (used === null || limit === null) return { value: "not recorded", note: "", tone: "neutral" };
  const value = fmtOf(used, limit);
  const tone: Tone = limit > 0 && used >= 0.9 * limit ? "warn" : "neutral";
  const origin = originBadge(provider).label;
  if (origin === "REPLAY") return { value, note: "recorded counts: a replay calls no model", tone };
  const requests = num(usage?.model_requests);
  if (requests === null) return { value, note: "", tone };
  if (requests === 0) {
    return { value, note: origin === "SCRIPTED" ? "no model requests: scripted policy" : "no model requests", tone };
  }
  const unreported = num(usage?.unreported_calls) ?? 0;
  return {
    value,
    note: unreported
      ? `as reported by the model server, plus a prompt estimate for ${plural(unreported, "request")} that reported no usage`
      : "as reported by the model server",
    tone,
  };
}

export interface ModelTokensSoFar {
  /** input + output tokens the llm.call events reported */
  tokens: number;
  requests: number;
  /** answers that reported no counts (the budget charged a prompt estimate for each) */
  unreported: number;
  replayed: number;
}

/** Model tokens a running run's llm.call events reported so far (its budget snapshot comes with run.summary). */
export function modelTokensSoFar(events: TraceEvent[]): ModelTokensSoFar {
  const so: ModelTokensSoFar = { tokens: 0, requests: 0, unreported: 0, replayed: 0 };
  for (const e of events) {
    if (e.category !== "llm.call") continue;
    const q = (e.payload ?? {}) as LlmCallPayload;
    so.requests += 1;
    if (q.replayed === true) so.replayed += 1;
    const input = num(q.input_tokens);
    const output = num(q.output_tokens);
    if (input !== null || output !== null) so.tokens += (input ?? 0) + (output ?? 0);
    else if (q.status === 200 || q.status === 400) so.unreported += 1; // a 429 or 5xx generated nothing
  }
  return so;
}

/**
 * Model tokens of a running run against its limit (the incident's budget.max_model_tokens,
 * M3), counted from its llm.call events: what the server reported, never an estimate.
 */
export function liveModelTokenUse(
  events: TraceEvent[],
  provider: Partial<ProviderInfo> | null | undefined,
  limit: number | null | undefined,
): ModelTokenUse {
  const so = modelTokensSoFar(events);
  const max = num(limit);
  const value = max !== null ? fmtOf(so.tokens, max) : fmtInt(so.tokens);
  const tone: Tone = max !== null && max > 0 && so.tokens >= 0.9 * max ? "warn" : "neutral";
  let note: string;
  if (!so.requests) {
    note = originBadge(provider).label === "SCRIPTED" ? "no model requests: scripted policy" : "no model requests yet";
  } else if (so.replayed === so.requests) {
    note = "recorded counts: a replay calls no model";
  } else {
    note = so.unreported
      ? `reported so far; ${plural(so.unreported, "answer")} without counts`
      : "reported by the model server so far";
  }
  return { value, note: max === null ? `${note}; limit not recorded` : note, tone };
}
