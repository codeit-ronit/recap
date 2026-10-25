/**
 * TypeScript mirror of the RECAP contracts (recap/core/models.py) and API responses
 * (apps/api/main.py, evals/runner.py).
 *
 * JSON field names are exactly the Python field names. Enums arrive as strings and
 * datetimes as ISO-8601 strings. Only the shapes the dashboard reads are mirrored.
 *
 * Trace-event payloads (bottom of this file) are typed with every key OPTIONAL: they are
 * produced by several backend modules, so the UI must tolerate missing keys.
 */

export type ISODateTime = string;

// ===================================================================================
// 1. Enums
// ===================================================================================

export type IncidentState =
  | "CREATED"
  | "RETRIEVING"
  | "INVESTIGATING"
  | "PROPOSING"
  | "AWAITING_APPROVAL"
  | "MITIGATING"
  | "VERIFYING"
  | "RESOLVED"
  | "FAILED"
  | "BLOCKED"
  | "BUDGET_EXHAUSTED"
  | "QUOTA_EXHAUSTED"; // a free model's quota ran out: the run was deferred, not failed

export type AgentRole =
  | "investigator"
  | "telemetry_investigator"
  | "infrastructure_investigator"
  | "evidence_verifier"
  | "controller";

export type SourceType = "tool" | "doc" | "memory" | "alert" | "oracle";
export type TrustLevel = "trusted" | "untrusted";
export type ToolRisk = "read" | "reversible_write" | "high_risk" | "destructive";
export type Verdict = "ALLOW" | "APPROVAL_REQUIRED" | "DENY";
export type HypothesisStatus = "proposed" | "supported" | "contradicted" | "insufficient";
export type FailureMechanism =
  | "bad_deployment"
  | "resource_pressure"
  | "dependency_failure"
  | "unknown";
export type StrategyMode = "single" | "parallel";
export type EpisodeOutcome = "success" | "failed" | "blocked" | "budget_exhausted";
// demo: scripted runs; live: runs whose decisions came from a real model (M3).
export type MemoryPartition = "train" | "test" | "demo" | "live";
export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired";
export type ActionResult =
  | "recovered"
  | "not_recovered"
  | "denied"
  | "rejected"
  | "expired"
  | "error";

// ===================================================================================
// 2. Incident & budget
// ===================================================================================

export interface RunBudget {
  max_steps: number;
  max_tool_calls: number;
  max_elapsed_seconds: number;
  max_estimated_tokens: number;
  max_parallel_agents: number;
  max_model_tokens?: number; // M3: tokens model servers report, reserved per request
}

export interface Incident {
  incident_id: string;
  scenario_id: string;
  service: string;
  affected_services: string[];
  symptoms: string[];
  created_at: ISODateTime;
  environment: string;
  metadata: Record<string, unknown>;
  budget: RunBudget;
}

export interface EnvironmentInfo {
  kind: string;
  name: string;
  namespace: string;
  is_simulated: boolean;
  supported_actions: string[];
  description: string;
  /** How long a fix may take to settle on a live target before the oracle's verdict counts (0 for the simulator). */
  stabilization_timeout_seconds: number;
  stabilization_poll_seconds: number;
}

export interface ScenarioInfo {
  id: string;
  title: string;
  description: string;
  family: string; // ground truth: never shown to agents
  partition: MemoryPartition;
  alert_service: string;
  tags: string[];
  expected: Record<string, unknown>; // eval-only ground truth
  run_overrides: Record<string, unknown>;
  default_seed: number | null;
  budget: Record<string, unknown>;
  requires_memory_of: string[];
}

// ===================================================================================
// 3. Evidence
// ===================================================================================

export interface Evidence {
  id: string;
  incident_id: string;
  source_type: SourceType;
  source_ref: string;
  timestamp: ISODateTime;
  content: string;
  data: Record<string, unknown>;
  tool_name: string | null;
  trust_level: TrustLevel;
  token_estimate: number;
  flags: string[]; // a Python tuple, serialized as a JSON array
  agent_role: AgentRole | null;
}

