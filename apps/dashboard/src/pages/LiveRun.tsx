import { useEffect, useMemo, useRef, useState } from "react";
import { api, openRunStream, toApiError } from "../api";
import type { ApiError } from "../api";
import { ApprovalCountdown, ApprovalModal } from "../components/ApprovalCard";
import { Chip, Mono, OriginChip } from "../components/Badge";
import { Card, KeyValues, Notice, PageHeader, Segmented, StatTile, Toggle } from "../components/Card";
import { DataSourceBadge } from "../components/DataSourceBadge";
import { EventFeed } from "../components/EventFeed";
import { Icon } from "../components/Icon";
import { StateStepper } from "../components/StateStepper";
import { EmptyView, ErrorView, LoadingView, PolledView } from "../components/StateViews";
import { StateToken, StatusToken } from "../components/StatusToken";
import {
  arr,
  budgetUses,
  costLabel,
  disagreementCounts,
  disagreementLabel,
  fmtArgs,
  fmtInt,
  fmtMs,
  fmtNum,
  humanize,
  hybridReadiness,
  isTerminal,
  liveModelTokenUse,
  memoryAccessLabel,
  modelTokenUse,
  num,
  obj,
  plural,
  searchModeLabel,
  stateLabel,
  str,
  tsMs,
} from "../format";
import { PAGE_LABELS, hrefFor, navigate, useNow, useOperatorToken, usePolling } from "../hooks";
import type { Polled, PageProps } from "../hooks";
import type * as dto from "../dto";
import type {
  Approval,
  ApprovalRequest,
  EnvironmentInfo,
  ExperienceEpisode,
  ProviderInfo,
  RunConfig,
  RunConfigInput,
  RunRecord,
  RunRequest,
  RunSummaryPayload,
  ScenarioInfo,
  SystemSummary,
  TraceEvent,
  Usage,
  VerificationReport,
} from "../types";
import {
  DEFAULT_PACE,
  INSPECT_PAGES,
  NOT_RECORDED,
  REPLAY_PACES,
  STAGE_HELP,
  approvalPhase,
  fixtureRouting,
  fixtureSource,
  fmtBytes,
  initialProvider,
  integrityView,
  launchView,
  mergeBySeq,
  missingPrerequisites,
  oracleView,
  outcomeView,
  providerChoices,
  rationaleAuthor,
  readinessView,
  replayRunBody,
  resumeDelay,
  runBanner,
  runConfigBody,
  sortScenarios,
  stageTimes,
  statesOf,
  transportView,
  trustedRiskOf,
  withOverrides,
} from "../view/lab";
import type { LabTransport, RunBanner } from "../view/lab";
import { knowledgeOverlays } from "../view/retrieval";
import {
  checkDetail,
  detailLabel,
  diagnosisCheck,
  isCheckedValue,
  reportLeads,
  ruleIdsOf,
  textAuthor,
  verifiedBeforeM3,
} from "../view/verification";
import "../styles/live-m3.css";
import "../styles/incident-lab.css";

/** How runbooks are searched (RunConfig.retrieval, M3): keywords, or keywords + meaning. */
type SearchMode = NonNullable<RunConfig["retrieval"]>;

const DEFAULT_CONFIG: RunConfigInput = {
  strategy: "adaptive",
  use_rag: true,
  use_memory: true,
  approval_mode: "manual",
  tool_latency_ms: 120,
};

function lastOf(events: TraceEvent[], category: string): TraceEvent | undefined {
  for (let i = events.length - 1; i >= 0; i--) if (events[i].category === category) return events[i];
  return undefined;
}

function hasKeys(value: Record<string, unknown>): boolean {
  return Object.keys(value).length > 0;
}

/**
 * Open the sign-in form in the top bar (OperatorSignIn). Nothing happens if the shell renders
 * it differently: the prompt's text still says where it is.
 */
function openTopbarSignIn(): void {
  if (typeof document === "undefined") return;
  const button = document.querySelector<HTMLButtonElement>(".topbar .operator button");
  if (!button) return;
  button.scrollIntoView({ block: "nearest" });
  if (button.getAttribute("aria-expanded") === "true") document.getElementById("operator-token")?.focus();
  else button.click();
}

/** Signed out: what needs the operator sign-in, where to sign in, and that viewing does not. */
export function SignInPrompt({ what = "start runs and decide approvals" }: { what?: string }) {
  return (
    <div className="lab-signin" role="note">
      <Icon name="key" size={16} />
      <div className="lab-signin-text">
        <strong>Sign in to {what}.</strong> They need the operator token that the API server printed when it started (also in{" "}
        <code className="mono">data/operator-token</code>). Watching runs needs no sign-in.
      </div>
      <button type="button" className="btn btn-small" onClick={openTopbarSignIn}>
        <Icon name="key" size={13} /> Open sign-in
      </button>
    </div>
  );
}

// ======================================================================================
// Live events: SSE that resumes after the last event, then polling as a fallback
// ======================================================================================

interface LabRunEvents {
  events: TraceEvent[];
  /** run.summary arrived, or the run is no longer running. */
  finished: boolean;
  transport: LabTransport;
  /** How many times the stream was reopened after a break. */
  resumes: number;
  /** The highest event sequence number received. */
  lastSeq: number;
  error: ApiError | null;
}

const NO_EVENTS: LabRunEvents = { events: [], finished: false, transport: "idle", resumes: 0, lastSeq: 0, error: null };

/**
 * All trace events of a run, live. Asks GET /runs/{id} first: a run another process started
 * ("external") gets its recorded events once. Otherwise opens GET /runs/{id}/stream. If the
 * stream breaks while the run still runs, it is reopened with after_seq = the last event received
 * (the server replays only what was missed), up to MAX_STREAM_RESUMES times with a growing wait;
 * then GET /runs/{id}/events?after_seq= is polled every second. Events are merged by seq, so none
 * is shown twice, and React updates are batched (a replay can deliver hundreds in a burst).
 */
