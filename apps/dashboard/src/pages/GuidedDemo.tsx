import { useMemo, useState } from "react";
import { ApiError, api, toApiError } from "../api";
import type { Page } from "../api";
import { ApprovalCountdown, ApprovalModal } from "../components/ApprovalCard";
import { HistoricalBanner } from "../components/Historical";
import { Mono } from "../components/Badge";
import { Card, Notice, PageHeader, Segmented, Toggle } from "../components/Card";
import { Icon } from "../components/Icon";
import type { IconName } from "../components/Icon";
import { EmptyView, ErrorView, PolledView } from "../components/StateViews";
import { StatusToken } from "../components/StatusToken";
import { UntrustedText } from "../components/Untrusted";
import type * as dto from "../dto";
import { fmtRelative, isTerminal, obj, stateLabel } from "../format";
import type { Tone } from "../format";
import { hrefFor, navigate, useNow, useOperatorToken, usePolling } from "../hooks";
import type { PageProps, Polled } from "../hooks";
import type { ApprovalRequest, EnvironmentInfo, ProviderInfo, RunRecord, RunRequest, SystemSummary, TraceEvent } from "../types";
import {
  DEFAULT_DEMO_PACE,
  DEMO_PACES,
  DEMO_PROVENANCE,
  DEMO_STEPS,
  activeActors,
  approvalStory,
  demoNotices,
  demoProgress,
  goldenFixture,
  paceFor,
  placeOf,
  provenanceWords,
  recentActivity,
} from "../view/demo";
import type { DemoActivity, DemoActor, DemoFact, DemoNotice, DemoPace, DemoStepId, DemoStepStatus, DemoStepView } from "../view/demo";
import { explainRule } from "../view/explain";
import { approvalTarget, fmtOffset, outcomeView, replayBlockedNote, replayRunBody, runBanner, statesOf, transportView } from "../view/lab";
import type { LabTransport, TargetFields } from "../view/lab";
import { Elapsed, OutcomeCard, RunBannerView, SignInPrompt, StopRunButton, useLabRunEvents, useRunApproval } from "./LiveRun";
import "../styles/guided-demo.css";

/**
 * Guided Demo: the interview path through RECAP. It replays the golden recording (GET /replays,
 * the committed real-model run pinned in fixtures/recordings/MANIFEST.json) against the simulated
 * shop, through the same API, safety gateway and approval dialog as the Incident Lab, and tells
 * the run as ten steps in plain words while its trace arrives. Nothing on this page is staged:
 * every step, time, actor and result comes from the run's own events, the approval is a fresh
 * one by the operator, and a replay always says that no model is called now.
 *
 * #/demo                     the start screen (and earlier demo runs to review)
 * #/demo?run=<id>            a demo run, followed live or reviewed after it ended
 * &mode=interview            presenter notes at each step
 * &pace=guided|quick|instant how long each recorded answer waits (replay_pace)
 */

type Loaded<T> = Pick<Polled<T>, "data" | "error" | "loading" | "refresh">;

const STATUS_WORDS: Readonly<Record<DemoStepStatus, string>> = {
  upcoming: "Not yet",
  current: "In progress",
  waiting: "Waiting for you",
  done: "Done",
  skipped: "Skipped",
};

const STATUS_ICONS: Readonly<Record<DemoStepStatus, IconName>> = {
  upcoming: "dot",
  current: "clock",
  waiting: "key",
  done: "check",
  skipped: "minus",
};

const TONE_ICONS: Readonly<Record<Tone, IconName>> = {
  neutral: "dot",
  accent: "dot",
  info: "info",
  ok: "check",
  warn: "alert",
  bad: "x",
};

/** The query a demo address keeps: interview mode, and a pace other than the guided default. */
function demoParams(run: string | null, interview: boolean, pace: DemoPace) {
  return { run, mode: interview ? "interview" : null, pace: pace.key === DEFAULT_DEMO_PACE.key ? null : pace.key };
}

// ======================================================================================
// Provenance
// ======================================================================================

