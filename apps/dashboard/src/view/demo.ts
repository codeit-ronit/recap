/**
 * View helpers for the Guided Demo (pages/GuidedDemo.tsx): a replay of a committed real-model
 * recording, told as ten steps a first-time visitor can follow. Every step, fact, time and actor
 * is read from the run's own trace events as they arrive (GET /runs/{id}/stream):
 *
 *   - a step is DONE once its events are in the trace, and CURRENT while the latest event
 *     belongs to it (an investigation's second round makes "strategy" current again);
 *   - the approval step is WAITING while the run waits for a person;
 *   - a step the run moved past, or ended without, is SKIPPED, with the reason.
 *
 * Nothing is invented: a step without events has no facts, a value the trace did not record is
 * left out, and text a model wrote is marked `model` so the page renders it as untrusted. Rule
 * codes are returned as codes; the page explains them with src/view/explain.ts.
 *
 * Pure functions with type-only imports, so tests/guided-demo.test.ts loads this file directly.
 */

import type { IconName } from "../components/Icon";
import type { FixtureView } from "../dto";
import type { Tone } from "../format";
import type { TraceEvent } from "../types";

/** The label every replayed demo carries (M6 specification, section C). */
export const DEMO_PROVENANCE = "Recorded real-model execution — replayed locally";

// --- small readers (local copies: this module imports no runtime values) -------------------------

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function isoMs(iso: string | null | undefined): number {
  if (!iso) return Number.NaN;
  return Date.parse(iso.replace(/(\.\d{3})\d+/, "$1"));
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
}

