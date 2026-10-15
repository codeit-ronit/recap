/**
 * Small React hooks: hash routing, polling, the live run event stream, run selection and
 * the operator sign-in state.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, openRunStream, toApiError } from "./api";
import { getToken, onTokenChange } from "./auth";
import type { ApiError } from "./api";
import type { RunSummary, SystemSummary, TraceEvent } from "./types";

// ======================================================================================
// Hash routing:  #/timeline?run=run-abc  ->  { page: "timeline", params: run=run-abc }
// ======================================================================================

export const PAGES = [
  "overview",
  "live",
  "timeline",
  "evidence",
  "memory",
  "safety",
  "evals",
  "architecture",
] as const;

export type PageId = (typeof PAGES)[number];

export interface Route {
  page: PageId;
  params: URLSearchParams;
}

export type Params = Record<string, string | null | undefined>;

export function parseHash(hash: string): Route {
  const raw = hash.replace(/^#\/?/, "");
  const q = raw.indexOf("?");
  const path = (q === -1 ? raw : raw.slice(0, q)).replace(/\/+$/, "");
  const page = (PAGES as readonly string[]).includes(path) ? (path as PageId) : "overview";
  return { page, params: new URLSearchParams(q === -1 ? "" : raw.slice(q + 1)) };
}

export function hrefFor(page: PageId, params: Params = {}): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value) query.set(key, value);
  const qs = query.toString();
  return `#/${page}${qs ? `?${qs}` : ""}`;
}

export function navigate(page: PageId, params: Params = {}, replace = false): void {
  const href = hrefFor(page, params);
  if (replace) {
    // replaceState does not fire "hashchange", so announce the change ourselves.
    window.history.replaceState(null, "", href);
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  } else {
    window.location.hash = href;
  }
}

export function paramsOf(route: Route): Params {
  return Object.fromEntries(route.params.entries());
}

export function useHashRoute(): Route {
  const [hash, setHash] = useState(() => window.location.hash);
  useEffect(() => {
    const sync = () => setHash(window.location.hash);
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);
  return useMemo(() => parseHash(hash), [hash]);
}

// ======================================================================================
// Polling
// ======================================================================================

export interface Polled<T> {
  data: T | undefined;
  error: ApiError | null;
  /** True until the first response (or error) for the current subject. */
  loading: boolean;
  /** Fetch now (and restart the polling loop). */
  refresh: () => void;
}

/** A fixed interval, null (fetch once), or a function of the latest data (e.g. poll while running). */
export type Interval<T> = number | null | ((data: T | undefined) => number | null);

/**
 * Fetch with `fetcher`, then again every `interval` ms. Requests never overlap: the next
 * one is scheduled after the previous one settles. `deps` identify the subject (e.g. the
 * run id): when they change, previous data is dropped. Data is kept across refetches, so
 * the UI never flashes back to a loading state while polling.
 */
export function usePolling<T>(
  fetcher: (() => Promise<T>) | null,
  interval: Interval<T>,
  deps: readonly unknown[],
): Polled<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(fetcher !== null);
  const [tick, setTick] = useState(0);

  const fetcherRef = useRef(fetcher);
  const intervalRef = useRef(interval);
  const dataRef = useRef<T | undefined>(undefined);
  const subjectRef = useRef<string | null>(null);
  const stoppedRef = useRef(false);
  fetcherRef.current = fetcher;
  intervalRef.current = interval;

  const subject = JSON.stringify(deps);

  useEffect(() => {
    if (subjectRef.current !== subject) {
      subjectRef.current = subject;
      dataRef.current = undefined;
      setData(undefined);
      setError(null);
    }
    if (!fetcherRef.current) {
      setLoading(false);
      return;
    }
    setLoading(dataRef.current === undefined);
    stoppedRef.current = false;
    let cancelled = false;
    let timer: number | undefined;

    const run = async () => {
      const fn = fetcherRef.current;
      if (!fn) return;
      try {
        const value = await fn();
        if (cancelled) return;
        dataRef.current = value;
        setData(value);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(toApiError(err));
      }
      if (cancelled) return;
      setLoading(false);
      const iv = intervalRef.current;
      const ms = typeof iv === "function" ? iv(dataRef.current) : iv;
      if (ms) {
        timer = window.setTimeout(() => void run(), ms);
      } else {
        stoppedRef.current = true;
      }
    };
    void run();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [subject, tick]);

  // A fixed interval that switches from "off" to "on" restarts a stopped loop.
  const numeric = typeof interval === "number" ? interval : null;
  useEffect(() => {
    if (numeric !== null && stoppedRef.current) setTick((t) => t + 1);
  }, [numeric]);

  const refresh = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, refresh };
}

// ======================================================================================
// Live run events: SSE first, polling as a fallback
// ======================================================================================

export interface RunEventsState {
  events: TraceEvent[];
  /** run.summary was received, or the run is no longer running. */
  finished: boolean;
  /**
   * How events arrive. "recorded": another process started the run, so this server has no
   * live events for it; the events recorded so far were loaded once and nothing follows.
   */
  transport: "idle" | "sse" | "polling" | "recorded";
  error: ApiError | null;
}

const IDLE: RunEventsState = { events: [], finished: false, transport: "idle", error: null };

/**
 * All trace events of a run, live. Asks GET /runs/{id} first: a run started by another
 * process (status "external") gets its recorded events once. Otherwise opens
 * GET /runs/{id}/stream (which replays stored events first); if the stream errors, falls
 * back to polling /runs/{id}/events?after_seq=. Events are de-duplicated by `seq` and kept
 * in order.
 */
