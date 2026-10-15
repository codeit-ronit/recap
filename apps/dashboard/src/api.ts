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
 */

import type {
  Approval,
  ApprovalDecisionBody,
  ApprovalRequest,
  EvalResults,
  EvalStartResponse,
  ExperienceEpisode,
  HealthResponse,
  KnowledgeDocRef,
  KnowledgeResult,
  MemoryPartition,
  RunDetail,
  RunRequest,
  RunStartResponse,
  RunSummary,
  ScenarioInfo,
  SystemSummary,
  TraceEvent,
} from "./types";

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
  const url = API_BASE + path;
  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
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
      : (detailMessage(body) ?? `${response.status} ${response.statusText}`);
    throw new ApiError(message, response.status, path, body, offline);
  }
  return body as T;
}

const enc = encodeURIComponent;

export const api = {
  health: () => request<HealthResponse>("/health"),
  system: () => request<SystemSummary>("/system"),
  scenarios: () => request<ScenarioInfo[]>("/scenarios"),

  runs: (limit = 100) => request<RunSummary[]>(`/runs?limit=${limit}`),
  run: (runId: string) => request<RunDetail>(`/runs/${enc(runId)}`),
  events: (runId: string, afterSeq = 0) =>
    request<TraceEvent[]>(`/runs/${enc(runId)}/events?after_seq=${afterSeq}`),
  startRun: (body: RunRequest) =>
    request<RunStartResponse>("/incidents/run", { method: "POST", body: JSON.stringify(body) }),

  pendingApprovals: () => request<ApprovalRequest[]>("/approvals/pending"),
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