/** "telemetry_investigator" -> "Telemetry investigator"; no role -> RECAP's controller. */
export function actorName(role: string | null | undefined): string {
  const words = text(role).replace(/_/g, " ");
  if (!words || words === "controller") return "RECAP's controller";
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** What each agent role does, in plain words (recap/core/models.py AgentRole). */
export const ROLE_WORDS: Readonly<Record<string, string>> = {
  investigator: "A generalist AI agent: reads health, logs, metrics, deployments and cluster events.",
  telemetry_investigator: "An AI specialist for health, logs and metrics.",
  infrastructure_investigator: "An AI specialist for deployments, cluster events and topology.",
  evidence_verifier: "Checks every diagnosis against the evidence with its own rules; no model, no tools.",
  controller: "RECAP's trusted lifecycle owner: the only part that may ever change anything.",
};

// ==================================================================================================
// The ten steps
// ==================================================================================================

export type DemoStepId = "incident" | "context" | "strategy" | "investigate" | "verify" | "propose" | "approve" | "act" | "recover" | "outcome";

export interface DemoStepDef {
  id: DemoStepId;
  title: string;
  /** What happens in this step, in plain words. */
  about: string;
  /** Interview mode: what a presenter can point out here. */
  talk: string;
}

export const DEMO_STEPS: readonly DemoStepDef[] = [
  {
    id: "incident",
    title: "An incident occurs",
    about: "An alert arrives and becomes an incident with its own budget of steps, tool calls and model tokens.",
    talk: "The alert names symptoms, not causes. RECAP gives the incident a hard budget before any model runs.",
  },
  {
    id: "context",
    title: "Runbooks and memory are searched",
    about: "RECAP looks up runbook sections relevant to the alert (retrieval) and similar past incidents in its experience memory.",
    talk: "Point out what retrieval found, and whether memory knew a similar incident: this recording is an unfamiliar incident, added in M3 (unfamiliar rather than held out: RECAP's log parsing was widened with it in view).",
  },
  {
    id: "strategy",
    title: "An investigation strategy is chosen",
    about: "A rules-based router reads the incident's signals and picks one investigator or specialists in parallel.",
    talk: "The router is plain rules, not a model, and the rule that decided is recorded with the choice.",
  },
  {
    id: "investigate",
    title: "AI agents inspect the evidence",
    about: "Specialist agents call read-only tools (logs, metrics, cluster events, deployments) and file hypotheses. Every call passes the safety policy first.",
    talk: "Each agent's decisions are the recorded model's own answers. Every tool result becomes evidence with an ID that later checks must cite.",
  },
  {
    id: "verify",
    title: "The verifier checks the diagnosis",
    about: "Separate code with its own rules accepts a diagnosis only if this run's evidence supports it; otherwise the agents get another round with its leads.",
    talk: "The verifier shares no heuristics with the agents. Log text never beats platform signals, and a guess without evidence is sent back.",
  },
  {
    id: "propose",
    title: "A fix is proposed",
    about: "A fix is drafted from the verified diagnosis. The evidence gate and the safety policy check it before anyone is asked.",
    talk: "Two checks before a person sees it: the evidence gate (VP1 to VP4) and the safety policy, which asks for an approval bound to this exact proposal.",
  },
  {
    id: "approve",
    title: "You approve or reject the fix",
    about: "Nothing changes until a person approves this exact action. The approval is single-use, expires, and is bound to the proposal's digest.",
    talk: "Nothing has changed yet. Approve to continue, or reject to show that nothing runs and the run ends blocked.",
  },
  {
    id: "act",
    title: "The approved fix runs",
    about: "The safety gateway checks the approval again, then runs the action exactly once on the run's target.",
    talk: "The gateway re-checks that the approval matches this exact proposal, unused and unexpired, then runs it exactly once.",
  },
  {
    id: "recover",
    title: "An independent check verifies recovery",
    about: "An oracle with its own reads, never the agents' tools or their word, checks whether the target recovered.",
    talk: "Success is decided by the oracle, not by the model saying it worked.",
  },
  {
    id: "outcome",
    title: "Outcome and full trace",
    about: "The run's final state, with every model answer, tool call and policy decision kept in its trace.",
    talk: "Open Trace Explorer to show any step in full: the model's answers, the tool calls, the policy's rule IDs and the evidence.",
  },
];

const STEP_INDEX: Readonly<Record<DemoStepId, number>> = Object.fromEntries(DEMO_STEPS.map((s, i) => [s.id, i])) as Record<DemoStepId, number>;

const TERMINAL = new Set(["RESOLVED", "FAILED", "BLOCKED", "BUDGET_EXHAUSTED", "QUOTA_EXHAUSTED"]);

/**
 * Which step an event belongs to: by the lifecycle stage it was recorded in (TraceEvent.stage),
 * and inside an investigation by its category (routing, agents and tools, verification).
 */
export function stepOf(event: Pick<TraceEvent, "stage" | "category">): DemoStepId | null {
  const stage = text(event.stage);
  switch (stage) {
    case "CREATED":
      return "incident";
    case "RETRIEVING":
      return "context";
    case "INVESTIGATING":
      if (event.category === "routing.decision" || event.category === "investigation.round" || event.category === "state.transition") return "strategy";
      if (event.category === "verification.merge" || event.category === "verification.evidence") return "verify";
      return "investigate";
    case "PROPOSING":
      return "propose";
    case "AWAITING_APPROVAL":
      return "approve";
    case "MITIGATING":
      return "act";
    case "VERIFYING":
      return "recover";
    default:
      return TERMINAL.has(stage) ? "outcome" : null;
  }
}

export type DemoStepStatus = "upcoming" | "current" | "waiting" | "done" | "skipped";

/** One thing a step did, from the trace. */
export interface DemoFact {
  text: string;
  /** Text a model wrote (replayed): rendered as untrusted, with its author. */
  model?: string;
  tone?: Tone;
  /** Rule codes behind the fact, for the technical details and the plain-English explanation. */
  codes?: string[];
}

export interface DemoStepView extends DemoStepDef {
  status: DemoStepStatus;
  /** When the step began, in ms after the run's first event; null when it did not happen. */
  at: number | null;
  /** How many trace events belong to it. */
  events: number;
  facts: DemoFact[];
  /** Why a skipped step did not happen. */
  skippedBecause: string | null;
  tone: Tone;
}

export interface DemoProgress {
  steps: DemoStepView[];
  /** The step the run is in now (the waiting approval, or the latest event's step). */
  current: DemoStepView | null;
  /** run.summary arrived, or the run reached a final state. */
  finished: boolean;
  /** The final lifecycle state, once there is one. */
  finalState: string | null;
  /** The latest investigation round (0 before the first). */
  round: number;
}

// --- facts per step -----------------------------------------------------------------------------

function last(events: readonly TraceEvent[], category: string, where: (e: TraceEvent) => boolean = () => true): TraceEvent | undefined {
  for (let i = events.length - 1; i >= 0; i -= 1) if (events[i].category === category && where(events[i])) return events[i];
  return undefined;
}

/** Where the run acts, from its first event: the simulated shop, a live target by name, or unknown. */
export function placeOf(events: readonly TraceEvent[]): { simulated: boolean | null; words: string } {
  const environment = record(record(events[0]?.payload).environment);
  if (environment.is_simulated === true) return { simulated: true, words: "the simulated shop" };
  if (environment.is_simulated === false) {
    const name = text(environment.name);
    return { simulated: false, words: name ? `${name}, a real system` : "a real system" };
  }
  return { simulated: null, words: "the target" };
}

function incidentFacts(events: readonly TraceEvent[]): DemoFact[] {
  const created = events.find((e) => e.category === "state.transition" && text(record(e.payload).to) === "CREATED");
  const incident = record(record(created?.payload).incident);
  const facts: DemoFact[] = [];
  const affected = list(incident.affected_services).map(text).filter(Boolean);
  if (affected.length) facts.push({ text: `Affected: ${affected.join(", ")}.` });
  for (const symptom of list(incident.symptoms).map(text).filter(Boolean).slice(0, 4)) facts.push({ text: symptom });
  const budget = record(incident.budget);
  const steps = count(budget.max_steps);
  const tools = count(budget.max_tool_calls);
  const tokens = count(budget.max_model_tokens);
  if (steps !== null && tools !== null) {
    facts.push({ text: `Budget: ${plural(steps, "step")}, ${plural(tools, "tool call")}${tokens !== null ? `, ${plural(tokens, "model token")}` : ""}.` });
  }
  return facts;
}

function fileName(path: string): string {
  const parts = path.split("/");
  return parts[parts.length - 1] || path;
}

function contextFacts(events: readonly TraceEvent[]): DemoFact[] {
  const facts: DemoFact[] = [];
  const docs = events.find((e) => e.category === "retrieval.docs" && text(e.stage) === "RETRIEVING");
  if (docs) {
    const results = list(record(docs.payload).results).map(record);
    const mode = text(record(docs.payload).mode);
    if (results.length) {
      const top = results[0];
      const where = text(top.heading) ? `“${text(top.heading)}” in ${fileName(text(top.doc_path))}` : fileName(text(top.doc_path));
      facts.push({ text: `Runbooks: ${plural(results.length, "relevant section")} found${mode ? ` (${mode} search)` : ""}; the best match is ${where}.` });
    } else {
      facts.push({ text: "Runbooks: no relevant section found.", tone: "warn" });
    }
  }
  const memory = events.find((e) => e.category === "memory.search");
  if (memory) {
    const hits = list(record(memory.payload).hits).length;
    facts.push(
      hits
        ? { text: `Experience memory: ${plural(hits, "similar past incident")} found; the verifier will still check any remembered diagnosis.` }
        : { text: "Experience memory: no similar past incident, so this incident is new to RECAP." },
    );
  }
  const skipped = events.find((e) => e.category === "memory.skip" && text(e.stage) === "RETRIEVING");
  if (skipped) facts.push({ text: `Experience memory not read: ${text(record(skipped.payload).reason) || "no reason recorded"}.` });
  return facts;
}

function strategyFacts(events: readonly TraceEvent[]): DemoFact[] {
  const facts: DemoFact[] = [];
  for (const event of events) {
    if (event.category !== "routing.decision") continue;
    const p = record(event.payload);
    const agents = list(p.agents).map(text).filter(Boolean);
    const round = count(p.round);
    const how = text(p.mode) === "parallel" ? `${plural(agents.length, "specialist")} in parallel` : "one investigator";
    const who = agents.length ? ` (${agents.map((a) => actorName(a).toLowerCase()).join(" and ")})` : "";
    const why = text(p.reason);
    const rule = text(p.rule_id);
    facts.push({ text: `${round !== null ? `Round ${round}: ` : ""}${how}${who}${why ? `, because ${why}` : ""}.`, codes: rule ? [rule] : [] });
  }
  return facts;
}

function investigationFacts(events: readonly TraceEvent[]): DemoFact[] {
  const agents = new Set<string>();
  let toolCalls = 0;
  let failed = 0;
  let modelCalls = 0;
  const evidence = new Set<string>();
  for (const event of events) {
    if (stepOf(event) !== "investigate") continue;
    if (event.agent) agents.add(event.agent);
    if (event.category === "tool.call") toolCalls += 1;
    if (event.category === "llm.call") modelCalls += 1;
    if (event.category === "tool.result") {
      const p = record(event.payload);
      if (p.ok === false) failed += 1;
      if (text(p.evidence_id)) evidence.add(text(p.evidence_id));
    }
  }
  const facts: DemoFact[] = [];
  if (agents.size || toolCalls) {
    facts.push({
      text: `${plural(agents.size, "agent")} made ${plural(modelCalls, "model request")} and ${plural(toolCalls, "read-only tool call")}, collecting ${plural(evidence.size, "evidence item")}.`,
    });
  }
  if (failed) facts.push({ text: `${plural(failed, "tool call")} failed; the agents saw the error and carried on.`, tone: "warn" });
  // Each agent's latest summary of what it found, as its model wrote it.
  const latest = new Map<string, string>();
  for (const event of events) {
    if (event.category !== "agent.finish") continue;
    const summary = text(record(event.payload).summary);
    if (summary) latest.set(text(record(event.payload).role) || event.agent || "", summary);
  }
  for (const [role, summary] of latest) facts.push({ text: summary, model: actorName(role) });
  return facts;
}

/** Plain words for a failure mechanism (recap/core/models.py FailureMechanism). */
export function mechanismWords(mechanism: string | null | undefined): string {
  const words: Record<string, string> = {
    bad_deployment: "a bad deployment",
    resource_pressure: "resource pressure (memory)",
    dependency_failure: "a failing dependency",
    config_error: "a configuration error",
    unknown: "an unknown cause",
  };
  const key = text(mechanism);
  return words[key] ?? key.replace(/_/g, " ");
}

function verifyFacts(events: readonly TraceEvent[]): DemoFact[] {
  const facts: DemoFact[] = [];
  let round = 0;
  for (const event of events) {
    if (event.category === "routing.decision") round = count(record(event.payload).round) ?? round + 1;
    if (event.category === "verification.merge") {
      const disagreements = list(record(event.payload).disagreements).length;
      if (disagreements) facts.push({ text: `The agents disagreed on ${plural(disagreements, "point")}; the verifier weighs both sides against the evidence.`, tone: "warn" });
    }
    if (event.category !== "verification.evidence" || "proposal_check" in record(event.payload)) continue;
    const p = record(event.payload);
    const status = text(p.status);
    const summary = text(p.summary);
    const prefix = round ? `Round ${round}: ` : "";
    if (status === "supported") {
      facts.push({ text: `${prefix}diagnosis verified. ${summary}`, tone: "ok" });
    } else {
      const focus = list(p.focus_services).map(text).filter(Boolean);
      const next = focus.length ? ` Next round looks at ${focus.join(", ")}.` : "";
      facts.push({ text: `${prefix}not enough evidence yet. ${summary}${status === "insufficient" ? next : ""}`, tone: status === "contradicted" || status === "conflict" ? "bad" : "warn" });
    }
    const unknown = list(p.unknown_evidence_ids).map(text).filter(Boolean);
    if (unknown.length) facts.push({ text: `A claim cited evidence that is not in this run: ${unknown.join(", ")}.`, tone: "warn", codes: ["V1-structural"] });
  }
  return facts;
}

/** A proposal in plain words: "restart the inventory service" (the action, its target, its version). */
export function actionWords(action: string, target: string, params: Record<string, unknown> = {}): string {
  const version = text(params.to_version) || text(params.version) || text(params.revision);
  switch (action) {
    case "restart_service":
      return `restart the ${target} service`;
    case "execute_rollback":
      return version ? `roll ${target} back to ${version}` : `roll ${target} back to its previous release`;
    case "delete_namespace":
      return `delete the ${target} namespace`;
    default:
      return `run ${action} on ${target}`;
  }
}

function proposeFacts(events: readonly TraceEvent[]): DemoFact[] {
  const facts: DemoFact[] = [];
  const proposed = last(events, "mitigation.proposed");
  if (!proposed) return facts;
  const proposal = record(record(proposed.payload).proposal);
  const action = text(proposal.action);
  const target = text(proposal.target);
  facts.push({ text: `Proposed: ${actionWords(action, target, record(proposal.params))} (${action} on ${target}).` });
  const rationale = text(proposal.rationale);
  if (rationale) facts.push({ text: rationale, model: "the proposing model" });
  const check = last(events, "verification.evidence", (e) => "proposal_check" in record(e.payload));
  if (check) {
    const c = record(record(check.payload).proposal_check);
    const codes = list(c.rule_ids).map(text).filter(Boolean);
    if (c.ok === true) {
      facts.push({ text: "Evidence gate: passed. The fix cites this run's evidence and targets the diagnosed service.", tone: "ok", codes });
    } else {
      const problems = list(c.problems).map(text).filter(Boolean);
      facts.push({ text: `Evidence gate: rejected${problems.length ? `: ${problems.join("; ")}` : ""}.`, tone: "bad", codes });
    }
  }
  const policy = last(events, "policy.decision", (e) => text(e.stage) === "PROPOSING" && text(record(e.payload).tool) === action);
  if (policy) {
    const p = record(policy.payload);
    const verdict = text(p.verdict);
    const rules = list(p.rule_ids).map(text).filter(Boolean);
    const deciding = rules.length ? [rules[rules.length - 1]] : [];
    if (verdict === "APPROVAL_REQUIRED") facts.push({ text: "Safety policy: a person must approve this exact action.", tone: "warn", codes: deciding });
    else if (verdict === "DENY") facts.push({ text: "Safety policy: refused.", tone: "bad", codes: deciding });
    else if (verdict) facts.push({ text: `Safety policy: ${verdict.toLowerCase()}.`, codes: deciding });
  }
  return facts;
}

function approveFacts(events: readonly TraceEvent[]): DemoFact[] {
  const requested = last(events, "approval.requested");
  if (!requested) return [];
  const decided = last(events, "approval.decided", (e) => e.seq > requested.seq);
  const auto = /auto/i.test(text(record(requested.payload).channel));
  if (!decided) {
    return [{ text: auto ? "The simulation auto-approver is deciding (approval_mode auto)." : "Waiting for a person's decision.", tone: "warn" }];
  }
  const p = record(decided.payload);
  const decision = text(p.decision);
  const actor = text(p.actor);
  const by = actor ? ` by ${actor}` : "";
  const waited = isoMs(decided.ts) - isoMs(requested.ts);
  const after = Number.isFinite(waited) && waited >= 0 ? `, ${(waited / 1000).toFixed(1)} s after the request` : "";
  if (decision === "approved") return [{ text: `Approved${by}${after}.`, tone: "ok" }];
  if (decision === "rejected") return [{ text: `Rejected${by}${after}. The fix will not run.`, tone: "warn" }];
  if (decision === "expired") return [{ text: "Nobody decided before the request expired, so the fix will not run.", tone: "warn" }];
  return [{ text: `Decision recorded: ${decision || "not recorded"}${by}.` }];
}

function actFacts(events: readonly TraceEvent[]): DemoFact[] {
  const facts: DemoFact[] = [];
  const allowed = last(events, "policy.decision", (e) => text(e.stage) === "MITIGATING");
  if (allowed) {
    const p = record(allowed.payload);
    const rules = list(p.rule_ids).map(text).filter(Boolean);
    const verdict = text(p.verdict);
    facts.push(
      verdict === "ALLOW"
        ? { text: "Safety gateway: the approval matches this exact proposal, unused and unexpired.", tone: "ok", codes: rules.length ? [rules[rules.length - 1]] : [] }
        : { text: `Safety gateway: ${verdict.toLowerCase() || "no verdict recorded"}.`, tone: "bad", codes: rules.length ? [rules[rules.length - 1]] : [] },
    );
  }
  const executed = last(events, "mitigation.executed");
  if (executed) {
    const p = record(executed.payload);
    const effects = list(p.side_effects).map(text).filter(Boolean);
    const place = placeOf(events);
    const where = place.simulated === null ? "" : ` in ${place.words}`;
    if (p.ok === true) {
      facts.push({ text: `${text(p.tool)} ran once on ${text(p.target)}${where}${effects.length ? `: ${effects.join("; ")}` : ""}.`, tone: "ok" });
    } else {
      facts.push({ text: `${text(p.tool)} on ${text(p.target)} failed${text(p.error) ? `: ${text(p.error)}` : ""}.`, tone: "bad" });
    }
  }
  return facts;
}

function recoverFacts(events: readonly TraceEvent[]): DemoFact[] {
  const recovery = last(events, "verification.recovery");
  if (!recovery) return [];
  const p = record(recovery.payload);
  const oracle = text(p.oracle);
  const health = (value: unknown) => (value === true ? "healthy" : value === false ? "unhealthy" : "not recorded");
  const by = oracle ? ` (${oracle})` : "";
  if (p.verified === true) {
    const place = placeOf(events).words;
    return [{ text: `Recovery verified${by}: ${place} was ${health(p.healthy_before)} before the fix and ${health(p.healthy_after)} after it.`, tone: "ok" }];
  }
  const still = list(p.unhealthy_after).map(text).filter(Boolean);
  return [{ text: `Not recovered${by}${still.length ? `: still unhealthy: ${still.join(", ")}` : ""}.`, tone: "bad" }];
}

function outcomeFacts(events: readonly TraceEvent[]): DemoFact[] {
  const summary = last(events, "run.summary");
  if (!summary) return [];
  const p = record(summary.payload);
  const state = text(p.state);
  const note = text(p.outcome_note);
  const facts: DemoFact[] = [{ text: `${state || "Final state not recorded"}${note ? `: ${note}` : ""}.`, tone: state === "RESOLVED" ? "ok" : state === "BLOCKED" || state === "BUDGET_EXHAUSTED" ? "warn" : "bad" }];
  const budget = record(p.budget);
  const steps = count(budget.steps);
  const tools = count(budget.tool_calls);
  const tokens = count(budget.model_tokens);
  if (steps !== null && tools !== null) {
    facts.push({ text: `Used ${plural(steps, "agent step")}, ${plural(tools, "tool call")}${tokens !== null ? ` and ${plural(tokens, "model token")} (as recorded)` : ""}.` });
  }
  const blocked = count(p.unsafe_blocked);
  if (blocked) facts.push({ text: `${plural(blocked, "unsafe action")} blocked by the safety policy.`, tone: "warn" });
  const skipped = last(events, "memory.skip", (e) => stepOf(e) === "outcome");
  if (skipped) facts.push({ text: `Not saved to experience memory: ${text(record(skipped.payload).reason) || "no reason recorded"}.` });
  return facts;
}

const FACTS: Readonly<Record<DemoStepId, (events: readonly TraceEvent[]) => DemoFact[]>> = {
  incident: incidentFacts,
  context: contextFacts,
  strategy: strategyFacts,
  investigate: investigationFacts,
  verify: verifyFacts,
  propose: proposeFacts,
  approve: approveFacts,
  act: actFacts,
  recover: recoverFacts,
  outcome: outcomeFacts,
};

/** Why a step did not happen, from how the run went. */
function skipReason(id: DemoStepId, events: readonly TraceEvent[], finalState: string | null): string {
  const decided = last(events, "approval.decided");
  const decision = text(record(decided?.payload).decision);
  if ((id === "act" || id === "recover") && decision === "rejected") return "Not run: the fix was rejected, so nothing changed.";
  if ((id === "act" || id === "recover") && decision === "expired") return "Not run: the approval request expired, so nothing changed.";
  if (id === "context") return "RECAP did not search runbooks or memory in this run.";
  if (finalState === "BUDGET_EXHAUSTED") return "Not reached: a run limit stopped the run safely first.";
  if (finalState === "BLOCKED") return "Not reached: the fix was refused before this step.";
  if (finalState === "FAILED") return "Not reached: the run ended without a verified diagnosis or recovery.";
  if (finalState === "QUOTA_EXHAUSTED") return "Not reached: the model's quota ran out and the run was deferred.";
  return "Not part of this run.";
}

/**
 * A finished step is green unless something in it went wrong (a refusal, a failed fix, no
 * recovery). A rejected or expired approval, and a run that ended blocked or out of budget, are
 * amber: RECAP stopped safely, but nothing was fixed.
 */
function stepTone(id: DemoStepId, status: DemoStepStatus, facts: readonly DemoFact[], finalState: string | null): Tone {
  if (status === "waiting") return "warn";
  if (status === "current") return "accent";
  if (status !== "done") return "neutral";
  if (id === "outcome" && finalState !== "RESOLVED") return finalState === "BLOCKED" || finalState === "BUDGET_EXHAUSTED" ? "warn" : "bad";
  // The verifier's latest verdict is the step's: a first round sent back, then a verified one, is green.
  if (id === "verify") return [...facts].reverse().find((f) => f.tone)?.tone ?? "ok";
  if (facts.some((f) => f.tone === "bad")) return "bad";
  if (id === "approve" && facts.some((f) => f.tone === "warn")) return "warn";
  return "ok";
}

/** The ten steps as the trace tells them so far. `ended`: the run is known to be over (no more events). */
export function demoProgress(events: readonly TraceEvent[], ended = false): DemoProgress {
  const t0 = events.length ? isoMs(events[0].ts) : Number.NaN;
  const first = new Map<DemoStepId, TraceEvent>();
  const counts = new Map<DemoStepId, number>();
  let latest: DemoStepId | null = null;
  let finalState: string | null = null;
  let round = 0;
  for (const event of events) {
    const step = stepOf(event);
    if (event.category === "routing.decision") round = count(record(event.payload).round) ?? round + 1;
    if (event.category === "state.transition" && TERMINAL.has(text(record(event.payload).to))) finalState = text(record(event.payload).to);
    if (event.category === "run.summary" && text(record(event.payload).state)) finalState = text(record(event.payload).state);
    if (!step) continue;
    if (!first.has(step)) first.set(step, event);
    counts.set(step, (counts.get(step) ?? 0) + 1);
    latest = step;
  }
  const finished = ended || finalState !== null || events.some((e) => e.category === "run.summary");
  const requested = last(events, "approval.requested");
  const waiting = !finished && !!requested && !last(events, "approval.decided", (e) => e.seq > requested.seq);
  const reached = latest !== null ? STEP_INDEX[latest] : -1;

  const steps = DEMO_STEPS.map((def, index): DemoStepView => {
    const started = first.get(def.id);
    const at = started ? isoMs(started.ts) - t0 : Number.NaN;
    const facts = started ? FACTS[def.id](events) : [];
    let status: DemoStepStatus;
    if (def.id === "approve" && waiting) status = "waiting";
    else if (started && def.id === latest && !finished) status = "current";
    else if (started) status = "done";
    else if (finished || index < reached) status = "skipped";
    else status = "upcoming";
    return {
      ...def,
      status,
      at: Number.isFinite(at) ? Math.max(0, at) : null,
      events: counts.get(def.id) ?? 0,
      facts,
      skippedBecause: status === "skipped" ? skipReason(def.id, events, finalState) : null,
      tone: stepTone(def.id, status, facts, finalState),
    };
  });
  const current = steps.find((s) => s.status === "waiting") ?? steps.find((s) => s.status === "current") ?? (finished ? steps[steps.length - 1] : null);
  return { steps, current, finished, finalState, round };
}

// ==================================================================================================
// Who is working now
// ==================================================================================================

export interface DemoActor {
  /** The role (an AgentRole value), or "verifier", "operator", "oracle". */
  id: string;
  name: string;
  does: string;
  /** What it did last, in plain words; `model` when a model wrote it. */
  doing: string | null;
  model: boolean;
  kind: "agent" | "rules" | "person" | "oracle";
}

/** A tool call in plain words: "read frontend's error_rate metric (last 60 min)". */
export function toolWords(tool: string, args: Record<string, unknown> = {}): string {
  const service = text(args.service);
  const of = service ? ` of ${service}` : "";
  switch (tool) {
    case "list_services":
      return "listed the services";
    case "get_service_health":
      return `checked ${service || "a service"}'s health`;
    case "get_service_logs":
      return `read ${service ? `${service}'s` : "the"} logs`;
    case "get_service_metrics": {
      const window = count(args.window_minutes);
      return `read ${service ? `${service}'s` : "a"} ${text(args.metric) || "metric"} metric${window !== null ? ` (last ${window} min)` : ""}`;
    }
    case "get_recent_deployments":
      return `read the recent deployments${of}`;
    case "get_k8s_like_events":
      return `read the cluster events${service ? ` for ${service}` : ""}`;
    case "propose_rollback":
      return `asked for a rollback plan (a dry run) for ${service || "a service"}`;
    case "search_runbooks":
      return "searched the runbooks";
    case "execute_rollback":
    case "restart_service":
    case "delete_namespace":
      return actionWords(tool, service || text(args.namespace) || "the target", args);
    default:
      return `called ${tool}`;
  }
}

function lastDoing(events: readonly TraceEvent[], role: string): { doing: string | null; model: boolean } {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i];
    if (event.agent !== role) continue;
    const p = record(event.payload);
    if (event.category === "tool.call") {
      const words = toolWords(text(p.tool), record(p.args));
      return { doing: `${words.charAt(0).toUpperCase()}${words.slice(1)}.`, model: false };
    }
    if (event.category === "agent.finish" && text(p.summary)) return { doing: text(p.summary), model: true };
    if (event.category === "llm.call") return { doing: "Waiting for the recorded model's next answer.", model: false };
    if (event.category === "agent.start") return { doing: "Starting its investigation.", model: false };
  }
  return { doing: null, model: false };
}