function useLabRunEvents(runId: string): LabRunEvents {
  const [state, setState] = useState<LabRunEvents>(NO_EVENTS);

  useEffect(() => {
    let cancelled = false;
    let finished = false;
    let lastSeq = 0;
    let attempt = 0; // reopen attempts since the last event arrived
    let resumes = 0;
    let list: readonly TraceEvent[] = [];
    let buffer: TraceEvent[] = [];
    let flushTimer: number | undefined;
    let retryTimer: number | undefined;
    let pollTimer: number | undefined;
    let closeStream: (() => void) | null = null;

    setState(NO_EVENTS);

    const flush = () => {
      flushTimer = undefined;
      if (cancelled) return;
      list = mergeBySeq(list, buffer);
      buffer = [];
      const events = list as TraceEvent[];
      setState((s) => ({ ...s, events, finished, resumes, lastSeq }));
    };
    const scheduleFlush = () => {
      if (flushTimer === undefined) flushTimer = window.setTimeout(flush, 60);
    };

    const add = (incoming: TraceEvent[]) => {
      for (const event of incoming) {
        if (!event || typeof event.seq !== "number") continue;
        buffer.push(event);
        if (event.seq > lastSeq) lastSeq = event.seq;
        if (event.category === "run.summary") finished = true;
      }
      scheduleFlush();
    };

    const stop = () => {
      finished = true;
      closeStream?.();
      closeStream = null;
      window.clearTimeout(pollTimer);
      window.clearTimeout(retryTimer);
      scheduleFlush();
    };

    const poll = async (round: number): Promise<void> => {
      if (cancelled || finished) return;
      try {
        add(await api.events(runId, lastSeq));
        // Every few polls, ask whether the run is still alive (a crashed run may never send run.summary).
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

    const startPolling = () => {
      if (cancelled || finished) return;
      setState((s) => ({ ...s, transport: "polling" }));
      void poll(0);
    };

    const follow = () => {
      if (cancelled || finished) return;
      setState((s) => ({ ...s, transport: "sse", resumes }));
      closeStream = openRunStream(runId, lastSeq, {
        onEvent: (event) => {
          attempt = 0;
          add([event]);
          if (finished) stop(); // close before the server hangs up, so the browser never reconnects
        },
        onError: () => {
          closeStream = null;
          if (cancelled || finished) return;
          void recover();
        },
      });
    };

    // The stream broke. A run still running gets the stream back, resuming after the last event
    // received; a run that is not running any more gets its remaining recorded events.
    const recover = async () => {
      try {
        const detail = await api.run(runId);
        if (cancelled || finished) return;
        if (detail.status !== "running") {
          if (detail.status === "external") setState((s) => ({ ...s, transport: "recorded" }));
          add(await api.events(runId, lastSeq));
          stop();
          return;
        }
      } catch {
        // Status unknown (the API may be restarting): try the stream again below.
      }
      if (cancelled || finished) return;
      const delay = resumeDelay(attempt);
      if (delay === null) {
        startPolling();
        return;
      }
      attempt += 1;
      setState((s) => ({ ...s, transport: "reconnecting" }));
      retryTimer = window.setTimeout(() => {
        resumes += 1;
        follow();
      }, delay);
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
      // Status unknown (e.g. the API is restarting): the stream and its fallbacks retry.
      () => follow(),
    );

    return () => {
      cancelled = true;
      closeStream?.();
      window.clearTimeout(pollTimer);
      window.clearTimeout(retryTimer);
      window.clearTimeout(flushTimer);
    };
  }, [runId]);

  return state;
}

// ======================================================================================
// Replay a recorded incident (GET /replays)
// ======================================================================================

function ReplayCard({
  fixture,
  environment,
  signedIn,
  starting,
  error,
  currentPrompt,
  onReplay,
}: {
  fixture: dto.FixtureView;
  environment: EnvironmentInfo | undefined;
  signedIn: boolean;
  /** The fixture being started right now, if any. */
  starting: string | null;
  error: ApiError | null;
  currentPrompt: string | null;
  onReplay: (fixture: dto.FixtureView) => void;
}) {
  const integrity = integrityView(fixture.integrity, fixture.sha256);
  const launch = launchView(fixture);
  const titleId = `replay-${fixture.id}`;
  const busy = starting === fixture.id;
  return (
    <article className={launch.launchable ? "lab-replay" : "lab-replay blocked"} aria-labelledby={titleId}>
      <div className="lab-replay-head">
        <DataSourceBadge provider={fixtureSource(fixture)} environment={environment} size="sm" label="What a replay of this recording is" />
        {fixture.tags.length ? (
          <span className="chip-row">
            {fixture.tags.map((tag) => (
              <Chip key={tag}>{tag}</Chip>
            ))}
          </span>
        ) : null}
      </div>
      <h3 id={titleId} className="lab-replay-title">
        {fixture.title || fixture.id}
      </h3>
      {fixture.description ? <p className="lab-replay-desc">{fixture.description}</p> : null}
      <div className="lab-replay-action">
        {launch.launchable ? (
          <>
            <button
              type="button"
              className="btn btn-primary btn-large"
              disabled={!signedIn || starting !== null}
              onClick={() => onReplay(fixture)}
            >
              <Icon name="play" size={15} /> {busy ? "Starting the replay…" : "Replay this incident"}
            </button>
            <StatusToken tone={integrity.tone} icon={integrity.icon} label={`Recording ${integrity.label.toLowerCase()}`} title={integrity.detail} size="sm" />
            {signedIn ? null : <span className="muted small">Sign in to start it.</span>}
          </>
        ) : (
          <div className="lab-blocked">
            <span className="chip-row">
              <StatusToken tone="bad" label="Cannot be replayed on this server" />
              <StatusToken tone={integrity.tone} icon={integrity.icon} label={`Recording ${integrity.label.toLowerCase()}`} title={integrity.detail} size="sm" />
            </span>
            <ul className="lab-reasons">
              {launch.reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
      {error ? <ErrorView error={error} title="The replay did not start" /> : null}
      <KeyValues
        className="lab-facts"
        rows={[
          [
            "Scenario",
            <span className="chip-row">
              <Mono>{fixture.scenario}</Mono>
              {fixture.scenario_available ? null : <StatusToken tone="bad" size="sm" label="not on this server" />}
            </span>,
          ],
          [
            "Model",
            <span className="chip-row">
              <span>
                <Mono>{fixture.model || NOT_RECORDED}</Mono>
                {fixture.provider ? <span className="muted"> via {fixture.provider}</span> : null}
              </span>
              <Mono>{fixture.prompt_version || NOT_RECORDED}</Mono>
              {fixture.prompt_version_matches ? (
                <StatusToken tone="ok" size="sm" label="prompts match this server" />
              ) : (
                <StatusToken tone="bad" size="sm" label={currentPrompt ? `this server renders ${currentPrompt}` : "prompts do not match this server"} />
              )}
            </span>,
          ],
          ["Agents", fixture.agents || NOT_RECORDED],
          ["Routing", fixtureRouting(fixture)],
          [
            "Original approval",
            <span className="cell-stack">
              <span>{fixture.approved_by || NOT_RECORDED}</span>
              <span className="muted small">The replay asks a person again: nothing is approved for you.</span>
            </span>,
          ],
          [
            "Recording",
            <span className="cell-stack">
              <span>
                <Mono>{fixture.recording_id || NOT_RECORDED}</Mono>
                <span className="muted"> · {fmtBytes(fixture.bytes)}</span>
              </span>
              <span className="muted small">
                {integrity.label === NOT_RECORDED ? integrity.detail : `${integrity.label}: ${integrity.detail.charAt(0).toLowerCase()}${integrity.detail.slice(1)}`}
              </span>
            </span>,
          ],
          ...(fixture.exported_run
            ? ([["Original run", <span><Mono>{fixture.exported_run}</Mono><span className="muted"> (export in this repository)</span></span>]] as [string, JSX.Element][])
            : []),
        ]}
      />
    </article>
  );
}

/**
 * The replay launcher: every committed recording of a real model run (GET /replays), with its
 * model, prompts, agents, how the original fix was approved, the recording's integrity and
 * whether this server can replay it; a pace choice; one click starts the replay.
 */
export function ReplayLauncher({
  replays,
  environment,
  signedIn,
  pace,
  onPace,
  starting,
  startError,
  onReplay,
}: {
  replays: Pick<Polled<dto.ReplaysView>, "data" | "error" | "loading" | "refresh">;
  environment: EnvironmentInfo | undefined;
  signedIn: boolean;
  pace: number;
  onPace: (pace: number) => void;
  starting: string | null;
  startError: { fixtureId: string; error: ApiError } | null;
  onReplay: (fixture: dto.FixtureView) => void;
}) {
  const chosen = REPLAY_PACES.find((p) => p.value === pace) ?? REPLAY_PACES[0];
  return (
    <Card title="Replay a recorded incident" subtitle="Real model runs recorded earlier, played back offline against the simulated shop">
      <p className="lab-intro">
        A replay feeds RECAP the answers a real model gave in an earlier run, so the whole multi-agent investigation can be watched
        without a model, GPU or network. The simulated shop is really changed, and the fix waits for a person's approval again.
      </p>
      <div className="field lab-pace">
        <span className="field-label" id="replay-pace-label">
          Pace
        </span>
        <Segmented<string>
          label="Replay pace"
          value={String(chosen.value)}
          onChange={(v) => onPace(Number(v))}
          options={REPLAY_PACES.map((p) => ({ value: String(p.value), label: p.label }))}
        />
        <span className="field-hint">{chosen.hint}</span>
      </div>
      <PolledView
        polled={replays}
        loadingLabel="Loading the recordings…"
        errorTitle="Could not load the recordings (GET /replays)"
        emptyTitle="No recordings to replay"
        emptyIcon="rewind"
        empty={(view) =>
          view.fixtures.length
            ? null
            : view.manifest_error
              ? `The recordings manifest cannot be read: ${view.manifest_error}`
              : "This server's recordings manifest (fixtures/recordings/MANIFEST.json) lists no recordings."
        }
      >
        {(view) => (
          <>
            {view.manifest_error ? (
              <Notice tone="warn" icon="alert" title="The recordings manifest has a problem">
                {view.manifest_error}
              </Notice>
            ) : null}
            <div className="lab-replays">
              {view.fixtures.map((fixture) => (
                <ReplayCard
                  key={fixture.id}
                  fixture={fixture}
                  environment={environment}
                  signedIn={signedIn}
                  starting={starting}
                  error={startError?.fixtureId === fixture.id ? startError.error : null}
                  currentPrompt={view.current_prompt_version || null}
                  onReplay={onReplay}
                />
              ))}
            </div>
          </>
        )}
      </PolledView>
    </Card>
  );
}

// ======================================================================================
// Run a scenario: scenario, backend, settings, safety
// ======================================================================================

function ScenarioCard({
  scenario,
  selected,
  onSelect,
  showTruth,
  missing,
}: {
  scenario: ScenarioInfo;
  selected: boolean;
  onSelect: () => void;
  showTruth: boolean;
  missing: string[];
}) {
  const overrides = Object.entries(obj(scenario.run_overrides));
  return (
    <button type="button" className={selected ? "scenario selected" : "scenario"} aria-pressed={selected} onClick={onSelect}>
      <span className="scenario-top">
        <span className="scenario-title">{scenario.title}</span>
        <Chip tone={scenario.partition === "demo" ? "accent" : "neutral"}>{scenario.partition}</Chip>
      </span>
      <code className="mono muted">{scenario.id}</code>
      {scenario.description ? <span className="scenario-desc">{scenario.description}</span> : null}
      {scenario.tags.length ? (
        <span className="chip-row">
          {scenario.tags.map((t) => (
            <Chip key={t}>{t}</Chip>
          ))}
        </span>
      ) : null}
      {overrides.length ? (
        <span className="scenario-note">
          <Icon name="info" size={13} /> scenario sets {overrides.map(([k, v]) => `${k}=${str(v)}`).join(", ")}
        </span>
      ) : null}
      {scenario.requires_memory_of.length ? (
        <span className={missing.length ? "scenario-note warn" : "scenario-note ok"}>
          <Icon name="memory" size={13} />
          {missing.length
            ? `needs a verified episode from ${missing.join(", ")}: run it first (memory on)`
            : `prerequisite episode from ${scenario.requires_memory_of.join(", ")} is in memory`}
        </span>
      ) : null}
      {showTruth ? (
        <span className="truth">
          <span className="truth-label">ground truth · hidden from agents</span>
          <span>
            family <code className="mono">{scenario.family}</code>
          </span>
          {Object.entries(obj(scenario.expected)).map(([k, v]) => (
            <span key={k}>
              {k} <code className="mono">{str(v)}</code>
            </span>
          ))}
        </span>
      ) : null}
    </button>
  );
}

/**
 * Which decision backend a scenario run uses: only what the server lets a run pick
 * (GET /providers `selectable`), the default preselected; every other provider is listed with
 * the server's reason it cannot be chosen here.
 */
export function BackendChoice({
  providers,
  value,
  onChange,
}: {
  providers: Pick<Polled<dto.ProviderView[]>, "data" | "error" | "loading" | "refresh">;
  value: string | null;
  onChange: (name: string) => void;
}) {
  return (
    <PolledView
      polled={providers}
      loadingLabel="Loading the backends…"
      errorTitle="Could not load the backends (GET /providers)"
      emptyTitle="No backends reported"
      empty={(list) => (list.length ? null : "The server reports no providers (configs/providers.yaml).")}
    >
      {(list) => {
        const choices = providerChoices(list);
        const chosen = choices.choosable.find((p) => p.name === value) ?? null;
        const readiness = chosen ? readinessView(chosen.status, chosen.detail) : null;
        return (
          <div className="lab-backends">
            {choices.choosable.length ? (
              <div className="lab-provider-list" role="radiogroup" aria-label="Decision backend">
                {choices.choosable.map((p) => {
                  const ready = readinessView(p.status, p.detail);
                  const checked = p.name === value;
                  const id = `backend-${p.name.replace(/[^A-Za-z0-9_-]/g, "-")}`;
                  // The radio is named by the backend; what it is and its limits describe it.
                  return (
                    <div key={p.name} className={checked ? "lab-provider selected" : "lab-provider"} onClick={() => onChange(p.name)}>
                      <input
                        type="radio"
                        id={id}
                        name="decision-backend"
                        value={p.name}
                        checked={checked}
                        onChange={() => onChange(p.name)}
                        aria-describedby={`${id}-about`}
                      />
                      <div className="lab-provider-body">
                        <label htmlFor={id} className="lab-provider-top">
                          <Mono>{p.name}</Mono>
                          {p.default ? <Chip tone="accent">server default</Chip> : null}
                        </label>
                        <div id={`${id}-about`} className="lab-provider-about">
                          <span className="chip-row">
                            <StatusToken tone={ready.tone} icon={ready.icon} label={ready.label} title={ready.detail || undefined} size="sm" />
                            <DataSourceBadge provider={p} size="sm" label={`${p.name} decision source`} />
                          </span>
                          {p.description ? <span className="lab-provider-desc">{p.description}</span> : null}
                          {p.limitations.length ? (
                            <ul className="lab-provider-notes">
                              {p.limitations.map((note) => (
                                <li key={note}>{note}</li>
                              ))}
                            </ul>
                          ) : null}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <EmptyView title="No backend can be chosen for a run here" reason="GET /providers marks none of them selectable for one run." />
            )}
            {chosen && readiness && chosen.status !== "ready" ? (
              <Notice tone="warn" icon="alert" title={`${chosen.name}: ${readiness.label.toLowerCase()}`}>
                {chosen.detail || "The server reports it is not ready."} A run started with it now would likely fail.
              </Notice>
            ) : null}
            {choices.replay ? (
              <p className="field-hint">
                <Mono>{choices.replay.name}</Mono> needs a recording: start it from Replay a recorded incident above.
              </p>
            ) : null}
            {choices.unavailable.length ? (
              <div className="lab-unavailable">
                <span className="field-label">Not available for a run started here</span>
                <ul>
                  {choices.unavailable.map(({ provider, reason }) => {
                    const ready = readinessView(provider.status, provider.detail);
                    return (
                      <li key={provider.name}>
                        <span className="chip-row">
                          <Mono>{provider.name}</Mono>
                          <DataSourceBadge provider={provider} size="sm" label={`${provider.name} decision source`} />
                          <StatusToken tone={ready.tone} icon={ready.icon} label={ready.label} title={ready.detail || undefined} size="sm" />
                        </span>
                        <span className="muted small">{reason}</span>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ) : null}
          </div>
        );
      }}
    </PolledView>
  );
}

/**
 * Runbook search (RunConfig.retrieval, M3): keywords (BM25) or keywords + meaning (hybrid).
 * Sent only once the operator picks one; until then the server's default (GET /system) applies.
 */
function SearchField({
  config,
  onChange,
  system,
}: {
  config: RunConfigInput;
  onChange: (config: RunConfigInput) => void;
  system: SystemSummary | undefined;
}) {
  const serverDefault = system?.retrieval_mode; // reported by servers since M3
  const chosen = config.retrieval;
  const shown = chosen ?? serverDefault;
  const readiness = hybridReadiness(system);
  const useDefault = () => {
    const next = { ...config };
    delete next.retrieval;
    onChange(next);
  };
  return (
    <div className="field">
      <span className="field-label">Runbook search</span>
      <Segmented<SearchMode | "">
        label="Runbook search"
        value={shown ?? ""}
        onChange={(v) => onChange({ ...config, retrieval: v || undefined })}
        options={[
          { value: "bm25", label: "Keywords (BM25)" },
          { value: "hybrid", label: "Keywords + meaning (hybrid)" },
        ]}
      />
      <span className="field-hint">
        {shown === "hybrid"
          ? "Adds a meaning ranking from nomic-embed-text on local Ollama to BM25. Needs Ollama running with nomic-embed-text and the stored runbook vectors (scripts/build_vector_index.py); without them the run fails at its first search."
          : shown === "bm25"
            ? "Keyword ranking only: offline and deterministic, no model needed."
            : "GET /system has not reported the server's default search."}
      </span>
      {shown === "hybrid" && readiness !== "unknown" ? (
        <span className="field-hint">
          {readiness === "ready" ? (
            `GET /system: stored vectors cover every runbook chunk${system?.embedder ? ` (${system.embedder})` : ""}.`
          ) : (
            <span className="text-warn">GET /system: the stored runbook vectors are missing or incomplete, so a hybrid run would fail now.</span>
          )}
        </span>
      ) : null}
      <span className="field-hint">
        {chosen
          ? `Sent with the run as retrieval: ${chosen}.`
          : `Not chosen: ${serverDefault ? `the server's default (${searchModeLabel(serverDefault)})` : "the server's default"} applies and nothing is sent.`}
        {config.use_rag ? "" : " RAG is off, so no runbook search runs."}
      </span>
      {chosen ? (
        <button type="button" className="btn btn-ghost btn-small" onClick={useDefault}>
          Use the server default
        </button>
      ) : null}
    </div>
  );
}

function ConfigForm({
  config,
  onChange,
  system,
}: {
  config: RunConfigInput;
  onChange: (config: RunConfigInput) => void;
  system: SystemSummary | undefined;
}) {
  const set = <K extends keyof RunConfigInput>(key: K, value: RunConfigInput[K]) => onChange({ ...config, [key]: value });
  const hints: Record<RunConfigInput["strategy"], string> = {
    adaptive: "The router picks one investigator or parallel specialists from observable signals and the remaining budget.",
    single: "Always one generalist investigator.",
    parallel: "Telemetry and infrastructure specialists run concurrently, then the verifier merges their evidence.",
  };
  return (
    <div className="config-grid">
      <div className="field">
        <span className="field-label">Approval</span>
        <Segmented
          label="Approval mode"
          vertical
          value={config.approval_mode}
          onChange={(v) => set("approval_mode", v)}
          options={[
            { value: "manual", label: "Manual: a person approves here" },
            { value: "auto", label: "Auto: simulation auto-approver" },
          ]}
        />
        <span className="field-hint">
          {config.approval_mode === "manual"
            ? "A person approves the exact action (action, target and parameters) in a dialog before anything changes; the run waits at Awaiting approval."
            : "The simulation's auto-approver approves instead of a person. The server allows it only against a simulated environment: never for real systems."}
        </span>
      </div>
      <div className="field">
        <span className="field-label">Strategy</span>
        <Segmented
          label="Strategy"
          value={config.strategy}
          onChange={(v) => set("strategy", v)}
          options={[
            { value: "adaptive", label: "Adaptive" },
            { value: "single", label: "Single" },
            { value: "parallel", label: "Parallel" },
          ]}
        />
        <span className="field-hint">{hints[config.strategy]}</span>
      </div>
      <div className="field">
        <span className="field-label">Context</span>
        <Toggle checked={config.use_rag} onChange={(v) => set("use_rag", v)} label="Runbook retrieval (RAG)" hint="runbooks in fixtures/knowledge, cited by chunk id" />
        <Toggle checked={config.use_memory} onChange={(v) => set("use_memory", v)} label="Experience memory" hint="past episodes, checked against live evidence" />
      </div>
      <SearchField config={config} onChange={onChange} system={system} />
      <div className="field">
        <label className="field-label" htmlFor="latency">
          Simulated tool latency (ms)
        </label>
        <input
          id="latency"
          className="input"
          type="number"
          min={0}
          max={5000}
          step={10}
          value={config.tool_latency_ms ?? ""}
          onChange={(e) => {
            const value = Number.parseInt(e.target.value, 10);
            set("tool_latency_ms", Number.isFinite(value) ? Math.max(0, Math.min(5000, value)) : null);
          }}
        />
        <span className="field-hint">Higher latency makes the parallel overlap easier to see on the timeline.</span>
      </div>
    </div>
  );
}

/** What keeps every run started here safe, in plain words (the environment from GET /system). */
export function SafetyExplainer({ environment }: { environment: Pick<EnvironmentInfo, "is_simulated" | "kind" | "name" | "description"> | undefined }) {
  const where = !environment
    ? { tone: "neutral" as const, title: "Target not reported yet.", text: "Waiting for GET /system to report which environment runs act on." }
    : environment.is_simulated
      ? {
          tone: "warn" as const,
          title: "Simulated target.",
          text: `${environment.name ? `${environment.name}: ` : ""}${environment.description || "RECAP's local simulation."} No real system is changed.`,
        }
      : {
          tone: "bad" as const,
          title: `Live target (${environment.kind}).`,
          text: "The server reports a live environment adapter: an approved action changes a real system.",
        };
  return (
    <ul className="lab-safety" aria-label="What keeps a run safe">
      <li className={`tone-${where.tone}`}>
        <Icon name={environment && !environment.is_simulated ? "alert" : "flask"} size={16} />
        <span>
          <strong>{where.title}</strong> {where.text}
        </span>
      </li>
      <li className="tone-neutral">
        <Icon name="shield" size={16} />
        <span>
          <strong>Read-only agents.</strong> Investigators can only call read tools; every write goes through the safety gateway's policy.
        </span>
      </li>
      <li className="tone-neutral">
        <Icon name="key" size={16} />
        <span>
          <strong>Exact approval.</strong> A write runs only with an approval bound by digest to one action, target and parameters; it is
          used once and expires.
        </span>
      </li>
      <li className="tone-neutral">
        <Icon name="lock" size={16} />
        <span>
          <strong>Safety stays on.</strong> The API refuses a run with safety disabled, and the auto-approver works only against a simulated
          environment.
        </span>
      </li>
    </ul>
  );
}

// ======================================================================================
// The run: header, approval, outcome, details
// ======================================================================================

/** The run's prominent data-source label and what it means. */
export function RunBannerView({
  banner,
  replay,
}: {
  banner: RunBanner;
  /** For a replay: which recording it answers from (GET /runs/{id} summary.replay). */
  replay?: dto.ReplayProvenance | null;
}) {
  return (
    <div className={`lab-banner tone-${banner.tone}`} role="note" aria-label="Where this run's decisions come from">
      <Icon name={banner.icon} size={20} />
      <div className="lab-banner-text">
        <div className="lab-banner-label">{banner.label}</div>
        <div className="lab-banner-why">{banner.explanation}</div>
        {replay ? (
          <div className="lab-banner-why">
            Recording <Mono>{replay.recording_id ?? NOT_RECORDED}</Mono>
            {replay.fixture_title ? <> of “{replay.fixture_title}”</> : null}
            {replay.recording_matches_fixture === true ? " (matches its committed fixture)" : replay.recording_matches_fixture === false ? " (does NOT match its committed fixture)" : ""}
            {replay.originally_approved_by ? <>. The original fix was approved by {replay.originally_approved_by}; this replay asks again.</> : "."}
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** Links to the pages that show more of this run. */
export function InspectLinks({ runId }: { runId: string }) {
  return (
    <nav className="lab-links" aria-label="Inspect this run">
      <h3 className="lab-links-title">Inspect this run</h3>
      <ul>
        {INSPECT_PAGES.map(({ page, hint }) => (
          <li key={page}>
            <a href={hrefFor(page, { run: runId })}>
              <span className="lab-link-name">
                {PAGE_LABELS[page]}
                <Icon name="chevronRight" size={13} />
              </span>
              <span className="lab-link-hint">{hint}</span>
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}

/** How the run ended (summary.outcome) with the independent oracle's verdict, and where to look next. */
export function OutcomeCard({
  runId,
  summary,
  record,
  loading,
}: {
  runId: string;
  summary: dto.RunSummary | null | undefined;
  record: RunRecord | null | undefined;
  /** Still waiting for the final checkpoint. */
  loading?: boolean;
}) {
  const outcome = outcomeView(summary?.outcome);
  const oracle = oracleView(summary?.oracle ?? null);
  const approvals = record?.approvals ?? [];
  return (
    <Card title="Outcome" subtitle="From the run's record and the independent oracle's verdict" tone={summary ? outcome.tone : undefined}>
      {!summary ? (
        loading ? (
          <LoadingView label="Loading the final checkpoint…" />
        ) : (
          <EmptyView title="No outcome recorded" reason="GET /runs/{id} returned no summary for this run." />
        )
      ) : (
        <>
          <div className="lab-outcome">
            <StatusToken tone={outcome.tone} icon={outcome.icon} label={outcome.label} className="lab-outcome-token" />
            <div className="lab-outcome-text">
              <p className="lab-outcome-sentence">{outcome.sentence}</p>
              {summary.outcome_note ? <p className="muted small">RECAP's note: {summary.outcome_note}</p> : null}
              <div className="lab-outcome-source">
                <span className="muted small">Data source</span>
                <DataSourceBadge
                  provider={{
                    origin: summary.provider_class ?? undefined,
                    kind: summary.provider_kind ?? undefined,
                    model: summary.model,
                    replay_of: summary.replay
                      ? { model: summary.replay.model, original_provider: summary.replay.original_provider, recording_id: summary.replay.recording_id }
                      : null,
                  }}
                  environment={
                    typeof summary.environment_simulated === "boolean"
                      ? { is_simulated: summary.environment_simulated, kind: summary.environment_kind ?? undefined }
                      : null
                  }
                  size="sm"
                  label="This run's data source"
                />
              </div>
            </div>
          </div>
          <div className="lab-outcome-grid">
            <div className="lab-fact">
              <span className="lab-fact-label">Oracle verdict</span>
              <StatusToken tone={oracle.tone} icon={oracle.icon} label={oracle.label} size="sm" />
              {summary.oracle ? (
                <span className="small">
                  {oracle.before} before → {oracle.after} after · <Mono>{oracle.detail}</Mono>
                  {oracle.services.length ? <span className="muted"> · checked {oracle.services.join(", ")}</span> : null}
                </span>
              ) : (
                <span className="muted small">{oracle.detail}</span>
              )}
            </div>
            <div className="lab-fact">
              <span className="lab-fact-label">Approval</span>
              {approvals.length ? (
                approvals.map((a: Approval) => (
                  <span key={a.approval_id} className="chip-row">
                    <StatusToken tone={a.decision === "approved" ? "ok" : "warn"} icon={a.decision === "approved" ? "check" : "stop"} label={a.decision} size="sm" />
                    <span className="small">by {a.actor}</span>
                    <span className="muted small">{a.consumed_at ? "used once" : "not used"}</span>
                  </span>
                ))
              ) : (
                <span className="muted small">{record ? "No approval was decided in this run." : NOT_RECORDED}</span>
              )}
            </div>
            <div className="lab-fact">
              <span className="lab-fact-label">Wall time</span>
              <span className="small">
                {summary.wall_seconds !== null ? `${fmtNum(summary.wall_seconds, 1)} s` : NOT_RECORDED}
                <span className="muted"> · from start to end, including any wait for a person</span>
              </span>
            </div>
          </div>
          {summary.error ? (
            <Notice tone="bad" icon="x" title="Run error">
              {summary.error}
            </Notice>
          ) : null}
          <InspectLinks runId={runId} />
        </>
      )}
    </Card>
  );
}

/** How the run searched the runbooks (RunRecord.retrieval, M3), and any extra runbook folders. */
function SearchFacts({ record }: { record: RunRecord }) {
  if (!record.config.use_rag) return <span className="muted">RAG off: no runbook search ran</span>;
  const info = record.retrieval;
  if (!info) return <span className="muted">{NOT_RECORDED}</span>;
  const requests = num(info.embedding_requests);
  const tokens = num(info.embedding_input_tokens);
  const replayed = num(info.replayed_requests);
  const searches = num(info.agent_searches);
  // RECAP_KNOWLEDGE_OVERLAYS: extra runbook folders (e.g. a red-team overlay) were indexed.
  const overlays = arr<string>(obj(record.versions).knowledge_overlays).filter((o) => typeof o === "string");
  return (
    <span className="cell-stack">
      <span className="chip-row">
        <Chip tone={info.mode === "hybrid" ? "info" : "neutral"}>{searchModeLabel(info.mode, true)}</Chip>
        <span className="muted">{record.config.retrieval ? "chosen for this run" : "server default"}</span>
      </span>
      {info.mode === "hybrid" ? (
        <span className="muted small">
          embedder <Mono>{info.embedder ?? NOT_RECORDED}</Mono>
          {requests !== null ? ` · ${plural(requests, "embedding request")}` : ""}
          {tokens !== null ? ` · ${fmtInt(tokens)} input tokens reported by the server` : ""}
          {replayed ? ` · ${fmtInt(replayed)} replayed from the recording` : ""}
        </span>
      ) : null}
      <span className="muted small">
        {searches !== null ? `${plural(searches, "search", "searches")} by the agents (search_runbooks)` : "agent searches not recorded"}
      </span>
      {overlays.length ? (
        <span className="chip-row">
          <Chip tone="warn">
            <Icon name="alert" size={12} /> extra runbook folders indexed
          </Chip>
          {overlays.map((o) => (
            <Mono key={o}>{o}</Mono>
          ))}
        </span>
      ) : null}
    </span>
  );
}

/**
 * The final verification's disagreements between agents (M3): what conflicted, and the rule
 * that settled it; and RECAP's leads for a next round. A run verified before M3 recorded none
 * (the API serves the missing list as []): "not recorded".
 */
function DisagreementFacts({ report, findings }: { report: VerificationReport | null; findings: RunRecord["findings"] }) {
  if (!report) return <span className="muted">no verification ran</span>;
  const counts = verifiedBeforeM3(report, findings) ? null : disagreementCounts(report.disagreements);
  if (!counts) return <span className="muted">{NOT_RECORDED}</span>;
  const leads = reportLeads(report);
  return (
    <span className="cell-stack">
      <span>
        {disagreementLabel(counts)} <span className="muted">(final verification)</span>
      </span>
      {arr<NonNullable<VerificationReport["disagreements"]>[number]>(report.disagreements).map((d, i) => {
        const claims = arr<string>(d.claims);
        const agents = arr<string>(d.agents);
        const rules = ruleIdsOf(d.rule_id);
        return (
          <span key={i} className="live-conflict">
            <span className="chip-row">
              <Chip tone={d.resolved ? "ok" : "warn"}>{d.resolved ? (rules.length ? "settled by" : "settled") : "open"}</Chip>
              {d.resolved
                ? rules.map((id) => (
                    <Chip key={id} mono>
                      {id}
                    </Chip>
                  ))
                : null}
              <span className="muted small">
                {humanize(str(d.kind, "disagreement"))}
                {agents.length ? ` · ${agents.join(", ")}` : ""}
              </span>
            </span>
            {claims.length ? <span className="small">{claims.join(" vs ")}</span> : null}
            {d.resolution ? <span className="muted small">{d.resolution}</span> : null}
          </span>
        );
      })}
      {leads.length ? (
        <span className="live-conflict">
          <span className="live-label">RECAP's leads for the next round (its own text)</span>
          {leads.map((lead) => (
            <span key={lead} className="small">
              {lead}
            </span>
          ))}
        </span>
      ) : null}
    </span>
  );
}

/**
 * The diagnosis: RECAP's checked facts (mechanism, service, and the release or dependency the
 * merger keyed it by), then the claim sentence and confidence note quoted as the agent's own
 * untrusted words. The verifier never reads the sentence: only its mechanism, service and detail.
 */
function DiagnosisFacts({ report, provider }: { report: VerificationReport; provider: Partial<ProviderInfo> | null | undefined }) {
  const best = report.best;
  if (!best) return <span className="muted">no supported diagnosis</span>;
  const author = textAuthor(provider);
  const check = diagnosisCheck(report);
  const detail = check ? checkDetail(check) : "";
  return (
    <span className="cell-stack">
      <span className="chip-row">
        <Mono>
          {best.mechanism}@{best.target_service ?? "?"}
        </Mono>
        {detail && isCheckedValue(best.mechanism, detail) ? <span className="muted">{detailLabel(best.mechanism, detail)}</span> : null}
      </span>
      {detail && !isCheckedValue(best.mechanism, detail) ? (
        <span className="live-untrusted">
          <span className="live-untrusted-label">judged as · untrusted {author.who} text</span>
          <q className="live-quote">{detail}</q>
        </span>
      ) : null}
      {best.claim ? (
        <span className="live-untrusted">
          <span className="live-untrusted-label">claim · untrusted {author.who} text</span>
          <q className="live-quote">{best.claim}</q>
        </span>
      ) : null}
      {best.confidence_note ? (
        <span className="live-untrusted">
          <span className="live-untrusted-label">confidence note · {author.words}, not a score</span>
          <q className="live-quote">{best.confidence_note}</q>
        </span>
      ) : null}
    </span>
  );
}

/** The budget as the run's run.summary recorded it: each counter against its limit, real counts only. */
function BudgetFacts({
  budget,
  usage,
  provider,
}: {
  budget: unknown;
  usage: Usage;
  provider: Partial<ProviderInfo> | null | undefined;
}) {
  const uses = budgetUses(budget).filter((b) => b.key !== "model_tokens");
  if (!uses.length) return <span className="muted">{NOT_RECORDED}</span>;
  const model = modelTokenUse(budget, usage, provider);
  return (
    <span className="cell-stack">
      <span>{uses.map((b) => b.text).join(" · ")}</span>
      <span>
        model tokens <strong className={model.tone === "warn" ? "text-warn" : undefined}>{model.value}</strong>
        {model.note ? <span className="muted"> · {model.note}</span> : null}
      </span>
    </span>
  );
}

/** Everything else the final checkpoint recorded: diagnosis, disagreements, provider, search, memory, usage. */
function RunDetails({
  record,
  summary,
  system,
  memorySkip,
}: {
  record: RunRecord;
  summary: RunSummaryPayload | undefined;
  system: SystemSummary | undefined;
  /** Why the run wrote no episode (its last memory.skip reason, RECAP's own text). */
  memorySkip: string;
}) {
  // Where the decisions came from: the run's own record (runs before M2: the live system's).
  const provider = record.provider_info ?? (system && record.provider === system.provider.name ? system.provider : null);
  const u = record.usage;
  const reported = u.actual_input_tokens !== null || u.actual_output_tokens !== null;
  return (
    <Card title="Run details" subtitle="From the final checkpoint (GET /runs/{id})">
      <KeyValues
        rows={[
          [
            "Diagnosis",
            record.verification ? <DiagnosisFacts report={record.verification} provider={provider} /> : <span className="muted">no supported diagnosis</span>,
          ],
          ["Disagreements", <DisagreementFacts report={record.verification} findings={record.findings} />],
          [
            "Decisions by",
            <span className="chip-row">
              <OriginChip provider={provider} />
              <Mono>{provider?.name ?? record.provider}</Mono>
              {provider?.mode ? <span className="muted">{provider.mode} mode</span> : null}
              {provider?.prompt_version ? <span className="muted">· {provider.prompt_version}</span> : null}
            </span>,
          ],
          ["Runbook search", <SearchFacts record={record} />],
          ["Memory partitions", <span>{memoryAccessLabel(record.config, !!record.episode_id)}</span>],
          [
            "Episode written",
            record.episode_id ? <Mono>{record.episode_id}</Mono> : <span className="muted">not written{memorySkip ? `: ${memorySkip}` : ""}</span>,
          ],
          [
            "Usage",
            <span>
              {fmtInt(u.steps)} step{u.steps === 1 ? "" : "s"} · {fmtInt(u.tool_calls)} tool call{u.tool_calls === 1 ? "" : "s"} ·{" "}
              {fmtInt(u.estimated_tokens)} tokens <Chip>estimated</Chip>
              {u.model_requests ? (
                <>
                  {" "}
                  · {fmtInt(u.model_requests)} model request{u.model_requests === 1 ? "" : "s"}
                  {reported ? (
                    <>
                      {" "}
                      · in {fmtInt(u.actual_input_tokens)} / out {fmtInt(u.actual_output_tokens)} tokens <Chip tone="ok">reported by the model server</Chip>
                    </>
                  ) : null}
                </>
              ) : null}
            </span>,
          ],
          ["Budget", <BudgetFacts budget={summary?.budget} usage={u} provider={provider} />],
          ["Cost", costLabel(summary?.cost, provider)],
          [
            "Run config",
            <Mono>
              {fmtArgs({
                strategy: record.config.strategy,
                use_rag: record.config.use_rag,
                retrieval: record.config.retrieval,
                use_memory: record.config.use_memory,
                approval_mode: record.config.approval_mode,
                planner: record.config.planner,
                tool_latency_ms: record.config.tool_latency_ms,
              })}
            </Mono>,
          ],
          [
            "Unsafe actions",
            summary ? (
              <span>
                {fmtInt(num(summary.unsafe_attempts))} attempted · {fmtInt(num(summary.unsafe_blocked))} blocked ·{" "}
                <strong className={num(summary.unsafe_executed) ? "text-bad" : undefined}>{fmtInt(num(summary.unsafe_executed))} executed</strong>
              </span>
            ) : (
              <span className="muted">{NOT_RECORDED}</span>
            ),
          ],
        ]}
      />
      {record.error ? (
        <Notice tone="bad" icon="x" title="Run error">
          {record.error}
        </Notice>
      ) : null}
    </Card>
  );
}

function LiveFacts({
  events,
  provider,
  modelTokenLimit,
}: {
  events: TraceEvent[];
  provider: Partial<ProviderInfo> | undefined;
  modelTokenLimit: number | null;
}) {
  const count = (category: string) => events.filter((e) => e.category === category).length;
  const modes = events.filter((e) => e.category === "routing.decision").map((e) => str(e.payload.mode, "?"));
  const injections = count("safety.injection_flag");
  const denials = events.filter((e) => e.category === "policy.decision" && e.payload.verdict === "DENY").length;
  // M3: how the run searches, the latest merge's disagreements, and reported model tokens.
  const search = events.find((e) => e.category === "retrieval.docs");
  const ragOff = events.some((e) => e.category === "retrieval.skip" && e.payload.stage === "initial");
  const merge = lastOf(events, "verification.merge");
  const conflicts = merge ? disagreementCounts(merge.payload.disagreements) : null;
  const tokens = liveModelTokenUse(events, provider, modelTokenLimit);
  return (
    <div className="stat-grid compact">
      <StatTile label="Tool calls" value={fmtInt(count("tool.call") + count("mitigation.executed"))} tone="neutral" />
      <StatTile label="Routing" value={modes.length ? modes.join(" → ") : "—"} tone="accent" />
      <StatTile label="Injection flags" value={fmtInt(injections)} tone={injections ? "warn" : "neutral"} />
      <StatTile label="Policy denials" value={fmtInt(denials)} tone={denials ? "bad" : "neutral"} />
      <StatTile label="Runbook search" value={search ? searchModeLabel(search.payload.mode) : ragOff ? "off" : "—"} tone="info" />
      <StatTile
        label="Disagreements"
        value={conflicts ? (conflicts.total ? `${conflicts.total} · ${conflicts.open} open` : "none") : "—"}
        note={merge ? "latest merge" : undefined}
        tone={conflicts?.open ? "warn" : "neutral"}
      />
      <StatTile label="Model tokens" value={tokens.value} note={tokens.note} tone={tokens.tone} />
    </div>
  );
}

/** Time since the run's first event, ticking while it runs (its own clock, so the page does not re-render). */
function Elapsed({ startIso, endIso, live }: { startIso: string | null; endIso: string | null; live: boolean }) {
  const now = useNow(1000);
  const start = tsMs(startIso);
  if (!Number.isFinite(start)) return null;
  const end = live ? now : tsMs(endIso);
  if (!Number.isFinite(end)) return null;
  return <span className="muted small">elapsed {fmtMs(Math.max(0, end - start))}</span>;
}

/** POST /runs/{id}/cancel (operator only). A pending approval request expires with it. */
function StopRunButton({ runId, signedIn }: { runId: string; signedIn: boolean }) {
  const [state, setState] = useState<"idle" | "sending" | "sent">("idle");
  const [error, setError] = useState<string | null>(null);
  async function cancel() {
    setState("sending");
    setError(null);
    try {
      await api.cancelRun(runId);
      setState("sent");
    } catch (err) {
      setError(toApiError(err).message);
      setState("idle");
    }
  }
  return (
    <>
      <button
        type="button"
        className="btn btn-reject"
        disabled={state !== "idle" || !signedIn}
        title={
          signedIn
            ? "Stops the run at its next step. A fix already being sent cannot be called back: the run then records its outcome, unverified."
            : "Sign in to stop a run."
        }
        onClick={() => void cancel()}
      >
        <Icon name="stop" size={14} /> {state === "idle" ? "Stop run" : "Stopping…"}
      </button>
      {error ? (
        <span className="small operator-warn" role="alert">
          {error}
        </span>
      ) : null}
    </>
  );
}

function RunPanel({
  runId,
  system,
  signedIn,
  onNewRun,
}: {
  runId: string;
  system: SystemSummary | undefined;
  signedIn: boolean;
  onNewRun: () => void;
}) {
  const stream = useLabRunEvents(runId);
  const events = stream.events;
  const states = useMemo(() => statesOf(events), [events]);
  const times = useMemo(() => stageTimes(events), [events]);
  const current = states.length ? states[states.length - 1] : null;
  const finished = stream.finished || isTerminal(current);
  // A run that another process is running is shown as recorded; nothing here follows it.
  const live = !finished && stream.transport !== "recorded";

  // The run's record and typed summary: once now (replay provenance), again once it ends.
  const detail = usePolling(() => api.runView(runId), (d) => (finished && d?.status === "running" ? 800 : null), [runId, finished]);
  const record = detail.data?.record ?? null;
  const runSummary = detail.data?.summary ?? null;

  // The first event (CREATED) names the provider and environment; the record has them too.
  const first = events.length ? obj(events[0].payload) : {};
  const eventProvider = obj(first.provider) as Partial<ProviderInfo>;
  const eventEnvironment = obj(first.environment) as Partial<EnvironmentInfo>;
  const provider: Partial<ProviderInfo> | null = hasKeys(eventProvider) ? eventProvider : (record?.provider_info ?? null);
  const environment: Partial<EnvironmentInfo> | null = hasKeys(eventEnvironment) ? eventEnvironment : (record?.environment ?? null);
  const incident = obj(first.incident);
  const scenarioId = str(incident.scenario_id) || record?.scenario_id || runSummary?.scenario_id || "";
  const modelTokenLimit = num(obj(incident.budget).max_model_tokens);
  const banner = runBanner(provider, environment);
  const manual = record ? record.config.approval_mode === "manual" : null;

  // --- approval ----------------------------------------------------------------------------
  const phase = approvalPhase(events);
  const waitingManual = live && phase.kind === "waiting" && !phase.auto;
  const pending = usePolling(() => api.pendingApprovals(), waitingManual ? 1500 : null, [runId]);
  const request = waitingManual
    ? (pending.data?.find((r) => r.run_id === runId && (!phase.proposalId || r.proposal_id === phase.proposalId)) ?? null)
    : null;
  const [shown, setShown] = useState<ApprovalRequest | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [decidedHere, setDecidedHere] = useState<Record<string, Approval>>({});
  const autoOpened = useRef(new Set<string>());
  useEffect(() => {
    if (!request) return;
    setShown(request);
    // A new request opens the dialog once; after "Decide later" it reopens only on request.
    if (!autoOpened.current.has(request.proposal_id)) {
      autoOpened.current.add(request.proposal_id);
      setOpenId(request.proposal_id);
    }
  }, [request?.proposal_id]); // eslint-disable-line react-hooks/exhaustive-deps

  const shownId = shown?.proposal_id ?? null;
  const evidence = usePolling(shownId ? () => api.evidenceView(runId) : null, null, [runId, shownId]);
  const proposalView = evidence.data?.proposals.find((p) => p.proposal_id === shownId) ?? null;
  const policy = shown
    ? ([...(evidence.data?.policy_verdicts ?? [])].reverse().find((v) => v.tool === shown.proposal.action && v.verdict === "APPROVAL_REQUIRED") ?? null)
    : null;
  const check = shown ? ([...(evidence.data?.proposal_checks ?? [])].reverse().find((c) => c.proposal_id === shownId) ?? null) : null;

  let closedReason: string | null = null;
  if (shown && !decidedHere[shown.proposal_id]) {
    if (phase.kind === "decided" && (!phase.proposalId || phase.proposalId === shown.proposal_id)) {
      closedReason =
        phase.decision === "expired"
          ? "This request expired before anyone decided, so the fix will not run."
          : `The server recorded “${phase.decision}”${phase.actor ? ` by ${phase.actor}` : ""} for this request, outside this dialog.`;
    } else if (finished) {
      closedReason = `The run ended (${stateLabel(current)}) before a decision was made here.`;
    }
  }
  const reviewable = waitingManual && !!shown && shown.proposal_id === phase.proposalId && openId !== shown.proposal_id;

  const summaryEvent = lastOf(events, "run.summary")?.payload as RunSummaryPayload | undefined;
  const transport = transportView(stream.transport, stream.resumes, stream.lastSeq, finished, events.length, !!stream.error);
  const caption = current ? (
    <>
      <strong>{stateLabel(current)}.</strong> {STAGE_HELP[current] ?? ""}
    </>
  ) : (
    "Waiting for the run's first event…"
  );

  return (
    <div className="run-panel" id="run-panel">
      <Card
        title={
          <span className="run-title">
            Run <Mono>{runId}</Mono>
            {scenarioId ? (
              <span className="muted run-title-scenario">
                <span className="run-title-sep" aria-hidden="true">
                  ·{" "}
                </span>
                {scenarioId}
              </span>
            ) : null}
          </span>
        }
        subtitle={
          <span className="chip-row">
            {current ? <StateToken state={current} size="sm" /> : <StatusToken tone="neutral" icon="clock" label="Starting" size="sm" />}
            <StatusToken tone={transport.tone} icon={transport.icon} label={transport.label} detail={transport.detail || undefined} size="sm" />
            <Elapsed startIso={events[0]?.ts ?? null} endIso={events[events.length - 1]?.ts ?? null} live={live} />
          </span>
        }
        actions={
          <span className="chip-row">
            {live ? <StopRunButton runId={runId} signedIn={signedIn} /> : null}
            <button type="button" className="btn" onClick={onNewRun}>
              <Icon name="play" size={14} /> New run
            </button>
          </span>
        }
      >
        <RunBannerView banner={banner} replay={runSummary?.replay ?? null} />
        {live && !signedIn && manual !== false ? <SignInPrompt what="approve or reject this run's fix" /> : null}
        <StateStepper states={states} running={live} times={times} caption={caption} />
        {stream.transport === "recorded" ? (
          <div className="muted small">Started by another process: showing its recorded events; live updates are unavailable here.</div>
        ) : null}
        {stream.error ? <ErrorView error={stream.error} title="Live updates interrupted" /> : null}
      </Card>

      {finished ? <OutcomeCard runId={runId} summary={runSummary} record={record} loading={detail.loading || detail.data?.status === "running"} /> : null}

      <div className="run-grid">
        <Card title="Live event feed" subtitle="Real trace events from the controller, the agents and the gateway, newest at the bottom" flush>
          <EventFeed events={events} live={live} height={560} overlays={knowledgeOverlays(record?.versions)} label={`Trace events of run ${runId}`} />
        </Card>
        <div className="run-side">
          {waitingManual ? (
            shown && shown.proposal_id === phase.proposalId ? (
              <Card tone="warn" title="Your approval is needed" subtitle="The run is paused until a person decides">
                <div className="approval-action">
                  <code className="mono">{shown.proposal.action}</code>
                  <span className="muted">on</span>
                  <code className="mono">{shown.proposal.target}</code>
                  {Object.keys(shown.proposal.params ?? {}).length ? <code className="mono muted">({fmtArgs(shown.proposal.params)})</code> : null}
                </div>
                <div className="chip-row">
                  <ApprovalCountdown expiresAt={shown.expires_at} />
                  <span className="muted small">proposal {shown.proposal_id}</span>
                </div>
                {reviewable ? (
                  <button type="button" className="btn btn-primary" onClick={() => setOpenId(shown.proposal_id)}>
                    <Icon name="key" size={14} /> Review approval
                  </button>
                ) : null}
              </Card>
            ) : (
              <Notice tone="warn" icon="key" title="Waiting for the approval request…">
                The run asked for approval; polling GET /approvals/pending for its request.
              </Notice>
            )
          ) : null}
          {live && phase.kind === "waiting" && phase.auto ? (
            <Notice tone="warn" icon="key" title="The simulation auto-approver is deciding">
              This run uses approval_mode = auto, which the controller allows only against a simulated environment.
            </Notice>
          ) : null}
          {live && phase.kind === "decided" ? (
            <Notice
              tone={phase.decision === "approved" ? "ok" : "warn"}
              icon={phase.decision === "approved" ? "check" : "stop"}
              title={`Approval ${phase.decision}${phase.actor ? ` by ${phase.actor}` : ""}`}
            >
              {phase.decision === "approved"
                ? "The approved action now goes through the safety gateway, then the oracle checks the recovery."
                : "The fix will not run."}
            </Notice>
          ) : null}
          {finished ? (
            record ? (
              <RunDetails record={record} summary={summaryEvent} system={system} memorySkip={str(lastOf(events, "memory.skip")?.payload.reason)} />
            ) : detail.error ? (
              <ErrorView error={detail.error} title="Could not load the final checkpoint" onRetry={detail.refresh} />
            ) : (
              <Card title="Run details">
                <LoadingView label="Loading the final checkpoint…" />
              </Card>
            )
          ) : (
            <Card title="So far" subtitle="Counted from the trace as it arrives">
              <LiveFacts events={events} provider={provider ?? undefined} modelTokenLimit={modelTokenLimit} />
            </Card>
          )}
        </div>
      </div>

      {shown ? (
        <ApprovalModal
          key={shown.proposal_id}
          request={shown}
          open={openId === shown.proposal_id}
          onDefer={() => setOpenId(null)}
          onDecided={(approval) => {
            setDecidedHere((d) => ({ ...d, [shown.proposal_id]: approval }));
            pending.refresh();
          }}
          trustedRisk={trustedRiskOf(events, shown.proposal_id)}
          policy={policy}
          check={check}
          author={rationaleAuthor(proposalView?.author, provider)}
          evidence={{
            registry: evidence.data?.registry ?? null,
            note: evidence.error ? `Could not load the run's evidence: ${evidence.error.message}` : undefined,
          }}
          closedReason={closedReason}
        />
      ) : null}
    </div>
  );
}

// ======================================================================================
// Page
// ======================================================================================

/**
 * POST /incidents/run for a replay at a pace. api.startReplay sends the same body without
 * replay_pace; api.startRun posts a run request with the operator token, so the body is the one
 * replayRunBody builds (src/view/lab.ts), typed here as the request it is.
 */
function startPacedReplay(fixture: dto.FixtureView, pace: number) {
  return api.startRun(replayRunBody(fixture, pace) as unknown as RunRequest);
}

export function LiveRunPage({ route, system }: PageProps) {
  const runId = route.params.get("run");
  const signedIn = useOperatorToken() !== null;
  const replays = usePolling(() => api.replays(), null, []);
  const providers = usePolling(() => api.providerViews(), 30000, []);
  const scenarios = usePolling(() => api.scenarios(), null, []);
  const memory = usePolling(() => api.memory(), 15000, []);
  const [launcherOpen, setLauncherOpen] = useState(!runId);

  const [pace, setPace] = useState(DEFAULT_PACE);
  const [replayStarting, setReplayStarting] = useState<string | null>(null);
  const [replayError, setReplayError] = useState<{ fixtureId: string; error: ApiError } | null>(null);

  const [selected, setSelected] = useState<string | null>(null);
  const [config, setConfig] = useState<RunConfigInput>(DEFAULT_CONFIG);
  const [backend, setBackend] = useState<string | null>(null);
  const [showTruth, setShowTruth] = useState(false);
  const [showOthers, setShowOthers] = useState(false);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<ApiError | null>(null);

  const sorted = useMemo(() => sortScenarios(scenarios.data ?? []), [scenarios.data]);
  const demo = sorted.filter((s) => s.partition === "demo");
  const others = sorted.filter((s) => s.partition !== "demo");
  const scenario = sorted.find((s) => s.id === selected) ?? null;
  const choices = useMemo(() => providerChoices(providers.data ?? []), [providers.data]);
  const environment = system.data?.environment;

  const choose = (s: ScenarioInfo) => {
    setSelected(s.id);
    setConfig((c) => withOverrides(c, s));
  };

  // Default: the first demo scenario, and the server's default backend.
  useEffect(() => {
    if (!selected && sorted.length) choose(sorted[0]);
  }, [sorted]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!backend && providers.data) setBackend(initialProvider(choices));
  }, [providers.data]); // eslint-disable-line react-hooks/exhaustive-deps

  // A deep link to a run opens with the launchers collapsed.
  useEffect(() => {
    if (runId) setLauncherOpen(false);
  }, [runId]);

  const openRun = (id: string) => {
    setLauncherOpen(false);
    navigate("live", { run: id });
    window.setTimeout(() => document.getElementById("run-panel")?.scrollIntoView({ behavior: "smooth", block: "start" }), 80);
  };

  async function replay(fixture: dto.FixtureView) {
    setReplayStarting(fixture.id);
    setReplayError(null);
    try {
      const started = await startPacedReplay(fixture, pace);
      openRun(started.run_id);
    } catch (err) {
      setReplayError({ fixtureId: fixture.id, error: toApiError(err) });
    } finally {
      setReplayStarting(null);
    }
  }

  async function start() {
    if (!scenario) return;
    setStarting(true);
    setStartError(null);
    try {
      // `retrieval` is sent only when the operator chose a search, `provider` only when it is not the default.
      const response = await api.startRun({ scenario_id: scenario.id, config: runConfigBody(config, backend, choices.defaultName) });
      openRun(response.run_id);
    } catch (err) {
      setStartError(toApiError(err));
    } finally {
      setStarting(false);
    }
  }

  const visibleScenarios = showOthers ? [...demo, ...others] : demo.length ? demo : sorted;
  const reopen = () => {
    setLauncherOpen(true);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  return (
    <>
      <PageHeader
        title="Incident Lab"
        description="Replay a recorded real-model incident or start a new one, then watch RECAP investigate, propose a fix, wait for a person's approval and verify the recovery. Everything streams from the API and acts only on the simulated shop."
      />

      {launcherOpen ? (
        <>
          {signedIn ? null : <SignInPrompt />}
          <ReplayLauncher
            replays={replays}
            environment={environment}
            signedIn={signedIn}
            pace={pace}
            onPace={setPace}
            starting={replayStarting}
            startError={replayError}
            onReplay={(fixture) => void replay(fixture)}
          />

          <Card
            title="Run a scenario"
            subtitle="Inject a scenario's fault into the simulated shop and let RECAP handle it with the backend you choose (GET /scenarios)"
            actions={<Toggle checked={showTruth} onChange={setShowTruth} label="Show ground truth" hint="hidden from agents" />}
          >
            <PolledView
              polled={scenarios}
              loadingLabel="Loading scenarios…"
              errorTitle="Could not load the scenarios (GET /scenarios)"
              emptyTitle="No scenarios"
              empty={(list) => (list.length ? null : "The environment reports no scenarios: check configs/environment.yaml and fixtures/scenarios.")}
            >
              {() => (
                <>
                  <div className="scenario-grid">
                    {visibleScenarios.map((s) => (
                      <ScenarioCard
                        key={s.id}
                        scenario={s}
                        selected={s.id === selected}
                        onSelect={() => choose(s)}
                        showTruth={showTruth}
                        missing={missingPrerequisites(s, memory.data as ExperienceEpisode[] | undefined)}
                      />
                    ))}
                  </div>
                  {others.length && demo.length ? (
                    <button type="button" className="btn btn-ghost btn-small" onClick={() => setShowOthers((v) => !v)} aria-expanded={showOthers}>
                      <Icon name={showOthers ? "chevronDown" : "chevronRight"} size={14} />
                      {showOthers ? "Hide" : "Show"} {others.length} evaluation scenarios (train/test partitions)
                    </button>
                  ) : null}
                </>
              )}
            </PolledView>
          </Card>

          <Card title="Backend, settings and safety" subtitle="Sent as the RunConfig of POST /incidents/run; safety cannot be switched off from the API">
            <div className="lab-setup">
              <section className="lab-setup-main" aria-labelledby="lab-backend-title">
                <h3 id="lab-backend-title" className="lab-subtitle">
                  Decision backend
                </h3>
                <p className="lab-help">Who makes the agents' decisions. Only backends this server lets a run pick are offered.</p>
                <BackendChoice providers={providers} value={backend} onChange={setBackend} />
                <h3 className="lab-subtitle">Settings</h3>
                <ConfigForm config={config} onChange={setConfig} system={system.data} />
              </section>
              <section className="lab-setup-side" aria-labelledby="lab-safety-title">
                <h3 id="lab-safety-title" className="lab-subtitle">
                  What keeps this safe
                </h3>
                <p className="lab-help">These hold for every run started here, whatever the settings.</p>
                <SafetyExplainer environment={environment} />
              </section>
            </div>
            <div className="run-bar">
              <div className="muted">
                {scenario ? (
                  <>
                    Scenario <Mono>{scenario.id}</Mono> · backend <Mono>{backend ?? "server default"}</Mono> · {config.strategy} · RAG{" "}
                    {config.use_rag ? "on" : "off"} · search {config.retrieval ? searchModeLabel(config.retrieval) : "server default"} · memory{" "}
                    {config.use_memory ? "on" : "off"} · {config.approval_mode} approval · {config.tool_latency_ms ?? "default"} ms
                  </>
                ) : (
                  "Select a scenario."
                )}
              </div>
              <div className="chip-row">
                {runId ? (
                  <button type="button" className="btn btn-ghost" onClick={() => setLauncherOpen(false)}>
                    Back to the current run
                  </button>
                ) : null}
                <button type="button" className="btn btn-primary btn-large" disabled={!scenario || starting || !signedIn} onClick={() => void start()}>
                  <Icon name="play" size={15} /> {starting ? "Starting…" : "Start run"}
                </button>
              </div>
            </div>
            {signedIn ? null : <span className="muted small">Sign in to start a run.</span>}
            {startError ? <ErrorView error={startError} title="Could not start the run" /> : null}
          </Card>
        </>
      ) : (
        <div className="setup-collapsed">
          <span className="muted">
            Viewing run <Mono>{runId ?? "—"}</Mono>. Its settings are listed under Run details when it ends.
          </span>
          <button type="button" className="btn" onClick={reopen}>
            <Icon name="play" size={14} /> Replay or start another run
          </button>
        </div>
      )}

      {runId ? <RunPanel key={runId} runId={runId} system={system.data} signedIn={signedIn} onNewRun={reopen} /> : null}
    </>
  );
}