// ===================================================================================
// 4. Reasoning
// ===================================================================================

export interface Hypothesis {
  claim: string;
  mechanism: FailureMechanism;
  target_service: string | null;
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
  status: HypothesisStatus;
  details: Record<string, unknown>;
  origin: "investigation" | "memory";
  proposed_by: AgentRole | null;
  // M3: the model's own words on how sure it is. UNTRUSTED model text: show it as such,
  // never as a score, and never let it decide anything.
  confidence_note?: string;
}

export interface Usage {
  steps: number;
  tool_calls: number;
  estimated_tokens: number;
  actual_input_tokens: number | null;
  actual_output_tokens: number | null;
  external_cost_usd: number;
  is_estimate: boolean;
  unreported_calls?: number;
  model_requests?: number; // HTTP requests actually sent to a language model (0 for scripted / replay)
}

/** How an agent ended its turn (M3). "" on runs recorded before M3. */
export type FindingOutcome =
  | ""
  | "hypothesis"
  | "need_more_evidence"
  | "stop"
  | "step_limit"
  | "no_usable_answer"
  | "error";

export interface AgentFinding {
  agent_role: AgentRole;
  evidence_ids: string[];
  doc_refs?: string[]; // runbook chunk IDs this agent saw in its packs: its citations
  hypotheses: Hypothesis[];
  proposed_next_steps: string[];
  usage: Usage;
  summary: string;
  error: string | null;
  started_at: ISODateTime;
  finished_at: ISODateTime;
  round: number;
  outcome?: FindingOutcome;
  // Kept from a round a budget stop cut short: never verified, so never a finished view.
  partial?: boolean;
}

export interface HypothesisCheck {
  hypothesis: Hypothesis;
  passed: string[];
  failed: string[];
  rule_ids: string[];
  proposers?: AgentRole[]; // M3: every agent that made this claim
  basis_evidence_ids?: string[]; // M3: the evidence the passing rules relied on
  // What the merger keys the claim by: the bad release's version as the deployment evidence
  // writes it, or the blamed dependency, resolved from evidence when the claim names none;
  // "" otherwise. A value the claim named but no evidence shows stays as the claim wrote it.
  detail?: string;
}

/** M3: where agents' claims conflict, and how the verifier's rules settled it (if they did). */
export interface Disagreement {
  kind: "detail_conflict" | "competing_root_causes" | "cross_agent_contradiction" | "missing_view";
  claims: string[];
  agents: AgentRole[];
  evidence_ids: string[];
  resolved: boolean;
  rule_id: string; // "" while open; may list several rules, comma-separated
  resolution: string;
  question: string; // an open disagreement: what the next round was asked (trusted text)
}

export interface VerificationReport {
  status: "supported" | "contradicted" | "insufficient";
  best: Hypothesis | null;
  checks: HypothesisCheck[];
  unknown_evidence_ids: string[];
  merged_evidence_ids: string[];
  focus_services: string[];
  summary: string;
  disagreements?: Disagreement[];
  // M3: where the next round should look although no claims conflict (RECAP's own text).
  leads?: string[];
}

export interface ProposalCheck {
  ok: boolean;
  problems: string[];
  rule_ids: string[];
}

// ===================================================================================
// 5. Tools & safety
// ===================================================================================

export interface SafetyDecision {
  verdict: Verdict;
  reasons: string[];
  rule_ids: string[];
  tool_name: string;
  risk: ToolRisk | null;
  caller: AgentRole | null;
  policy_version: string;
  decided_at: ISODateTime;
}

export interface Approval {
  approval_id: string;
  incident_id: string;
  proposal_id: string;
  proposal_digest: string;
  scope: "single_action";
  decision: "approved" | "rejected";
  actor: string;
  decided_at: ISODateTime;
  expires_at: ISODateTime;
  consumed_at: ISODateTime | null;
}

// ===================================================================================
// 6. Mitigation & recovery
// ===================================================================================

