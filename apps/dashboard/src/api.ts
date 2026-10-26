/**
 * Typed client for the RECAP FastAPI backend (apps/api/main.py).
 *
 * Every number on the dashboard comes from these calls; nothing is hardcoded.
 *
 * Base URL:
 *   - dev  (`npm run dev`):  "/api", proxied by Vite to http://127.0.0.1:8000
 *   - prod (`npm run build`): "", because FastAPI serves the built app and the API from
 *     the same origin (API routes at the root)
 *   - VITE_API_URL overrides both.
 *
 * State-changing calls carry the operator token (src/auth.ts); a 401 means "sign in".
 */

import { SIGN_IN_HINT, authHeaders } from "./auth";
import type {
  Approval,
  ApprovalDecisionBody,
  ApprovalEvent,
  ApprovalRequest,
  CancelResponse,
  EvalResults,
  EvalStartResponse,
  ExperienceEpisode,
  HealthResponse,
  KnowledgeDocRef,
  KnowledgeResult,
  MemoryPartition,
  ProviderStatus,
  RunDetail,
  RunRequest,
  RunStartResponse,
  RunSummary,
  ScenarioInfo,
  SessionInfo,
  SystemSummary,
  TraceEvent,
} from "./types";
// The M4 typed API (schema recap-api-v1): types generated from apps/api/dto.py.
import type * as dto from "./dto";

export const API_BASE: string = (
  import.meta.env.VITE_API_URL ?? (import.meta.env.DEV ? "/api" : "")
).replace(/\/+$/, "");

/** The command that starts the backend (shown whenever the API is unreachable). */
export const BACKEND_COMMAND = "uvicorn apps.api.main:app --reload";

/** An HTTP or network failure, with the server's `detail` message when it sent one. */
export class ApiError extends Error {
  readonly status: number; // 0 = the request never reached the API
  readonly path: string;
  readonly body: unknown;
  readonly offline: boolean;

  constructor(message: string, status: number, path: string, body: unknown, offline: boolean) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.path = path;
    this.body = body;
    this.offline = offline;
  }
}

/** Normalize anything thrown by a fetch helper into an ApiError. */
export function toApiError(err: unknown, path = ""): ApiError {
  if (err instanceof ApiError) return err;
  const message = err instanceof Error ? err.message : String(err);
  return new ApiError(message, 0, path, null, false);
}

/** FastAPI errors look like {"detail": "..."} or, for validation errors, {"detail": [{msg}]}. */
function detailMessage(body: unknown): string | null {
  if (!body || typeof body !== "object") return typeof body === "string" && body ? body : null;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        if (d && typeof d === "object" && "msg" in d) {
          const loc = Array.isArray((d as { loc?: unknown }).loc)
            ? ((d as { loc: unknown[] }).loc.join(".") + ": ")
            : "";
          return loc + String((d as { msg: unknown }).msg);
        }
        return String(d);
      })
      .join("; ");
  }
  return null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  return (await requestWithHeaders<T>(path, init)).body;
}

/** One page of a list endpoint: its items and the server's count of all matches. */
export interface Page<T> {
  items: T[];
  /** X-Total-Count (null when the server did not send it). */
  total: number | null;
}

async function requestPage<T>(path: string): Promise<Page<T>> {
  const { body, headers } = await requestWithHeaders<T[]>(path);
  const raw = headers?.get("X-Total-Count");
  const total = raw !== null && raw !== undefined && raw !== "" ? Number(raw) : null;
  return { items: body, total: total !== null && Number.isFinite(total) ? total : null };
}

/** A query string from the defined values only ("" when nothing is set). */
export function queryString(params: Record<string, string | number | boolean | null | undefined>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") query.set(key, String(value));
  }
  const text = query.toString();
  return text ? `?${text}` : "";
}

async function requestWithHeaders<T>(
  path: string,
  init?: RequestInit,
): Promise<{ body: T; headers: Headers | null }> {
  const url = API_BASE + path;
  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...authHeaders(), // the operator token, when signed in (reads do not need it)
        ...(init?.headers ?? {}),
      },
    });
  } catch {
    throw new ApiError("Cannot reach the RECAP API.", 0, path, null, true);
  }

  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!response.ok) {
    // The Vite dev proxy answers 500 with an empty body when the backend is down;
    // a gateway error means the same thing behind other proxies.
    const offline = [502, 503, 504].includes(response.status) || (response.status === 500 && !text);
    const message = offline
      ? "Cannot reach the RECAP API."
      : response.status === 401
        ? SIGN_IN_HINT
        : (detailMessage(body) ?? `${response.status} ${response.statusText}`);
    throw new ApiError(message, response.status, path, body, offline);
  }
  return { body: body as T, headers: response.headers ?? null };
}

/** GET /runs filters (all optional; the server matches them exactly). */
export interface RunFilters {
  limit?: number;
  offset?: number;
  provider_class?: dto.ProviderClass;
  provider?: string;
  scenario?: string;
  state?: string;
  environment?: string;
  label?: string;
}

/** GET /runs/{id}/timeline paging and filters (resume with next_after_seq). */
export interface TimelineQuery {
  after_seq?: number;
  limit?: number;
  agent?: string;
  round?: number;
  kind?: dto.TimelineKind;
  tool?: string;
}

const enc = encodeURIComponent;

