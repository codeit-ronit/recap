/**
 * Formatting helpers: tones (colors) for lifecycle states and verdicts, number/time
 * formatting, honest labels for the environment, provider and cost, and `describeEvent`,
 * which turns one TraceEvent into a readable one-line description for the live feed.
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
  RunSummaryPayload,
  StatePayload,
  ToolCallPayload,
  ToolResultPayload,
  TraceEvent,
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
      return { label: "LOCAL LLM", tone: "accent", title: "Real inference by a model on this machine (Ollama). No API, $0." };
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

/** A text fragment; `code` fragments render in monospace (ids, tool names, digests). */
export type Part = string | { code: string };

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

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function describeEvent(e: TraceEvent): EventLine {
  const p = e.payload ?? {};
  const who = e.agent ?? "";
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
      return {
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
    }

    case "investigation.round": {
      const q = p as InvestigationRoundPayload;
      const focus = arr<string>(q.focus_services);
      return {
        icon: "search",
        tone: "neutral",
        tag: "ROUND",
        parts: [
          `Investigation round ${str(q.round, "?")}`,
          q.escalation ? " (escalated)" : "",
          focus.length ? ` · focus: ${focus.join(", ")}` : "",
        ],
      };
    }

    case "retrieval.docs": {
      const q = p as RetrievalPayload;
      const results = arr<DocRef>(q.results);
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
        return { icon: "memory", tone: "neutral", tag: "MEMORY", parts: ["No similar past episodes"] };
      }
      const best = hits[0];
      return {
        icon: "memory",
        tone: "info",
        tag: "MEMORY",
        parts: [
          `${plural(hits.length, "similar episode")}; best `,
          code(str(best.episode_id, "?")),
          ` similarity ${fmtScore(num(best.similarity))} (${str(best.outcome, "?")}${best.verified ? ", verified" : ""})`,
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
        parts.push(`: safe to reuse ${h.safe_to_reuse ? "YES" : "NO"}${h.reason ? ` — ${h.reason}` : ""}`);
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
      const parallel = q.mode === "parallel";
      return {
        icon: "users",
        tone: "accent",
        tag: "AGENTS",
        parts: parallel
          ? [
              `Agents ran concurrently for ${fmtMs(num(q.overlap_ms))}`,
              ` (wall ${fmtMs(num(q.wall_ms))}, sum of agent time ${fmtMs(num(q.sum_ms))})`,
            ]
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
      return {
        icon: "cpu",
        tone: "neutral",
        tag: "AGENT",
        internal: true,
        parts: [
          code(who || "agent"),
          ` step ${str(q.step, "?")}: ${str(q.action, "?")}`,
          calls.length ? ` → ${calls.join(", ")}` : "",
          q.summary && !calls.length ? ` — ${q.summary}` : "",
        ],
      };
    }

    case "agent.finish": {
      const q = p as AgentFinishPayload;
      const hyps = arr<string>(q.hypotheses);
      return {
        icon: "cpu",
        tone: "neutral",
        tag: "AGENT",
        parts: [
          code(str(q.role, who || "agent")),
          ` finished · ${plural(num(q.tool_calls) ?? 0, "tool call")} · ${plural(hyps.length, "hypothesis", "hypotheses")}`,
          q.summary ? ` — ${q.summary}` : "",
        ],
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
      return {
        icon: "tool",
        tone: "neutral",
        tag: "TOOL",
        parts: [`${str(q.caller, who || "controller")} → `, code(fmtCall(str(q.tool, "?"), q.args))],
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
      const claim = str(q.best?.claim) || str(q.summary);
      return {
        icon: "search",
        tone: hypothesisTone(status),
        tag: "VERIFY",
        parts: [`Evidence ${status}`, claim ? `: ${claim}` : ""],
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