/** Who is working in the current step: the running agents, the verifier, you, the gateway or the oracle. */
export function activeActors(events: readonly TraceEvent[], progress: DemoProgress): DemoActor[] {
  const step = progress.current?.id ?? null;
  if (step === "investigate" || step === "strategy") {
    const running: string[] = [];
    let round = 0;
    for (const event of events) {
      if (event.category === "routing.decision") round = count(record(event.payload).round) ?? round + 1;
    }
    for (const event of events) {
      const p = record(event.payload);
      if (count(p.round) !== round) continue;
      const role = text(p.role) || event.agent || "";
      if (event.category === "agent.start" && role && !running.includes(role)) running.push(role);
      if (event.category === "agent.finish" && running.includes(role)) running.splice(running.indexOf(role), 1);
    }
    return running.map((role) => ({ id: role, name: actorName(role), does: ROLE_WORDS[role] ?? "An AI agent.", kind: "agent", ...lastDoing(events, role) }));
  }
  if (step === "verify") {
    return [{ id: "evidence_verifier", name: "Evidence verifier", does: ROLE_WORDS.evidence_verifier, doing: null, model: false, kind: "rules" }];
  }
  if (step === "propose" || step === "act") {
    return [{ id: "controller", name: actorName("controller"), does: ROLE_WORDS.controller, doing: null, model: false, kind: "rules" }];
  }
  if (step === "approve" && progress.current?.status === "waiting") {
    return [{ id: "operator", name: "You, the operator", does: "Decide whether this exact fix may run. The run waits for you.", doing: null, model: false, kind: "person" }];
  }
  if (step === "recover") {
    return [{ id: "oracle", name: "Independent oracle", does: "Checks the target's health with its own reads, never through the agents.", doing: null, model: false, kind: "oracle" }];
  }
  return [];
}