export function useRunEvents(runId: string | null): RunEventsState {
  const [state, setState] = useState<RunEventsState>(IDLE);

  useEffect(() => {
    if (!runId) {
      setState(IDLE);
      return;
    }
    let cancelled = false;
    let finished = false;
    let lastSeq = 0;
    let pollTimer: number | undefined;
    let flushTimer: number | undefined;
    let closeStream: (() => void) | null = null;
    const list: TraceEvent[] = [];
    const seen = new Set<number>();

    setState(IDLE);

    // Batch React updates: a replay can deliver hundreds of frames in one burst.
    const scheduleFlush = () => {
      if (flushTimer !== undefined) return;
      flushTimer = window.setTimeout(() => {
        flushTimer = undefined;
        if (!cancelled) setState((s) => ({ ...s, events: list.slice(), finished }));
      }, 60);
    };

    const add = (incoming: TraceEvent[]) => {
      for (const event of incoming) {
        if (seen.has(event.seq)) continue;
        seen.add(event.seq);
        if (list.length && event.seq < list[list.length - 1].seq) {
          list.splice(list.findIndex((x) => x.seq > event.seq), 0, event);
        } else {
          list.push(event);
        }
        lastSeq = Math.max(lastSeq, event.seq);
        if (event.category === "run.summary") finished = true;
      }
      scheduleFlush();
    };

    const stop = () => {
      finished = true;
      closeStream?.();
      closeStream = null;
      window.clearTimeout(pollTimer);
      scheduleFlush();
    };

    const poll = async (round: number): Promise<void> => {
      if (cancelled || finished) return;
      try {
        add(await api.events(runId, lastSeq));
        // Every few polls, ask whether the run is still alive (a crashed or older run
        // may never emit run.summary).
        if (!finished && round % 3 === 2) {
          const detail = await api.run(runId);
          if (detail.status !== "running") {
            add(await api.events(runId, lastSeq));
            stop();
          }
        }
        if (!cancelled) setState((s) => (s.error ? { ...s, error: null } : s));
      } catch (err) {
        const error = toApiError(err);
        if (cancelled) return;
        setState((s) => ({ ...s, error }));
        if (error.status === 404) {
          stop();
          return;
        }
      }
      if (!cancelled && !finished) pollTimer = window.setTimeout(() => void poll(round + 1), 1000);
    };

    const follow = () => {
      if (cancelled) return;
      setState((s) => ({ ...s, transport: "sse" }));
      closeStream = openRunStream(runId, 0, {
        onEvent: (event) => {
          add([event]);
          if (finished) stop(); // close before the server hangs up, so the browser never reconnects
        },
        onError: () => {
          closeStream = null;
          if (cancelled || finished) return;
          setState((s) => ({ ...s, transport: "polling" }));
          void poll(0);
        },
      });
    };

    // Live events reach only the server process that runs the incident. For a run started
    // elsewhere, show what it has recorded instead of waiting on a stream that stays silent.
    const showRecorded = async () => {
      if (cancelled) return;
      setState((s) => ({ ...s, transport: "recorded" }));
      try {
        add(await api.events(runId));
      } catch (err) {
        if (!cancelled) setState((s) => ({ ...s, error: toApiError(err) }));
      }
    };

    void api.run(runId).then(
      (detail) => {
        if (detail.status === "external") void showRecorded();
        else follow();
      },
      // Status unknown (e.g. the API is restarting): the stream and its polling fallback retry.
      () => follow(),
    );

    return () => {
      cancelled = true;
      closeStream?.();
      window.clearTimeout(pollTimer);
      window.clearTimeout(flushTimer);
    };
  }, [runId]);

  return state;
}

// ======================================================================================
// Misc
// ======================================================================================

/** The current time, refreshed every `intervalMs` (for countdowns and "x s ago"). */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}

/** Width of an element, kept up to date with a ResizeObserver. Use the returned callback ref. */
export function useElementWidth<T extends HTMLElement>(): [(el: T | null) => void, number] {
  const [element, setElement] = useState<T | null>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!element) return;
    setWidth(element.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) setWidth(Math.floor(entry.contentRect.width));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [element]);
  return [setElement, width];
}

/** What every page receives from the App shell. */
export interface PageProps {
  route: Route;
  /** GET /system, fetched once by the shell (which implementation backs each interface). */
  system: Polled<SystemSummary>;
}

export interface RunSelection {
  runId: string | null;
  runs: Polled<RunSummary[]>;
  select: (runId: string) => void;
}

/**
 * The run a page shows: `?run=` from the hash, else the most recent run. The default is
 * written back into the hash (replace), so every page view is a shareable deep link.
 */
export function useRunSelection(route: Route): RunSelection {
  const runs = usePolling(() => api.runs(100), 5000, []);
  const requested = route.params.get("run");
  const latest = runs.data && runs.data.length ? runs.data[0].run_id : null;
  const runId = requested ?? latest;

  const routeRef = useRef(route);
  routeRef.current = route;

  useEffect(() => {
    if (!requested && latest) {
      const current = routeRef.current;
      navigate(current.page, { ...paramsOf(current), run: latest }, true);
    }
  }, [requested, latest]);

  const select = useCallback((id: string) => {
    const current = routeRef.current;
    navigate(current.page, { ...paramsOf(current), run: id });
  }, []);

  return { runId, runs, select };
}

/** The operator token of this tab (null when signed out); re-renders on sign-in and sign-out. */
export function useOperatorToken(): string | null {
  const [token, setTokenState] = useState<string | null>(getToken);
  useEffect(() => onTokenChange(setTokenState), []);
  return token;
}