export const api = {
  health: () => request<HealthResponse>("/health"),
  system: () => request<SystemSummary>("/system"),
  providers: () => request<ProviderStatus[]>("/providers"),
  scenarios: () => request<ScenarioInfo[]>("/scenarios"),

  runs: (limit = 100) => request<RunSummary[]>(`/runs?limit=${limit}`),
  run: (runId: string) => request<RunDetail>(`/runs/${enc(runId)}`),
  events: (runId: string, afterSeq = 0) =>
    request<TraceEvent[]>(`/runs/${enc(runId)}/events?after_seq=${afterSeq}`),
  startRun: (body: RunRequest) =>
    request<RunStartResponse>("/incidents/run", { method: "POST", body: JSON.stringify(body) }),
  cancelRun: (runId: string) =>
    request<CancelResponse>(`/runs/${enc(runId)}/cancel`, { method: "POST" }),

  /** Who a token belongs to; `token` checks a token before it is saved (the sign-in box). */
  session: (token?: string) =>
    request<SessionInfo>("/auth/session", token ? { headers: authHeaders(token) } : undefined),

  pendingApprovals: () => request<ApprovalRequest[]>("/approvals/pending"),
  approvalEvents: (proposalId: string) =>
    request<ApprovalEvent[]>(`/approvals/${enc(proposalId)}/events`),
  recentApprovalEvents: (limit = 30) => request<ApprovalEvent[]>(`/approvals/events?limit=${limit}`),
  decideApproval: (proposalId: string, body: ApprovalDecisionBody) =>
    request<Approval>(`/approvals/${enc(proposalId)}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  memory: (partition?: MemoryPartition) =>
    request<ExperienceEpisode[]>(partition ? `/memory?partition=${partition}` : "/memory"),
  knowledgeSearch: (q: string, k = 5) =>
    request<KnowledgeResult[]>(`/knowledge/search?q=${enc(q)}&k=${k}`),
  knowledgeDocs: () => request<KnowledgeDocRef[]>("/knowledge/docs"),

  // --- M4 typed API (apps/api/dto.py; generated types in ./dto) -----------------------------
  /** Components, environment adapters (simulated active; Kind/SREGym planned), flags. */
  systemView: () => request<dto.SystemView>("/system"),
  /** Every provider with its class, limits and whether this server lets a run pick it. */
  providerViews: () => request<dto.ProviderView[]>("/providers"),
  /** The committed real-model recordings (fixtures/recordings/MANIFEST.json), checked. */
  replays: () => request<dto.ReplaysView>("/replays"),
  runsPage: (filters: RunFilters = {}) =>
    requestPage<dto.RunSummary>(`/runs${queryString({ ...filters })}`),
  runView: (runId: string) => request<dto.RunDetail>(`/runs/${enc(runId)}`),
  timeline: (runId: string, opts: TimelineQuery = {}) =>
    request<dto.TimelineView>(`/runs/${enc(runId)}/timeline${queryString({ ...opts })}`),
  evidenceView: (runId: string) => request<dto.EvidenceView>(`/runs/${enc(runId)}/evidence`),
  retrievalView: (runId: string) => request<dto.RetrievalView>(`/runs/${enc(runId)}/retrieval`),
  usageView: (runId: string) => request<dto.UsageView>(`/runs/${enc(runId)}/usage`),
  evals: () => request<dto.EvalsView>("/evals"),
  evalBatch: (batchId: string) => request<dto.BatchView>(`/evals/${enc(batchId)}`),
  /** Replay a committed recording: the server takes its scenario and routing from the
   * manifest and asks for a fresh approval (operator only). */
  startReplay: (fixture: Pick<dto.FixtureView, "id" | "scenario">) =>
    request<dto.RunStarted>("/incidents/run", {
      method: "POST",
      body: JSON.stringify({
        scenario_id: fixture.scenario,
        config: { provider: "replay", replay_fixture: fixture.id, approval_mode: "manual" },
      }),
    }),

  startEvaluation: () =>
    request<EvalStartResponse>("/evals/run", { method: "POST", body: JSON.stringify({}) }),
  evalResults: () => request<EvalResults>("/evals/results"),
};

// --- live stream ----------------------------------------------------------------------

export function streamUrl(runId: string, afterSeq = 0): string {
  return `${API_BASE}/runs/${enc(runId)}/stream?after_seq=${afterSeq}`;
}

export interface StreamHandlers {
  onEvent: (event: TraceEvent) => void;
  /** Called once if the stream breaks. The source is already closed by then. */
  onError: () => void;
}

/**
 * Subscribe to GET /runs/{id}/stream (server-sent events, event name "trace", data = one
 * TraceEvent). Returns a function that closes the stream.
 *
 * EventSource reconnects by itself after an error; we close it instead and let the
 * caller fall back to polling, so the server never replays a stream twice.
 */
export function openRunStream(runId: string, afterSeq: number, handlers: StreamHandlers): () => void {
  if (typeof EventSource === "undefined") {
    handlers.onError();
    return () => undefined;
  }
  const source = new EventSource(streamUrl(runId, afterSeq));
  let closed = false;
  const close = () => {
    closed = true;
    source.close();
  };
  source.addEventListener("trace", (message) => {
    if (closed) return;
    try {
      handlers.onEvent(JSON.parse((message as MessageEvent<string>).data) as TraceEvent);
    } catch {
      // A malformed frame is skipped; the next poll or frame carries on.
    }
  });
  source.onerror = () => {
    if (closed) return;
    close();
    handlers.onError();
  };
  return close;
}