// ==================================================================================================
// Model and tool activity, with provenance
// ==================================================================================================

export interface DemoActivity {
  seq: number;
  /** ms after the run's first event. */
  at: number | null;
  kind: "model" | "tool" | "policy";
  who: string;
  text: string;
  /** The technical detail: event category and IDs, for the advanced view. */
  detail: string;
  tone: Tone;
  /** A replayed model answer (llm.call origin "replay"), never fresh inference. */
  replayed: boolean;
}

/** The newest model answers, tool results and refusals, newest last (read ALLOWs are left out as noise). */
export function recentActivity(events: readonly TraceEvent[], limit = 6): DemoActivity[] {
  const t0 = events.length ? isoMs(events[0].ts) : Number.NaN;
  const calls = new Map<string, Record<string, unknown>>();
  const out: DemoActivity[] = [];
  for (const event of events) {
    const p = record(event.payload);
    const at = isoMs(event.ts) - t0;
    const base = { seq: event.seq, at: Number.isFinite(at) ? Math.max(0, at) : null, who: actorName(event.agent) };
    if (event.category === "tool.call") calls.set(text(p.invocation_id), p);
    if (event.category === "llm.call") {
      const replayed = p.replayed === true || text(p.origin) === "replay";
      const model = text(p.model) || "the model";
      const tokensIn = count(p.input_tokens);
      const tokensOut = count(p.output_tokens);
      const took = count(p.duration_ms);
      const what = text(p.response_kind) === "tool_request" ? "asked to call tools" : "gave its answer";
      const tokens = tokensIn !== null && tokensOut !== null ? ` · ${tokensIn.toLocaleString("en-US")} tokens in, ${tokensOut.toLocaleString("en-US")} out` : "";
      const timing = took !== null ? ` · originally took ${(took / 1000).toFixed(1)} s` : "";
      // An answer RECAP could not use: why, in RECAP's own words (its first note).
      const why = list(p.notes).map(text).filter(Boolean)[0];
      const refused = p.ok === false ? ` RECAP could not use it${why ? ` (${why})` : ""} and asked again.` : "";
      out.push({
        ...base,
        kind: "model",
        text: `${replayed ? `Recorded answer of ${model} (replayed, not called now)` : `${model} answered`}: ${what}${tokens}${replayed ? timing : ""}.${refused}`,
        detail: `llm.call · purpose ${text(p.purpose) || "not recorded"} · origin ${text(p.origin) || "not recorded"} · attempt ${count(p.attempt) ?? "?"}`,
        tone: p.ok === false ? "warn" : "info",
        replayed,
      });
    } else if (event.category === "tool.result") {
      const call = calls.get(text(p.invocation_id)) ?? {};
      const words = toolWords(text(p.tool), record(call.args));
      const evidence = text(p.evidence_id);
      out.push({
        ...base,
        kind: "tool",
        text: p.ok === false ? `${base.who} ${words}: failed${text(p.error) ? ` (${text(p.error)})` : ""}.` : `${base.who} ${words}.`,
        detail: `tool.result · ${text(p.tool)}${evidence ? ` · evidence ${evidence}` : ""}${count(p.duration_ms) !== null ? ` · ${Math.round(count(p.duration_ms) ?? 0)} ms` : ""}`,
        tone: p.ok === false ? "warn" : "neutral",
        replayed: false,
      });
    } else if (event.category === "policy.decision" && text(p.verdict) && text(p.verdict) !== "ALLOW") {
      const rules = list(p.rule_ids).map(text).filter(Boolean);
      const verdict = text(p.verdict);
      out.push({
        ...base,
        kind: "policy",
        text: verdict === "DENY" ? `The safety policy refused ${text(p.tool)}.` : `The safety policy asked for a person's approval of ${text(p.tool)}.`,
        detail: `policy.decision · ${verdict} · ${rules.length ? rules[rules.length - 1] : "no rule recorded"}`,
        tone: verdict === "DENY" ? "bad" : "warn",
        replayed: false,
      });
    }
  }
  return out.slice(-limit);
}