export interface MitigationDraft {
  action: string;
  target: string;
  params: Record<string, unknown>;
  rationale: string;
  evidence_ids: string[];
  expected_health_change: string;
  rollback_hint: string;
  risk_level: ToolRisk; // declared by the proposer; NOT trusted
  hypothesis_claim: string | null;
  source: "runbook" | "memory" | "fallback" | "llm" | "redteam";
  citations: string[];
}

export interface MitigationProposal extends MitigationDraft {
  proposal_id: string;
  incident_id: string;
  created_at: ISODateTime;
}

export interface ApprovalRequest {
  proposal_id: string;
  run_id: string;
  incident_id: string;
  proposal_digest: string;
  proposal: MitigationProposal;
  status: ApprovalStatus;
  requested_at: ISODateTime;
  expires_at: ISODateTime;
}

export interface ActionRecord {
  action: string;
  target: string;
  params: Record<string, unknown>;
  result: ActionResult;
  at: ISODateTime;
  note: string;
}

export interface RecoveryResult {
  healthy_before: boolean;
  healthy_after: boolean;
  observed_signals: Record<string, unknown>;
  oracle_name: string;
  verified: boolean;
  checked_at: ISODateTime;
  services_checked: string[];
}

// ===================================================================================
// 7. Knowledge (RAG) and memory
// ===================================================================================

export interface DocChunk {
  chunk_id: string;
  doc_path: string;
  title: string;
  heading: string;
  text: string;
  checksum: string;
  provenance: Record<string, string>;
  token_estimate: number;
}

export interface RetrievedChunk {
  chunk: DocChunk;
  score: number;
  rank: number;
  retriever: string;
  reranker: string | null;
  // Why it ranked here (M3): bm25_rank, bm25, dense_rank, cosine, rrf.
  signals?: Record<string, number>;
}

export interface EvidenceSummary {
  evidence_id: string;
  summary: string;
}

export interface ExperienceEpisode {
  episode_id: string;
  origin_run_id: string;
  scenario_id: string;
  failure_family: string;
  partition: MemoryPartition;
  service: string;
  symptoms: string[];
  failure_mechanism: FailureMechanism | null;
  mechanism_target: string | null;
  verified_evidence: EvidenceSummary[];
  unsuccessful_actions: ActionRecord[];
  successful_action: ActionRecord | null;
  outcome: EpisodeOutcome;
  verified: boolean;
  verification_source: string | null;
  provenance: Record<string, unknown>;
  created_at: ISODateTime;
  // consolidator-v2 (M3): a deterministic summary of the run's trajectory.
  summary_method?: string;
  trajectory?: { agent: AgentRole | null; call: string; evidence_id: string; kind: string }[];
  checked_claims?: {
    mechanism: FailureMechanism;
    target: string | null;
    status: string;
    proposers: AgentRole[];
    rule_ids: string[];
    failed: string[];
    detail?: string; // the version or dependency that told this claim apart (HypothesisCheck.detail)
  }[];
  diagnosis_basis?: string[];
  fix_preconditions?: Record<string, string>;
  embedding_model?: string | null;
}

export interface MemoryHit {
  episode: ExperienceEpisode;
  similarity: number;
  mechanism_match: boolean | null;
  outcome: EpisodeOutcome;
  safe_to_reuse: boolean;
  reason: string;
  signals?: Record<string, number>; // lexical, semantic
  supporting?: string[];
  contradicting?: string[];
}

// ===================================================================================
// 8. Routing
// ===================================================================================

export interface RoutingSignals {
  services_affected: number;
  symptom_categories: string[];
  memory_hits: number;
  top_memory_similarity: number;
  familiar_success: boolean;
  memory_conflict: boolean;
  prior_failed_attempts: number;
  escalation: boolean;
  disagreement?: boolean; // M3: the last round left a conflict no evidence rule settled
  remaining_tool_calls: number;
  remaining_steps: number;
  max_parallel_agents: number;
}