/** The label every replayed demo carries, and what it means. */
export function DemoProvenance({ words }: { words: string }) {
  return (
    <div className="demo-provenance" role="note" aria-label="Where this demo's decisions come from">
      <Icon name="rewind" size={20} />
      <div>
        <div className="demo-provenance-label">{DEMO_PROVENANCE}</div>
        <p className="demo-provenance-text">{words}</p>
      </div>
    </div>
  );
}

/** Provenance words for a run, from its first event's provider (replay_of names the recording). */
function runProvenance(provider: Partial<ProviderInfo> | null): string {
  const of = obj(provider?.replay_of);
  return provenanceWords({
    model: typeof of.model === "string" ? of.model : (provider?.model ?? "a real model"),
    provider: typeof of.original_provider === "string" ? of.original_provider : "",
    recording_id: typeof of.recording_id === "string" ? of.recording_id : "",
  });
}

// ======================================================================================
// The ten steps
// ======================================================================================

/** The step list: a button per step to look at it, the current one marked for assistive technology. */
export function DemoStepper({ steps, shown, onSelect }: { steps: DemoStepView[]; shown: DemoStepId | null; onSelect: (id: DemoStepId) => void }) {
  return (
    <ol className="demo-steps" aria-label="The demo's ten steps">
      {steps.map((step, index) => {
        const icon = step.status === "done" && step.tone !== "ok" ? TONE_ICONS[step.tone] : STATUS_ICONS[step.status];
        const live = step.status === "current" || step.status === "waiting";
        return (
          <li key={step.id} className={`demo-step status-${step.status}${shown === step.id ? " shown" : ""}`} aria-current={live ? "step" : undefined}>
            <button type="button" className="demo-step-button" onClick={() => onSelect(step.id)} aria-pressed={shown === step.id}>
              <span className={`demo-step-mark tone-${step.tone}`} aria-hidden="true">
                {step.status === "upcoming" ? index + 1 : <Icon name={icon} size={13} />}
              </span>
              <span className="demo-step-text">
                <span className="demo-step-title">
                  <span className="sr-only">Step {index + 1}: </span>
                  {step.title}
                </span>
                <span className="demo-step-status">
                  {STATUS_WORDS[step.status]}
                  {step.at !== null ? ` · ${fmtOffset(step.at)}` : ""}
                </span>
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}

/** The rules behind a fact, explained; the codes themselves are the technical detail. */
export function RuleDetails({ codes, failed }: { codes: string[]; failed: boolean }) {
  return (
    <details className="demo-rules">
      <summary>{codes.length === 1 ? "The rule behind this" : `The ${codes.length} rules behind this`}</summary>
      <ul>
        {codes.map((code) => {
          const rule = explainRule(code);
          return (
            <li key={code}>
              <code className="mono">{rule.code}</code> <span className="muted">{rule.family}.</span> {failed ? rule.failed : rule.checks}
            </li>
          );
        })}
      </ul>
    </details>
  );
}

function FactLine({ fact, replay }: { fact: DemoFact; replay: boolean }) {
  const tone = fact.tone ?? "neutral";
  const source = replay ? "recorded model output, replayed" : "model output";
  return (
    <li className={`demo-fact tone-${tone}`}>
      <Icon name={TONE_ICONS[tone]} size={14} />
      <div className="demo-fact-body">
        {fact.model ? (
          <>
            <span className="demo-fact-author">
              Written by {fact.model} ({source}):
            </span>{" "}
            <UntrustedText text={fact.text} author={`${fact.model} (${source})`} />
          </>
        ) : (
          <span>{fact.text}</span>
        )}
        {fact.codes?.length ? <RuleDetails codes={fact.codes} failed={tone === "bad"} /> : null}
      </div>
    </li>
  );
}

/** One step explained: what it does, what it found, and (in interview mode) what to point out. */
export function StepPanel({
  step,
  interview,
  replay,
  pinned,
  onFollow,
}: {
  step: DemoStepView;
  interview: boolean;
  replay: boolean;
  /** The visitor chose this step; the panel no longer follows the run. */
  pinned: boolean;
  onFollow: () => void;
}) {
  const index = DEMO_STEPS.findIndex((s) => s.id === step.id);
  return (
    <section className="demo-panel" aria-labelledby="demo-step-heading">
      <div className="demo-panel-top">
        <span className="demo-eyebrow">
          Step {index + 1} of {DEMO_STEPS.length}
        </span>
        <StatusToken tone={step.tone} icon={STATUS_ICONS[step.status]} label={STATUS_WORDS[step.status]} size="sm" />
        {step.at !== null ? <span className="muted small">{fmtOffset(step.at)} after the alert</span> : null}
        {pinned ? (
          <button type="button" className="btn btn-ghost btn-small demo-follow" onClick={onFollow}>
            <Icon name="arrowRight" size={13} /> Back to the current step
          </button>
        ) : null}
      </div>
      <h2 id="demo-step-heading" className="demo-step-heading">
        {step.title}
      </h2>
      <p className="demo-about">{step.about}</p>
      {interview ? (
        <div className="demo-talk">
          <Icon name="bulb" size={15} />
          <div>
            <strong>Presenter note.</strong> {step.talk}
          </div>
        </div>
      ) : null}
      {step.status === "skipped" ? (
        <Notice tone="neutral" icon="minus" title="This step did not happen">
          {step.skippedBecause}
        </Notice>
      ) : step.status === "upcoming" ? (
        <p className="muted">This step has not happened yet.</p>
      ) : step.facts.length ? (
        <ul className="demo-facts" aria-label={`What step ${index + 1} found`}>
          {step.facts.map((fact, i) => (
            <FactLine key={i} fact={fact} replay={replay} />
          ))}
        </ul>
      ) : (
        <p className="muted">In progress: its results appear here as the trace records them.</p>
      )}
    </section>
  );
}

const ACTOR_ICONS: Readonly<Record<DemoActor["kind"], IconName>> = { agent: "cpu", rules: "shield", person: "users", oracle: "heart" };

export function ActorsPanel({ actors, replay }: { actors: DemoActor[]; replay: boolean }) {
  if (!actors.length) return null;
  return (
    <div className="demo-actors">
      <h3 className="demo-subhead">Working now</h3>
      <ul>
        {actors.map((actor) => (
          <li key={actor.id} className={`demo-actor kind-${actor.kind}`}>
            <Icon name={ACTOR_ICONS[actor.kind]} size={16} />
            <div>
              <div className="demo-actor-name">
                {actor.name}
                {actor.kind === "agent" ? <span className="demo-tag">{replay ? "AI agent · recorded answers" : "AI agent"}</span> : null}
              </div>
              <div className="muted small">{actor.does}</div>
              {actor.doing ? (
                <div className="small demo-actor-doing">
                  {actor.model ? <UntrustedText text={actor.doing} author={`${actor.name} (model output${replay ? ", replayed" : ""})`} /> : actor.doing}
                </div>
              ) : null}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

const ACTIVITY_ICONS: Readonly<Record<DemoActivity["kind"], IconName>> = { model: "cpu", tool: "tool", policy: "shield" };

export function ActivityList({ items, technical }: { items: DemoActivity[]; technical: boolean }) {
  if (!items.length) return <p className="muted small">Nothing yet: model answers and tool results appear here as they are recorded.</p>;
  return (
    <ol className="demo-activity" aria-label="Latest model and tool activity, newest last">
      {items.map((item) => (
        <li key={item.seq} className={`demo-act tone-${item.tone}`}>
          <span className="demo-act-kind">
            <Icon name={ACTIVITY_ICONS[item.kind]} size={13} />
            {item.kind === "model" ? (item.replayed ? "Model · replayed" : "Model") : item.kind === "tool" ? "Tool" : "Safety policy"}
          </span>
          <span className="demo-act-text">{item.text}</span>
          {technical ? (
            <span className="demo-act-meta mono">
              {fmtOffset(item.at)} · event {item.seq} · {item.detail}
            </span>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

export function NoticeList({ notices }: { notices: DemoNotice[] }) {
  if (!notices.length) return null;
  return (
    <div className="demo-notices">
      {notices.map((notice, i) => (
        <Notice key={i} tone={notice.tone} icon={notice.icon} title={notice.title}>
          {notice.text}
          {notice.codes.length ? <RuleDetails codes={notice.codes} failed={notice.tone === "bad"} /> : null}
        </Notice>
      ))}
    </div>
  );
}

// ======================================================================================
// The decision
// ======================================================================================

/** What the waiting fix would do, either way, and the button that opens the approval dialog. */
export function DecisionCard({
  request,
  simulated,
  trustedRisk,
  signedIn,
  reviewable,
  onReview,
}: {
  request: ApprovalRequest | null;
  simulated: boolean;
  trustedRisk: string | null;
  signedIn: boolean;
  reviewable: boolean;
  onReview: () => void;
}) {
  if (!request) {
    return (
      <Notice tone="warn" icon="key" title="The fix needs your approval">
        Loading the approval request (GET /approvals/pending)…
      </Notice>
    );
  }
  const story = approvalStory(request.proposal, simulated, trustedRisk);
  return (
    <Card tone="warn" title="Your decision is needed" subtitle="The run is paused. Nothing has changed yet.">
      <p className="demo-decision-headline">{story.headline}</p>
      <dl className="demo-decision">
        <div>
          <dt>If you approve</dt>
          <dd>{story.ifApprove}</dd>
        </div>
        <div>
          <dt>If you reject</dt>
          <dd>{story.ifReject}</dd>
        </div>
        {story.risk ? (
          <div>
            <dt>Why it asks you</dt>
            <dd>{story.risk}</dd>
          </div>
        ) : null}
      </dl>
      <div className="chip-row">
        <ApprovalCountdown expiresAt={request.expires_at} />
        <span className="muted small">
          proposal <Mono>{request.proposal_id}</Mono>
        </span>
      </div>
      {!signedIn ? (
        <SignInPrompt what="approve or reject this fix" />
      ) : reviewable ? (
        <button type="button" className="btn btn-primary" onClick={onReview}>
          <Icon name="key" size={14} /> Review the fix and decide
        </button>
      ) : (
        <p className="muted small">The approval dialog shows the exact action, the evidence it cites, and the checks it passed.</p>
      )}
    </Card>
  );
}

// ======================================================================================
// A demo run
// ======================================================================================

export interface DemoRunViewProps {
  runId: string;
  events: TraceEvent[];
  /** The run is over (run.summary, a final state, or the stream says so). */
  finished: boolean;
  /** Followed live (not a run another process recorded). */
  live: boolean;
  transport: LabTransport;
  streamError: ApiError | null;
  interview: boolean;
  signedIn: boolean;
  /** The run's own provider and environment (its first event, else its record). */
  provider: Partial<ProviderInfo> | null;
  environment: Partial<EnvironmentInfo> | null;
  summary: dto.RunSummary | null;
  record: RunRecord | null;
  detailLoading: boolean;
  /** The approval: waiting for a person, the request, and whether its dialog can be reopened. */
  waiting: boolean;
  request: ApprovalRequest | null;
  reviewable: boolean;
  onReview: () => void;
  /** A step the visitor chose (null: follow the run). */
  pinned: DemoStepId | null;
  onPin: (id: DemoStepId | null) => void;
  technical: boolean;
  onTechnical: (on: boolean) => void;
  onInterview: (on: boolean) => void;
  onRestart: () => void;
  restarting: boolean;
  restartError: ApiError | null;
}

export function DemoRunView(props: DemoRunViewProps) {
  const { runId, events, finished, live, interview, signedIn, provider, environment, summary, record, pinned, technical } = props;
  const progress = useMemo(() => demoProgress(events, finished), [events, finished]);
  const actors = useMemo(() => activeActors(events, progress), [events, progress]);
  const activity = useMemo(() => recentActivity(events), [events]);
  const notices = useMemo(() => demoNotices(events), [events]);
  const replay = provider?.origin === "replay" || summary?.provider_kind === "replay";
  // A real-cluster run published with the repository: historical, told step by step, never acted on.
  const published = summary?.status === "published";
  const simulated = placeOf(events).simulated !== false && environment?.is_simulated !== false;
  const shown = (pinned ? progress.steps.find((s) => s.id === pinned) : null) ?? progress.current ?? progress.steps[0];
  const following = !pinned || pinned === progress.current?.id;
  const transport = transportView(props.transport, 0, events.length ? events[events.length - 1].seq : 0, finished, events.length, !!props.streamError);
  const outcome = finished ? outcomeView(summary?.outcome, environment) : null;
  const banner = runBanner(provider, environment);
  const trustedRisk = props.request ? (events.find((e) => e.category === "mitigation.proposed")?.payload.trusted_risk as string | undefined) ?? null : null;

  if (props.streamError?.status === 404) {
    return (
      <EmptyView
        title="This run is not on this server"
        reason="It may belong to another data folder."
        action={
          <a className="btn btn-primary" href={hrefFor("demo")}>
            <Icon name="play" size={14} /> Start a new guided demo
          </a>
        }
      />
    );
  }

  return (
    <div className="demo">
      {replay ? (
        <DemoProvenance words={runProvenance(provider)} />
      ) : published ? (
        <HistoricalBanner summary={summary} />
      ) : (
        <Notice tone="info" icon="info" title="This run is not a replay">
          The guided view tells any run's steps from its trace, but only a replay of a recording is the demo. {banner.explanation}
        </Notice>
      )}

      <div className="demo-toolbar" role="group" aria-label="Demo controls">
        <span className="demo-status" aria-live="polite">
          <strong>
            {finished
              ? summary
                ? (outcome?.label ?? "Finished")
                : progress.finalState
                  ? stateLabel(progress.finalState)
                  : "Finished"
              : progress.current
                ? `Step ${DEMO_STEPS.findIndex((s) => s.id === progress.current?.id) + 1} of 10`
                : "Starting"}
          </strong>
          {finished ? "" : progress.current ? `: ${progress.current.title}` : ": waiting for the run's first event"}
        </span>
        <Elapsed startIso={events[0]?.ts ?? null} endIso={events[events.length - 1]?.ts ?? null} live={live} />
        <span className="demo-toolbar-spacer" />
        <Toggle checked={interview} onChange={props.onInterview} label="Presenter notes" />
        <Toggle checked={technical} onChange={props.onTechnical} label="Technical details" />
        {live && !finished ? <StopRunButton runId={runId} signedIn={signedIn} /> : null}
        {published ? (
          <a className="btn" href={hrefFor("published")}>
            <Icon name="package" size={14} /> All published runs
          </a>
        ) : (
          <button type="button" className="btn" onClick={props.onRestart} disabled={!signedIn || props.restarting} title={signedIn ? "Start the demo again: a new replay run" : "Sign in to start a run"}>
            <Icon name="refresh" size={14} /> {props.restarting ? "Restarting…" : "Restart"}
          </button>
        )}
      </div>
      {props.restartError ? <ErrorView error={props.restartError} title="Could not restart the demo" /> : null}
      {props.streamError ? <ErrorView error={props.streamError} title="Live updates interrupted" /> : null}
      {live && !signedIn && !props.waiting ? <SignInPrompt what="approve or reject this run's fix when it asks" /> : null}

      <div className="demo-layout">
        <aside className="demo-aside">
          <DemoStepper steps={progress.steps} shown={shown.id} onSelect={(id) => props.onPin(id === progress.current?.id ? null : id)} />
        </aside>
        <div className="demo-main">
          <NoticeList notices={notices} />
          {props.waiting ? (
            <DecisionCard request={props.request} simulated={simulated} trustedRisk={trustedRisk} signedIn={signedIn} reviewable={props.reviewable} onReview={props.onReview} />
          ) : null}
          <Card>
            <StepPanel step={shown} interview={interview} replay={replay} pinned={!following} onFollow={() => props.onPin(null)} />
            {following && !finished ? <ActorsPanel actors={actors} replay={replay} /> : null}
          </Card>
          {finished ? (
            <>
              <OutcomeCard
                runId={runId}
                summary={summary}
                record={record}
                loading={props.detailLoading || summary?.status === "running"}
                environment={environment}
                recovery={(events.find((e) => e.category === "verification.recovery")?.payload as Record<string, unknown> | undefined) ?? null}
              />
              <Notice tone="accent" icon="compass" title="Go deeper">
                Every step above is in this run's trace: open <a href={hrefFor("timeline", { run: runId })}>Trace Explorer</a> for each model answer,
                tool call and policy decision. How the parts fit together: <a href={hrefFor("architecture")}>Architecture &amp; Learning</a>. How
                RECAP is measured: <a href={hrefFor("evals")}>Evaluations</a>.
              </Notice>
            </>
          ) : null}
          <Card title="Latest model and tool activity" subtitle={replay ? "From the trace. Model answers are the recording's, replayed; tools run now." : "From the trace."}>
            <ActivityList items={activity} technical={technical} />
          </Card>
          {technical ? (
            <Card title="Technical details" subtitle="Where this run's decisions come from, and how this page follows it">
              <RunBannerView banner={banner} replay={summary?.replay ?? null} />
              <ul className="demo-tech">
                <li>
                  Run <Mono>{runId}</Mono>: {events.length} trace events · <StatusToken tone={transport.tone} icon={transport.icon} label={transport.label} size="sm" />
                </li>
                <li>
                  Every event, in full: <a href={hrefFor("timeline", { run: runId })}>Trace Explorer</a> · the same run in the{" "}
                  <a href={hrefFor("live", { run: runId })}>Incident Lab</a>
                </li>
              </ul>
            </Card>
          ) : null}
        </div>
      </div>
    </div>
  );
}

/** A demo run, followed live: its events, its record, and its approval (the Incident Lab's own). */
function DemoRun({
  runId,
  system,
  signedIn,
  interview,
  onInterview,
  onRestart,
  restarting,
  restartError,
}: {
  runId: string;
  system: Polled<SystemSummary>;
  signedIn: boolean;
  interview: boolean;
  onInterview: (on: boolean) => void;
  onRestart: (cancelFirst: boolean) => void;
  restarting: boolean;
  restartError: ApiError | null;
}) {
  const stream = useLabRunEvents(runId);
  const events = stream.events;
  const states = useMemo(() => statesOf(events), [events]);
  const current = states.length ? states[states.length - 1] : null;
  const finished = stream.finished || isTerminal(current);
  const live = !finished && stream.transport !== "recorded";
  const detail = usePolling(() => api.runView(runId), (d) => (finished && d?.status === "running" ? 800 : null), [runId, finished]);
  const record = detail.data?.record ?? null;

  const first = events.length ? obj(events[0].payload) : {};
  const eventProvider = obj(first.provider) as Partial<ProviderInfo>;
  const eventEnvironment = obj(first.environment) as Partial<EnvironmentInfo>;
  const provider: Partial<ProviderInfo> | null = Object.keys(eventProvider).length ? eventProvider : (record?.provider_info ?? null);
  const environment: Partial<EnvironmentInfo> | null = Object.keys(eventEnvironment).length ? eventEnvironment : (record?.environment ?? null);
  const decideOn: TargetFields | null = approvalTarget(environment, system.data?.environment ?? null);
  const approval = useRunApproval({ runId, events, live, finished, current, provider, decideOn });
  const waitingRequest = approval.waitingManual && approval.shown && approval.phase.kind === "waiting" && approval.shown.proposal_id === approval.phase.proposalId ? approval.shown : null;

  const [pinned, setPinned] = useState<DemoStepId | null>(null);
  const [technical, setTechnical] = useState(false);

  return (
    <>
      <DemoRunView
        runId={runId}
        events={events}
        finished={finished}
        live={live}
        transport={stream.transport}
        streamError={stream.error}
        interview={interview}
        signedIn={signedIn}
        provider={provider}
        environment={environment}
        summary={detail.data?.summary ?? null}
        record={record}
        detailLoading={detail.loading}
        waiting={approval.waitingManual}
        request={waitingRequest}
        reviewable={approval.reviewable}
        onReview={() => approval.shown && approval.setOpenId(approval.shown.proposal_id)}
        pinned={pinned}
        onPin={setPinned}
        technical={technical}
        onTechnical={setTechnical}
        onInterview={onInterview}
        onRestart={() => onRestart(live)}
        restarting={restarting}
        restartError={restartError}
      />
      {approval.modal ? <ApprovalModal key={approval.modal.request.proposal_id} {...approval.modal} /> : null}
    </>
  );
}

// ======================================================================================
// The start screen
// ======================================================================================

export interface DemoStartViewProps {
  replays: Loaded<dto.ReplaysView>;
  environment: TargetFields | null | undefined;
  signedIn: boolean;
  pace: DemoPace;
  onPace: (key: DemoPace["key"]) => void;
  interview: boolean;
  onInterview: (on: boolean) => void;
  starting: boolean;
  startError: ApiError | null;
  onStart: (fixture: dto.FixtureView) => void;
  /** Earlier replays of the demo's recording on this server, newest first. */
  previous: dto.RunSummary[];
  now: number;
}

function Recording({ fixture }: { fixture: dto.FixtureView }) {
  return (
    <div className="demo-recording">
      <div className="demo-recording-title">{fixture.title}</div>
      <div className="demo-recording-meta">
        <span>{fixture.agents}</span>
        <span>
          scenario <Mono>{fixture.scenario}</Mono>
        </span>
        <StatusToken
          tone={fixture.integrity === "intact" ? "ok" : "bad"}
          icon={fixture.integrity === "intact" ? "check" : "x"}
          label={fixture.integrity === "intact" ? "Recording intact" : `Recording ${fixture.integrity}`}
          title={`SHA-256 ${fixture.sha256}… checked against the manifest`}
          size="sm"
          variant="outline"
        />
      </div>
      <p className="small muted">Originally approved by {fixture.approved_by}. This replay asks you again.</p>
    </div>
  );
}

export function DemoStartView(props: DemoStartViewProps) {
  const { replays, environment, signedIn, pace, interview, previous, now } = props;
  const fixture = replays.data ? goldenFixture(replays.data.fixtures) : null;
  const blocked = replayBlockedNote(environment);
  return (
    <div className="demo">
      <PageHeader
        title="Guided Demo"
        description="Watch RECAP investigate a real recorded incident and fix it with your approval, one step at a time. It takes three to five minutes and needs no cluster and no paid model."
      />
      <DemoProvenance words={provenanceWords(fixture)} />

      <div className="demo-start-grid">
        <Card title="Start the demo" tone="accent">
          <PolledView
            polled={replays}
            loadingLabel="Loading the committed recordings…"
            errorTitle="Could not load the recordings (GET /replays)"
            emptyTitle="No recording to replay"
            empty={(view) => (view.fixtures.length ? null : (view.manifest_error ?? "fixtures/recordings/MANIFEST.json lists no committed recording."))}
          >
            {(view) =>
              fixture ? (
                <Recording fixture={fixture} />
              ) : (
                <Notice tone="warn" icon="alert" title="No recording can be replayed here">
                  {view.fixtures[0]?.not_launchable_because.join(" ") || "The server did not say why."}
                </Notice>
              )
            }
          </PolledView>
          <div className="demo-options">
            <div>
              <Segmented
                label="Pace"
                value={pace.key}
                options={DEMO_PACES.map((p) => ({ value: p.key, label: p.label }))}
                onChange={props.onPace}
              />
              <p className="small muted demo-option-hint">{pace.hint} Only the waiting changes, never what is replayed.</p>
            </div>
            <Toggle checked={interview} onChange={props.onInterview} label="Interview mode" hint="a presenter note at each step" />
          </div>
          {blocked ? (
            <Notice tone="warn" icon="alert" title="Replays are refused on this server's target">
              {blocked}
            </Notice>
          ) : null}
          {!signedIn ? <SignInPrompt what="start the demo and approve its fix" /> : null}
          <div className="demo-start-actions">
            <button
              type="button"
              className="btn btn-primary btn-large"
              disabled={!fixture || !signedIn || !!blocked || props.starting}
              onClick={() => fixture && props.onStart(fixture)}
            >
              <Icon name="play" size={15} />
              {props.starting ? "Starting…" : "Start the guided demo"}
            </button>
            <span className="muted small">It stops at step 7 and waits for your decision.</span>
          </div>
          {props.startError ? <ErrorView error={props.startError} title="Could not start the demo" /> : null}
        </Card>

        <Card title="What you will see" subtitle="Ten steps, each read from the run's own trace as it happens">
          <ol className="demo-preview">
            {DEMO_STEPS.map((step) => (
              <li key={step.id}>
                <span className="demo-preview-title">{step.title}</span>
                <span className="small muted">{step.about}</span>
              </li>
            ))}
          </ol>
        </Card>
      </div>

      {previous.length ? (
        <Card title="Earlier demo runs on this server" subtitle="Reviewing a finished run needs no sign-in">
          <ul className="demo-previous">
            {previous.map((run) => {
              const look = outcomeView(run.outcome);
              return (
                <li key={run.run_id}>
                  <a href={hrefFor("demo", { run: run.run_id, mode: interview ? "interview" : null })}>
                    <Mono>{run.run_id}</Mono>
                  </a>
                  <StatusToken tone={look.tone} icon={look.icon} label={look.label} size="sm" />
                  <span className="muted small">{fmtRelative(run.created_at, now)}</span>
                </li>
              );
            })}
          </ul>
        </Card>
      ) : null}
    </div>
  );
}

// ======================================================================================
// Page
// ======================================================================================

/** Earlier replays of the demo recording, newest first (at most five). */
export function previousDemoRuns(page: Page<dto.RunSummary> | undefined, fixture: dto.FixtureView | null): dto.RunSummary[] {
  if (!page || !fixture) return [];
  return page.items.filter((run) => run.replay?.fixture_id === fixture.id).slice(0, 5);
}

export function GuidedDemoPage({ route, system }: PageProps) {
  const runId = route.params.get("run");
  const interview = route.params.get("mode") === "interview";
  const pace = paceFor(route.params.get("pace"));
  const signedIn = useOperatorToken() !== null;
  const replays = usePolling(() => api.replays(), null, []);
  const runs = usePolling(runId ? null : () => api.runsPage({ limit: 100 }), null, [runId]);
  const now = useNow(30_000);
  const fixture = replays.data ? goldenFixture(replays.data.fixtures) : null;
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<ApiError | null>(null);

  /** A new replay of the demo recording (stopping the one followed now first, when asked), then follow it. */
  async function start(cancel: string | null) {
    if (!fixture) {
      setStartError(new ApiError("The demo's recording is not loaded yet (GET /replays): try again in a moment.", 0, "/replays", null, false));
      return;
    }
    setStarting(true);
    setStartError(null);
    try {
      // A stopped run's open approval request expires with it; a failed stop does not block a new run.
      if (cancel) await api.cancelRun(cancel).catch(() => undefined);
      const started = await api.startRun(replayRunBody(fixture, pace.value) as unknown as RunRequest);
      navigate("demo", demoParams(started.run_id, interview, pace));
    } catch (err) {
      setStartError(toApiError(err));
    } finally {
      setStarting(false);
    }
  }

  if (runId) {
    return (
      <>
        <PageHeader title="Guided Demo" description="A recorded real-model incident, investigated and fixed step by step. Choose any step to read about it." />
        <DemoRun
          key={runId}
          runId={runId}
          system={system}
          signedIn={signedIn}
          interview={interview}
          onInterview={(on) => navigate("demo", demoParams(runId, on, pace), true)}
          onRestart={(cancelFirst) => void start(cancelFirst ? runId : null)}
          restarting={starting}
          restartError={startError}
        />
      </>
    );
  }
  return (
    <DemoStartView
      replays={replays}
      environment={system.data?.environment}
      signedIn={signedIn}
      pace={pace}
      onPace={(key) => navigate("demo", demoParams(null, interview, paceFor(key)), true)}
      interview={interview}
      onInterview={(on) => navigate("demo", demoParams(null, on, pace), true)}
      starting={starting}
      startError={startError}
      onStart={() => void start(null)}
      previous={previousDemoRuns(runs.data, fixture)}
      now={now}
    />
  );
}