// ==================================================================================================
// Problems worth showing, never hidden behind a green summary
// ==================================================================================================

export interface DemoNotice {
  tone: Tone;
  icon: IconName;
  title: string;
  text: string;
  /** Rule codes behind it; the page shows each one's plain-English meaning. */
  codes: string[];
}

export function demoNotices(events: readonly TraceEvent[]): DemoNotice[] {
  const notices: DemoNotice[] = [];
  const failedTools = events.filter((e) => e.category === "tool.result" && record(e.payload).ok === false);
  if (failedTools.length) {
    const first = record(failedTools[0].payload);
    notices.push({
      tone: "warn",
      icon: "alert",
      title: `${plural(failedTools.length, "tool call")} failed`,
      text: `For example ${text(first.tool)}${text(first.error) ? `: ${text(first.error)}` : ""}. The agent saw the error; nothing was retried behind its back.`,
      codes: [],
    });
  }
  // A model answer RECAP could not use (e.g. it repeated a tool call already made): RECAP says why
  // and asks the same agent again. Whether the next attempt was usable is in the trace too.
  const refused = events.filter((e) => e.category === "llm.call" && record(e.payload).ok === false);
  if (refused.length) {
    const first = refused[0];
    const why = list(record(first.payload).notes).map(text).filter(Boolean)[0] ?? "";
    const retried = events.some(
      (e) => e.category === "llm.call" && e.seq > first.seq && e.agent === first.agent && record(e.payload).ok === true && (count(record(e.payload).attempt) ?? 1) > 1,
    );
    notices.push({
      tone: retried ? "info" : "warn",
      icon: retried ? "info" : "alert",
      title: `RECAP refused ${plural(refused.length, "model answer")} and asked again`,
      text: `${actorName(first.agent)}${why ? `: ${why}` : ""}.${retried ? " The next attempt was usable." : " No later attempt was usable."}`,
      codes: [],
    });
  }
  for (const event of events) {
    const p = record(event.payload);
    if (event.category === "policy.decision" && text(p.verdict) === "DENY") {
      const rules = list(p.rule_ids).map(text).filter(Boolean);
      notices.push({ tone: "bad", icon: "shield", title: `The safety policy refused ${text(p.tool)}`, text: `Asked by ${actorName(event.agent ?? text(p.caller))}.`, codes: rules.length ? [rules[rules.length - 1]] : [] });
    }
    if (event.category === "verification.evidence" && "proposal_check" in p) {
      const check = record(p.proposal_check);
      if (check.ok === false) {
        notices.push({
          tone: "bad",
          icon: "shield",
          title: "The evidence gate rejected the proposed fix",
          text: list(check.problems).map(text).filter(Boolean).join("; ") || "No problem was recorded.",
          codes: list(check.rule_ids).map(text).filter(Boolean),
        });
      }
    }
    if (event.category === "approval.decided" && text(p.decision) === "expired") {
      notices.push({ tone: "warn", icon: "clock", title: "The approval request expired", text: "Nobody decided in time, so the fix did not run.", codes: [] });
    }
    if (event.category === "mitigation.executed" && p.ok === false) {
      notices.push({ tone: "bad", icon: "x", title: "The approved fix failed", text: text(p.error) || "No error was recorded.", codes: [] });
    }
    if (event.category === "verification.recovery" && p.verified === false) {
      const still = list(p.unhealthy_after).map(text).filter(Boolean);
      notices.push({ tone: "bad", icon: "x", title: "Recovery was not verified", text: still.length ? `Still unhealthy: ${still.join(", ")}.` : "The oracle did not see a recovery.", codes: [] });
    }
    if (event.category === "state.transition" && text(p.to) === "BUDGET_EXHAUSTED") {
      notices.push({ tone: "warn", icon: "stop", title: "A run limit was reached", text: text(p.reason) || "The run stopped safely.", codes: [] });
    }
  }
  return notices;
}

