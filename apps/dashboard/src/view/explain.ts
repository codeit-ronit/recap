/**
 * Plain-English explanations of the rule codes RECAP records, so a page can say what happened
 * first and keep the code for its technical details:
 *
 *   R0 to R14   the safety policy (recap/safety/policy.py) and the gateway's R13 (recap/tools/gateway.py)
 *   V1 to V7    the verifier's diagnosis rules (recap/agents/verifier.py)
 *   VP1 to VP4  the evidence gate's checks of a proposed fix (the same module)
 *   RT0 to RT7  the router's strategy rules (recap/routing/router.py)
 *
 * Each code has what the rule checks and what it means when it refused or contradicted
 * something. The sentences restate those modules' own documentation; a code this file does not
 * know reads as itself, never as a guess.
 *
 * Pure, with no imports, so tests/guided-demo.test.ts loads this file directly.
 */

export interface RuleExplanation {
  /** The code as recorded, e.g. "VP4-restore-point". */
  code: string;
  /** Which part of RECAP applies the rule. */
  family: string;
  /** What the rule checks, in one sentence. */
  checks: string;
  /** What it means when the rule refused, contradicted or held something back. */
  failed: string;
}

const POLICY = "Safety policy";
const VERIFIER = "Verifier";
const GATE = "Evidence gate";
const ROUTER = "Router";

const RULES: Readonly<Record<string, Omit<RuleExplanation, "code">>> = {
  "R0-safety-disabled-simulation": {
    family: POLICY,
    checks: "Records that the safety checks were switched off, which RECAP allows only in a simulated environment.",
    failed: "The safety checks were switched off for this simulated run.",
  },
  "R1-tool-name-format": {
    family: POLICY,
    checks: "A tool's name must be a plain lowercase identifier, which blocks look-alike names.",
    failed: "Call refused: the tool's name is not a plain lowercase identifier.",
  },
  "R2-known-tool": {
    family: POLICY,
    checks: "Only tools in RECAP's trusted registry can be called.",
    failed: "Call refused: the tool is not in RECAP's trusted registry.",
  },
  "R3-safety-scope": {
    family: POLICY,
    checks: "The safety checks may be switched off only in a simulated environment.",
    failed: "Refused: the safety checks cannot be switched off outside the simulation.",
  },
  "R4-args-valid": {
    family: POLICY,
    checks: "Every argument of a call must pass strict validation.",
    failed: "Call refused: its arguments did not pass validation.",
  },
  "R5-namespace-allowlist": {
    family: POLICY,
    checks: "Only allowlisted environments and namespaces can be touched.",
    failed: "Call refused: the environment or namespace is not on the allowlist.",
  },
  "R6-service-allowlist": {
    family: POLICY,
    checks: "Only allowlisted services can be named (checked with the arguments).",
    failed: "Call refused: the service is not on the allowlist.",
  },
  "R7-worker-read-only": {
    family: POLICY,
    checks: "Investigating agents may only read; only RECAP's controller may change anything.",
    failed: "Call refused: an investigating agent asked for a tool that changes something, which only the controller may use.",
  },
  "R8-read-allow": {
    family: POLICY,
    checks: "Read-only calls are allowed automatically.",
    failed: "Read-only calls are allowed automatically.",
  },
  "R9-destructive-deny": {
    family: POLICY,
    checks: "Destructive tools, such as deleting a namespace, are always refused.",
    failed: "Refused: destructive tools are never allowed.",
  },
  "R10-high-risk-deny": {
    family: POLICY,
    checks: "High-risk tools are refused.",
    failed: "Refused: the tool is high-risk.",
  },
  "R11-reversible-approval": {
    family: POLICY,
    checks: "A reversible change needs a written proposal and a person's approval of that exact proposal.",
    failed: "Held back: a person must approve this exact change before it runs.",
  },
  "R12-approval-binding": {
    family: POLICY,
    checks: "The approval must match this exact proposal, and be unexpired and unused.",
    failed: "Refused: the approval does not match this exact proposal, has expired, or was already used.",
  },
  "R13-conflicting-remediation": {
    family: POLICY,
    checks: "One fix at a time per service: a fix in progress on the same target blocks a second one.",
    failed: "Refused: another fix for the same service is already in progress.",
  },
  "R14-remediation-catalog": {
    family: POLICY,
    checks: "A fix must use a tool from the trusted remediation catalog.",
    failed: "Refused: the fix does not use a tool from the trusted remediation catalog.",
  },
  "V1-structural": {
    family: VERIFIER,
    checks: "A diagnosis must name a target, a known failure mechanism, and evidence that exists in this run.",
    failed: "Diagnosis not accepted: it lacks a target or a known mechanism, or it cites evidence that is not in this run.",
  },
  "V1-grounding": {
    family: VERIFIER,
    checks: "A supported diagnosis must cite at least one piece of the evidence its rules relied on.",
    failed: "Diagnosis not accepted: it cites none of the evidence that supports it.",
  },
  "V1-consistency": {
    family: VERIFIER,
    checks: "Details that belong to a different failure mechanism are reported, never used.",
    failed: "Noted: the diagnosis named details of a different mechanism, so it was judged as filed.",
  },
  "V2-deploy-timing": {
    family: VERIFIER,
    checks: "A bad release must line up in time: the service's errors began between 5 minutes before and 30 minutes after it was deployed.",
    failed: "Bad-release diagnosis contradicted: the errors did not start around that deployment.",
  },
  "V2-upstream-dominance": {
    family: VERIFIER,
    checks: "A bad release is ruled out when most of the service's errors blame a dependency that is not healthy.",
    failed: "Bad-release diagnosis contradicted: most of the service's errors blame another failing service.",
  },
  "V3-resource": {
    family: VERIFIER,
    checks: "Resource pressure needs out-of-memory kills, or memory at 90% or more of the limit; out-of-memory log lines only corroborate.",
    failed: "Resource-pressure diagnosis not supported: no out-of-memory kill, and memory stayed below the limit.",
  },
  "V4-dependency": {
    family: VERIFIER,
    checks: "A failing dependency needs the service's own logs to show its calls to that dependency failing. It points the search there and is never fixed by itself.",
    failed: "Dependency diagnosis not supported: the service's logs do not show its calls to that dependency failing.",
  },
  "V5-memory": {
    family: VERIFIER,
    checks: "A diagnosis remembered from a past incident gets no free pass: it faces the same rules.",
    failed: "A remembered diagnosis did not hold up against this run's evidence.",
  },
  "V6-conflict-resolution": {
    family: VERIFIER,
    checks: "When log text disagrees with platform signals (health, metrics, cluster events), the platform signals win.",
    failed: "Log text lost to platform signals: what the logs claim is contradicted by health, metrics or cluster events.",
  },
  "V7-deepest-cause": {
    family: VERIFIER,
    checks: "Of the supported causes, the one that most other services' errors lead back to wins; equal causes stay an open conflict.",
    failed: "No single deepest cause: equally supported causes remain, so the conflict stays open.",
  },
  "VP1-evidence-exists": {
    family: GATE,
    checks: "A fix must cite evidence that exists in this run.",
    failed: "Proposed fix rejected: it cites evidence that is not in this run.",
  },
  "VP2-target-matches-diagnosis": {
    family: GATE,
    checks: "A fix must target the service the verified diagnosis names.",
    failed: "Proposed fix rejected: it targets a different service than the verified diagnosis.",
  },
  "VP3-action-named": {
    family: GATE,
    checks: "A fix must name an action from the remediation catalog.",
    failed: "Proposed fix rejected: it does not name a catalog action.",
  },
  "VP4-restore-point": {
    family: GATE,
    checks: "A rollback must restore a version the evidence names as safe: the release before the bad one, or the rollback dry run's target.",
    failed: "Proposed rollback rejected: the selected revision is not a verified safe restore point.",
  },
  "RT0-forced": {
    family: ROUTER,
    checks: "The run's configuration set the strategy (one investigator or parallel specialists).",
    failed: "The run's configuration set the strategy.",
  },
  "RT1-budget-floor": {
    family: ROUTER,
    checks: "Too little budget is left for parallel specialists, so one investigator continues.",
    failed: "Too little budget was left for parallel specialists, so one investigator continued.",
  },
  "RT2-cross-service": {
    family: ROUTER,
    checks: "Two or more services are affected, so telemetry and infrastructure specialists look side by side.",
    failed: "Two or more services were affected, so two specialists looked side by side.",
  },
  "RT3-disagreement": {
    family: ROUTER,
    checks: "The last round left conflicting claims that no evidence rule settles, so both specialists are asked again.",
    failed: "The last round left conflicting claims, so both specialists were asked again.",
  },
  "RT3-escalation": {
    family: ROUTER,
    checks: "The last round was inconclusive or an earlier fix failed, so more compute is spent.",
    failed: "The last round was inconclusive or an earlier fix failed, so RECAP spent more compute.",
  },
  "RT4-memory-conflict": {
    family: ROUTER,
    checks: "Similar past incidents disagree on the cause or the outcome, so two independent views are taken.",
    failed: "Similar past incidents disagreed, so RECAP took two independent views.",
  },
  "RT5-ambiguous-symptoms": {
    family: ROUTER,
    checks: "The symptoms span three or more categories, so the cause is ambiguous and two specialists look.",
    failed: "The symptoms spanned three or more categories, so two specialists looked.",
  },
  "RT6-familiar": {
    family: ROUTER,
    checks: "A verified similar incident is in memory, so one investigator confirms it.",
    failed: "A verified similar incident was in memory, so one investigator confirmed it.",
  },
  "RT7-default": {
    family: ROUTER,
    checks: "A clear single-service incident gets one investigator.",
    failed: "A clear single-service incident got one investigator.",
  },
};