export interface StrategyDecision {
  mode: StrategyMode;
  reason: string;
  rule_id: string;
  selected_agents: AgentRole[];
  provider: string;
  budget: Record<string, number>;
  signals: RoutingSignals;
  round: number;
}

// ===================================================================================
// 9-10. Providers, telemetry & run records
// ===================================================================================

/** Where a run's decisions came from (recap/core/models.py ProviderOrigin): the run-origin badge. */
export type ProviderOrigin = "scripted" | "local_llm" | "free_hosted_llm" | "paid_llm" | "replay";

export interface ProviderInfo {
  name: string;
  kind: "scripted" | "llm" | "replay";
  origin?: ProviderOrigin; // absent on runs recorded before M2
  model: string | null;
  description: string;
  mode?: "json" | "tools" | null;
  prompt_version?: string | null;
  replay_of?: Record<string, unknown> | null;
  recording?: string | null;
  red_team?: boolean; // a deliberately compromised planner wraps the provider (tests, evals)
}

/** GET /providers (recap/providers/catalog.py): one configured provider and whether it can run. */
export interface ProviderStatus {
  name: string;
  kind: ProviderInfo["kind"];
  origin: ProviderOrigin;
  model: string | null;
  mode: "json" | "tools" | null;
  description: string;
  default: boolean;
  status: "ready" | "needs_setup" | "stopped" | "disabled" | "unavailable";
  detail: string;
  usage?: { requests_last_minute: number; requests_last_24h: number; tokens_last_24h: number };
  // local (Ollama)
  base_url?: string;
  installed?: string[];
  // free hosted: a key is reported only as present or not, never its value
  key_env?: string;
  key_present?: boolean;
  opted_in?: boolean;
  confirmed_on?: string | null;
  confirmation_age_days?: number | null;
  allowed_models?: string[];
  limits_checked_on?: string;
  caps?: Record<string, number>;
  last_rate_limit?: Record<string, unknown> | null;
  stopped?: { stopped_at: string; reason: string } | null;
  // replay
  recording?: string;
  replay_of?: Record<string, unknown>;
}

export interface TraceEvent {
  event_id: string;
  run_id: string;
  seq: number;
  stage: string;
  ts: ISODateTime;
  agent: string | null;
  category: string;
  payload: Record<string, unknown>;
  duration_ms: number | null;
  usage: Usage | null;
  schema_version: string;
}

export interface RunConfig {
  strategy: "adaptive" | "single" | "parallel";
  use_rag: boolean;
  use_memory: boolean;
  memory_write: boolean;
  // null: chosen by the run's origin (scripted: demo; a real model: live).
  memory_read_partitions: MemoryPartition[] | null;
  memory_write_partition: MemoryPartition | null;
  retrieval?: "bm25" | "hybrid" | null;
  safety_enabled: boolean;
  approval_mode: "manual" | "auto";
  planner: "default" | "prompt_injected";
  provider: string | null;
  seed: number | null;
  budget: RunBudget | null;
  tool_latency_ms: number | null;
  label: string;
}

/** M3: how a run searched the runbooks (RunRecord.retrieval). */
export interface RetrievalInfo {
  mode: "bm25" | "hybrid";
  embedder: string | null;
  server: Record<string, unknown> | null; // what Ollama reported: version, weights file, size
  embedding_requests: number;
  embedding_input_tokens: number | null; // as the server reported them
  replayed_requests: number;
  agent_searches: number; // search_runbooks calls answered
}

export interface StateChange {
  state: IncidentState;
  at: ISODateTime;
  reason: string;
}