// ==================================================================================================
// The approval, in plain words
// ==================================================================================================

export interface ApprovalStory {
  /** "Restart the inventory service". */
  headline: string;
  ifApprove: string;
  ifReject: string;
  /** The risk tier from RECAP's trusted registry, in words; "" when not recorded. */
  risk: string;
}

export function approvalStory(proposal: { action: string; target: string; params?: Record<string, unknown> | null }, simulated: boolean, trustedRisk?: string | null): ApprovalStory {
  const words = actionWords(proposal.action, proposal.target, record(proposal.params));
  const where = simulated ? "against RECAP's simulated shop. Nothing real changes" : "against the live target";
  const risks: Record<string, string> = {
    reversible_write: "A reversible change: the safety policy lets it run only with a person's approval of this exact proposal.",
    high_risk: "High risk: the safety policy refuses it.",
    destructive: "Destructive: the safety policy always refuses it.",
  };
  return {
    headline: `${words.charAt(0).toUpperCase()}${words.slice(1)}`,
    ifApprove: `RECAP runs ${proposal.action} on ${proposal.target} once, through the safety gateway, ${where}. Then an independent oracle checks whether it recovered.`,
    ifReject: "Nothing runs. The run ends as blocked, and your decision is logged.",
    risk: risks[text(trustedRisk)] ?? "",
  };
}