/** "VP4-restore-point" -> "VP4"; "" when the code does not start like a rule id. */
function prefixOf(code: string): string {
  const match = /^(RT|VP|R|V)\d+/.exec(code);
  return match ? match[0] : "";
}

/**
 * The explanation of one rule code. A code recorded with a different suffix than the ones above
 * (a newer server) is explained by its number when that number has exactly one rule; otherwise,
 * and for anything unknown, the code stands for itself.
 */
export function explainRule(code: string | null | undefined): RuleExplanation {
  const id = (code ?? "").trim();
  const known = RULES[id];
  if (known) return { code: id, ...known };
  const prefix = prefixOf(id);
  const sameNumber = prefix ? Object.keys(RULES).filter((key) => prefixOf(key) === prefix) : [];
  if (sameNumber.length === 1) return { code: id, ...RULES[sameNumber[0]] };
  return { code: id, family: "Rule", checks: id ? `Rule ${id}.` : "No rule recorded.", failed: id ? `Rule ${id} applied.` : "No rule recorded." };
}

/** Whether RECAP has a plain-English explanation for this code. */
export function isExplained(code: string | null | undefined): boolean {
  return explainRule(code).family !== "Rule";
}

/** A tooltip for a rule code: its family and what it checks. */
export function ruleTitle(code: string | null | undefined): string {
  const rule = explainRule(code);
  return rule.family === "Rule" ? rule.checks : `${rule.family}: ${rule.checks}`;
}