export interface RunRecord {
  run_id: string;
  scenario_id: string;
  config: RunConfig;
  incident: Incident | null;
  state: IncidentState;
  history: StateChange[];
  environment: EnvironmentInfo | null;
  provider: string;
  provider_info?: ProviderInfo | null; // absent on runs recorded before M2
  strategies: StrategyDecision[];
  findings: AgentFinding[];
  verification: VerificationReport | null;
  retrieved_docs: RetrievedChunk[];
  retrieval?: RetrievalInfo | null; // M3: how this run searched the runbooks
  memory_hits: MemoryHit[];
  proposals: MitigationProposal[];
  safety_decisions: SafetyDecision[];
  pending_approval: ApprovalRequest | null;
  approvals: Approval[];
  actions: ActionRecord[];
  recovery: RecoveryResult | null;
  episode_id: string | null;
  usage: Usage;
  outcome_note: string;
  error: string | null;
  versions: Record<string, unknown>;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

// ===================================================================================
// API response shapes (apps/api/main.py)
// ===================================================================================

export interface HealthResponse {
  status: string;
  version: string;
  environment: string;
  provider: string;
  label: string;
}

/** GET /system: which implementation is plugged into each interface. */
export interface SystemSummary {
  environment: EnvironmentInfo;
  environment_kind: string;
  tool_transport: string;
  provider: ProviderInfo;
  // What a run that does not choose searches with ("bm25" or "hybrid-bm25-embeddings"). Before
  // hybrid_ready existed, it named the capability instead (see format.ts hybridReadiness).
  retriever: string;
  retrieval_mode?: "bm25" | "hybrid"; // M3: the default for runs that do not choose
  hybrid_ready?: boolean; // M3: stored vectors cover every runbook chunk, so hybrid can run
  embedder?: string | null; // M3: the stored vectors' embedder, when loaded
  knowledge_chunks: number;
  router: string;
  safety_policy: string;
  verifier: string;
  oracle: string;
  memory_episodes: number;
  trace_sinks: string[];
  config_hash: string;
  fixture_hash: string;
}

/** One row of GET /runs (apps/api/main.py::_run_summary). */
export interface RunSummary {
  run_id: string;
  scenario_id: string;
  state: IncidentState;
  label: string;
  created_at: ISODateTime;
  updated_at: ISODateTime;
  modes: StrategyMode[];
  recovered: boolean;
  outcome_note: string;
  provider: string;
  tool_calls: number;
  pending_approval: boolean;
}

/**
 * RunManager.status() (apps/api/service.py). A run started by another process (the CLI, an
 * earlier server) is "finished" once its stored state is terminal and "external" until
 * then: its live events exist only in that process.
 */
export type RunStatus = "running" | "finished" | "error" | "unknown" | "external";

/** GET /runs/{id}. `record` is null while a just-started run has no checkpoint yet. */
export interface RunDetail {
  status: RunStatus;
  record: RunRecord | null;
  evidence: Evidence[];
  error: string | null;
}

/** RunConfig fields the dashboard sends when starting a run. */
export type RunConfigInput = Pick<
  RunConfig,
  "strategy" | "use_rag" | "use_memory" | "approval_mode" | "tool_latency_ms"
> & {
  /** M3: bm25 or hybrid; left out, the server's configs/retrieval.yaml default applies. */
  retrieval?: NonNullable<RunConfig["retrieval"]>;
};

export interface RunRequest {
  scenario_id: string;
  config: RunConfigInput;
}

export interface RunStartResponse {
  run_id: string;
  status: string;
}

/** POST /approvals/{id}. Who decided is taken from the operator token, never from the body. */
export interface ApprovalDecisionBody {
  decision: "approved" | "rejected";
  proposal_digest: string;
}

export type ApprovalEventKind = "requested" | "approved" | "rejected" | "revoked" | "expired" | "consumed" | "refused";

/** One entry of the append-only approval decision log (GET /approvals/{id}/events). */
export interface ApprovalEvent {
  event_id: number;
  proposal_id: string;
  kind: ApprovalEventKind;
  actor: string;
  at: string;
  proposal_digest: string;
  detail: string;
}

/** GET /auth/session: the signed-in operator. */
export interface SessionInfo {
  operator: string;
  actor: string;
}

/** POST /runs/{id}/cancel. */
export interface CancelResponse {
  run_id: string;
  status: string;
}

export interface ActionHint {
  mechanism: string;
  action: string;
}

/** GET /knowledge/search: a RetrievedChunk plus parsed hints and injection flags. */
export interface KnowledgeResult extends RetrievedChunk {
  action_hints: ActionHint[];
  flags: string[];
}

export interface KnowledgeDocRef {
  chunk_id: string;
  doc_path: string;
  title: string;
  heading: string;
}

// ===================================================================================
// Evaluation report (evals/runner.py::EvalReport)
// ===================================================================================

export interface EvalSplit {
  name: string;
  train: string[];
  test: string[];
  redteam: string[];
}

export interface CaseResult {
  config: string;
  suite: string; // "train" | "test" | "redteam"
  scenario_id: string;
  family: string;
  seed: number;
  final_state: string;
  expected_state: string;
  outcome_as_expected: boolean;
  recovered: boolean;
  diagnosed: string | null;
  expected_root: string;
  diagnosis_correct: boolean;
  routing_modes: string[];
  investigation_rounds: number;
  steps: number;
  tool_calls: number;
  estimated_tokens: number;
  latency_ms: number;
  mitigation_attempts: number;
  unsafe_attempts: number;
  unsafe_blocked: number;
  unsafe_executed: number;
  unsafe_refused?: number; // reached the sandbox, which refused it
  memory_hits: number;
  injection_flags: number;
  run_id: string;
}

export interface ConfigSummary {
  name: string;
  suite: string; // "test" | "redteam"
  description: string;
  n: number;
  outcome_as_expected: number;
  recovered: number;
  diagnosis_correct: number;
  parallel_rounds: number;
  mean_tool_calls: number;
  mean_steps: number;
  mean_estimated_tokens: number;
  mean_latency_ms: number;
  unsafe_attempts: number;
  unsafe_blocked: number;
  unsafe_executed: number;
  unsafe_refused?: number; // reached the sandbox, which refused it
}

export interface EvalReport {
  report_id: string;
  created_at: ISODateTime;
  recap_version: string;
  git_commit: string;
  config_hash: string;
  fixture_hash: string;
  provider: string;
  split: EvalSplit;
  latency: Record<string, number>;
  train: CaseResult[];
  cases: CaseResult[];
  summaries: ConfigSummary[];
  caveats: string[];
}

/**
 * GET /evals/results: the evaluation this server started (running, failed with `error`, or
 * its report); before it starts one, the committed evals/reports/latest. `report` is null
 * while an evaluation runs and after one fails.
 */
export interface EvalResults {
  running: boolean;
  progress: string[];
  report: EvalReport | null;
  error: string | null;
}

/** POST /evals/run */
export interface EvalStartResponse {
  status: string;
  report_dir: string;
}

// ===================================================================================
// Trace-event payloads (read from the controller/harness/gateway emit(...) calls).
// Every key is optional on purpose: handle absence with optional chaining.
// ===================================================================================

export interface StatePayload {
  from?: string | null;
  to?: string;
  reason?: string;
  incident?: Partial<Incident>;
  environment?: Partial<EnvironmentInfo>;
  provider?: Partial<ProviderInfo>;
}

/** llm.call: one request to a language model (recap/core/models.py ModelCall). */
export interface LlmCallPayload {
  step?: number;
  provider?: string;
  model?: string;
  model_reported?: string | null;
  origin?: string;
  purpose?: "investigate" | "propose";
  mode?: "json" | "tools";
  attempt?: number;
  status?: number | null;
  ok?: boolean;
  response_kind?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  duration_ms?: number;
  finish_reason?: string | null;
  prompt_sha256?: string;
  prompt_chars?: number;
  output_excerpt?: string;
  thinking_excerpt?: string;
  rate_limit?: Record<string, unknown>;
  replayed?: boolean;
  notes?: string[];
}

export interface RoutingPayload {
  round?: number;
  mode?: string;
  rule_id?: string;
  reason?: string;
  agents?: string[];
  budget?: Record<string, number>;
  signals?: Partial<RoutingSignals>;
  router?: string;
}

export interface InvestigationRoundPayload {
  round?: number;
  focus_services?: string[];
  escalation?: boolean;
  open_questions?: string[]; // M3: the verifier's questions for this round (RECAP's own text)
  leads?: string[]; // M3: the verifier's leads for this round (RECAP's own text)
}

export interface OrchestrationAgent {
  role?: string;
  started_at?: ISODateTime;
  finished_at?: ISODateTime;
  duration_ms?: number;
  error?: string | null;
}

export interface OrchestrationPayload {
  mode?: string;
  round?: number;
  agents?: OrchestrationAgent[];
  wall_ms?: number;
  sum_ms?: number;
  overlap_ms?: number;
  // The note a budget stop leaves instead (no mode): the agents whose findings were kept.
  partial_findings_kept?: string[];
  stopped_by?: string;
}

/** tool.discovery = recap/tools/registry.py::DiscoveryReport */
export interface DiscoveryPayload {
  transport?: string;
  advertised?: string[];
  usable?: string[];
  unknown_tools?: string[];
  missing_tools?: string[];
  annotation_mismatches?: string[];
}

export interface ToolCallPayload {
  invocation_id?: string;
  tool?: string;
  args?: Record<string, unknown>;
  caller?: string;
}

export interface ToolResultPayload {
  invocation_id?: string;
  tool?: string;
  ok?: boolean;
  error?: string | null;
  evidence_id?: string | null;
  duration_ms?: number;
  truncated?: boolean;
  flags?: string[];
}

export interface PolicyPayload {
  tool?: string;
  verdict?: string;
  rule_ids?: string[];
  reasons?: string[];
  caller?: string | null;
  invocation_id?: string;
  risk?: string | null;
}

export interface InjectionPayload {
  evidence_id?: string;
  patterns?: string[];
  tool?: string;
  excerpt?: string;
}

export interface ProposalPayload {
  proposal?: MitigationProposal | null;
  digest?: string;
  notes?: string[];
  trusted_risk?: string;
}

export interface ApprovalRequestedPayload {
  proposal_id?: string;
  digest?: string;
  expires_at?: ISODateTime;
  channel?: string;
}

export interface ApprovalDecidedPayload {
  decision?: string;
  actor?: string;
  approval_id?: string;
  proposal_id?: string;
}

export interface MitigationExecutedPayload {
  invocation_id?: string;
  tool?: string;
  target?: string;
  params?: Record<string, unknown>;
  ok?: boolean;
  error?: string | null;
  side_effects?: string[];
  approval_id?: string | null;
  proposal_digest?: string;
  evidence_id?: string | null;
  safety_enabled?: boolean;
  /** True when the run was cancelled while this write was in flight (its outcome is still recorded). */
  run_cancelled?: boolean;
}

export interface CheckSummary {
  claim?: string;
  mechanism?: string;
  target?: string | null;
  origin?: string;
  status?: string;
  passed?: string[];
  failed?: string[];
  rule_ids?: string[];
  proposers?: string[]; // M3: every agent that made this claim
  basis_evidence_ids?: string[]; // M3: the evidence the passing rules relied on
}

export interface EvidenceCheckPayload {
  status?: string;
  summary?: string;
  best?: Partial<Hypothesis> | null;
  checks?: CheckSummary[];
  unknown_evidence_ids?: string[];
  focus_services?: string[];
  proposal_check?: Partial<ProposalCheck>;
  proposal_id?: string;
  disagreements?: Partial<Disagreement>[]; // M3
  leads?: string[]; // M3: the report's leads for the next round (RECAP's own text)
  enforced?: boolean; // proposal checks: false in the simulation-only "safety off" ablation
}

/** verification.merge (M3, controller._merge_payload): the agents' claims and their conflicts. */
export interface VerificationMergePayload {
  claims_by_agent?: Record<string, number>;
  merged_claims?: number;
  shared_claims?: { mechanism?: string; target?: string | null; proposers?: string[] }[];
  disagreements?: { kind?: string; resolved?: boolean; rule_id?: string; claims?: string[] }[];
  open_questions?: string[];
  leads?: string[]; // RECAP's own text, like the open questions
}

export interface RecoveryPayload {
  oracle?: string;
  healthy_before?: boolean;
  healthy_after?: boolean;
  verified?: boolean;
  unhealthy_after?: string[];
  simulated?: boolean;
}

export interface DocRef {
  chunk_id?: string;
  doc_path?: string;
  heading?: string;
  score?: number;
  rank?: number;
  retriever?: string; // M3: bm25, or hybrid when the meaning ranking took part
  signals?: Record<string, number> | null; // M3: the rankings behind this result
}

/** One embedding request (recap/knowledge/ollama_embedder.py), as the server reported it. */
export interface EmbeddingCallPayload {
  model?: string;
  purpose?: string;
  inputs?: number;
  status?: number;
  input_tokens?: number | null;
  duration_ms?: number;
  replayed?: boolean;
}

export interface RetrievalPayload {
  stage?: string; // M3: "agent" for an agent's own search_runbooks call
  reason?: string;
  query?: string;
  mode?: "bm25" | "hybrid"; // M3
  retriever?: string; // what this search actually ranked with ("bm25" or "hybrid-bm25-embeddings")
  embedding?: EmbeddingCallPayload | null; // M3: the query's embedding request, in hybrid mode
  results?: DocRef[];
}

export interface MemorySearchHit {
  episode_id?: string;
  similarity?: number;
  outcome?: string;
  verified?: boolean;
  mechanism?: string | null;
  service?: string;
  partition?: string; // M3
  signals?: Record<string, number>; // M3: lexical always, semantic when the run embedded
}

export interface MemorySearchPayload {
  readable_partitions?: string[];
  semantic?: string | null; // M3: the embedder behind the semantic signal; null: not embedded
  // A replay reads memory as it was when its recording began: episodes created before this time.
  created_before?: string | null;
  hits?: MemorySearchHit[];
}

export interface MemoryAssessHit {
  episode_id?: string;
  similarity?: number;
  mechanism_match?: boolean | null;
  safe_to_reuse?: boolean;
  reason?: string;
  supporting?: string[]; // M3: the reuse conditions that held
  contradicting?: string[]; // M3: what argues against reuse
}

export interface MemoryAssessPayload {
  hits?: MemoryAssessHit[];
  restore_points?: string[] | null; // M3: versions today's evidence names; null: none looked up
}

export interface MemoryWritePayload {
  episode_id?: string;
  outcome?: string;
  verified?: boolean;
  partition?: string;
  mechanism?: string | null;
}

export interface ContextPackPayload {
  step?: number;
  tokens_before?: number;
  tokens_after?: number;
  budget_tokens?: number;
  items?: string[];
  dropped?: string[];
  estimate?: boolean;
}

export interface AgentStartPayload {
  role?: string;
  round?: number;
  max_steps?: number;
  focus_services?: string[];
  questions?: string[]; // M3: the verifier's open questions this agent was asked
  tools?: string[];
}

export interface AgentDecisionPayload {
  step?: number;
  action?: string;
  tool_calls?: { tool_name?: string; args?: Record<string, unknown>; reason?: string }[];
  summary?: string;
  hypotheses?: string[];
}

export interface AgentFinishPayload {
  role?: string;
  round?: number;
  steps?: number;
  tool_calls?: number;
  evidence_ids?: string[];
  hypotheses?: string[];
  summary?: string;
  outcome?: FindingOutcome; // M3: how the agent ended its turn
  dropped_out_of_role?: string[]; // M3: mechanisms of claims its role contract may not make
}

export interface BudgetPayload {
  limit?: string;
  detail?: string;
}

export interface RunSummaryPayload {
  state?: string;
  outcome_note?: string;
  recovered?: boolean;
  mitigation_attempts?: number;
  unsafe_attempts?: number;
  unsafe_blocked?: number;
  unsafe_executed?: number;
  unsafe_refused_by_environment?: number;
  budget?: Record<string, unknown>;
  cost?: string;
  simulated?: boolean;
  episode_id?: string | null;
}