// ==================================================================================================
// Starting and following a demo
// ==================================================================================================

export interface DemoPace {
  key: "guided" | "quick" | "instant";
  value: number;
  label: string;
  hint: string;
}

/** Paces for the demo (RunConfig.replay_pace): only the waiting changes, never what is replayed. */
export const DEMO_PACES: readonly DemoPace[] = [
  {
    key: "guided",
    value: 0.25,
    label: "Guided",
    hint: "Each recorded answer waits a quarter of the time the model originally took (at most 15 s), so you can read every step: the golden recording reaches your approval in about two minutes.",
  },
  {
    key: "quick",
    value: 0.05,
    label: "Quick",
    hint: "Each recorded answer waits 5% of the time the model originally took (at most 15 s): about half a minute to your approval.",
  },
  {
    key: "instant",
    value: 0,
    label: "Instant",
    hint: "Recorded answers come back at once: your approval is asked within seconds.",
  },
];

export const DEFAULT_DEMO_PACE: DemoPace = DEMO_PACES[0];

/** The pace a URL asks for (?pace=guided|quick|instant), else the guided one. */
export function paceFor(key: string | null | undefined): DemoPace {
  return DEMO_PACES.find((p) => p.key === key) ?? DEFAULT_DEMO_PACE;
}

/** When the recording was made, from its id ("20261025-121828-ollama-6a5cfd" -> "2026-10-25"); "" if unknown. */
export function recordedOn(recordingId: string | null | undefined): string {
  const match = /^(\d{4})(\d{2})(\d{2})-/.exec(text(recordingId));
  return match ? `${match[1]}-${match[2]}-${match[3]}` : "";
}

/** What the provenance label means for this recording, in plain words. */
export function provenanceWords(fixture: Pick<FixtureView, "model" | "provider" | "recording_id"> | null): string {
  const model = fixture ? `${fixture.model}${fixture.provider ? ` (served by ${fixture.provider})` : ""}` : "a real model";
  const on = fixture ? recordedOn(fixture.recording_id) : "";
  return (
    `The AI decisions are the answers ${model} gave in a real run${on ? ` on ${on}` : ""}, each request answered from the recording: no model is called now. ` +
    "The tools, the safety checks, your approval, the fix and the recovery check all run now, against RECAP's simulated shop. Nothing real is changed."
  );
}

/** The demo's address: a run to follow (or the start screen), and interview mode. */
export function demoPageHref(opts: { run?: string | null; interview?: boolean; pace?: string | null } = {}): string {
  const query = new URLSearchParams();
  if (opts.run) query.set("run", opts.run);
  if (opts.interview) query.set("mode", "interview");
  if (opts.pace) query.set("pace", opts.pace);
  const qs = query.toString();
  return `#/demo${qs ? `?${qs}` : ""}`;
}

/** The recording the demo replays: the launchable "golden" one, else the first launchable. */
export function goldenFixture(fixtures: readonly FixtureView[]): FixtureView | null {
  const launchable = fixtures.filter((f) => f.launchable);
  return launchable.find((f) => f.tags.includes("golden")) ?? launchable[0] ?? null;
}
