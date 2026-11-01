import type { ReactNode } from "react";
import { api } from "../api";
import type { Page } from "../api";
import { Mono } from "../components/Badge";
import { Card, Notice, PageHeader } from "../components/Card";
import { DataSourceBadge, summarySource } from "../components/DataSourceBadge";
import { Icon } from "../components/Icon";
import { EmptyView, PolledView } from "../components/StateViews";
import { StateToken, StatusToken } from "../components/StatusToken";
import type * as dto from "../dto";
import { fmtDateTime, fmtInt, fmtMs, fmtRelative, stateLabel } from "../format";
import { hrefFor, useNow, useOperatorToken, usePolling } from "../hooks";
import type { PageProps, Polled } from "../hooks";
import "../styles/overview-architecture.css";
import {
  DOCS,
  OPERATOR_ACTIONS,
  SOURCE_LABELS,
  SOURCE_NOTES,
  accessFacts,
  activeRuns,
  adapterLook,
  adapterName,
  componentLook,
  keyStatus,
  livenessLook,
  orderProviders,
  outcomeLook,
  outcomesBySource,
  plannedGaps,
  providerReadiness,
  repoUrl,
  runDuration,
  scanNote,
  selectableLook,
  sessionLook,
  tallyComponents,
} from "../view/system";
import type { SessionState, SourceBreakdown } from "../view/system";

/**
 * Overview: the first screen. What this RECAP server has plugged in (GET /system), who can
 * drive its agents (GET /providers), what its newest runs did and how their outcomes split by
 * data source (GET /runs), what is running now, who may act, and what M5 still has to build.
 * Every value comes from the API; a field the server did not record reads "Not recorded".
 */

/** How many of the newest runs the outcome breakdown and active incidents read (GET /runs). */
export const RUN_SCAN = 500;
/** Rows in the recent-runs list. */
export const RECENT_RUNS = 10;

type Loaded<T> = Pick<Polled<T>, "data" | "error" | "loading" | "refresh">;

/** The Incident Lab's replay panel (and the recording to preselect, when known). */
export function demoHref(fixtureId?: string | null): string {
  return hrefFor("live", { panel: "replay", fixture: fixtureId ?? null });
}

function count(n: number, one: string, many = `${one}s`): string {
  return `${fmtInt(n)} ${n === 1 ? one : many}`;
}

function ExternalDoc({ path, children }: { path: string; children?: ReactNode }) {
  return (
    <a className="ov-doc-link" href={repoUrl(path)} target="_blank" rel="noopener noreferrer">
      {children ?? <Mono>{path}</Mono>}
      <span className="sr-only"> (opens on GitHub)</span>
    </a>
  );
}

// ======================================================================================
// Simulation notice and the active-incident pill
// ======================================================================================

function SimulationNotice({ system }: { system: dto.SystemView | undefined }) {
  if (!system) return null; // the shell's environment strip covers the wait
  if (system.environment.is_simulated) {
    return (
      <Notice tone="warn" icon="flask" title="Simulated environment · this page only reads">
        {system.simulation_notice ?? `${system.environment.name}: a simulated environment.`} Nothing on this page changes
        anything.
      </Notice>
    );
  }
  return (
    <Notice tone="bad" icon="alert" title={`Live environment adapter: ${system.environment_kind}`}>
      The server reports an environment that is not simulated: approved actions would change a real system.
    </Notice>
  );
}

/**
 * The header's one-line status of running incidents (the card below lists them). It is a
 * polite live region that stays in place, so a screen reader hears once when an incident
 * starts or waits for approval, not every time a relative time ticks.
 */
export function ActivePill({ runs }: { runs: Loaded<Page<dto.RunSummary>> }) {
  if (!runs.data) return null;
  const active = activeRuns(runs.data.items);
  const waiting = active.filter((run) => run.pending_approval).length;
  return (
    <span className="ov-pill-status" role="status">
      {active.length ? (
        <a className="ov-pill is-active" href={hrefFor("live", { run: active[0].run_id })}>
          <StatusToken
            tone={waiting ? "warn" : "accent"}
            icon={waiting ? "key" : "live"}
            label={`${count(active.length, "incident")} in progress${waiting ? ` · ${fmtInt(waiting)} awaiting approval` : ""}`}
            size="sm"
            variant="plain"
          />
        </a>
      ) : (
        <a className="ov-pill" href={hrefFor("live")}>
          <StatusToken tone="neutral" icon="minus" label="No incident running" size="sm" variant="plain" />
        </a>
      )}
    </span>
  );
}

// ======================================================================================
// Active incidents
// ======================================================================================

export function ActiveIncidents({ runs, now }: { runs: dto.RunSummary[]; now: number }) {
  const waiting = runs.filter((run) => run.pending_approval).length;
  return (
    <Card
      title={
        <span id="ov-active" className="ov-title-row">
          Active incidents <span className="ov-count-badge">{fmtInt(runs.length)}</span>
        </span>
      }
      subtitle="Runs that have not reached a final state. Open one in the Incident Lab to follow it live or decide its approval."
      tone={waiting ? "warn" : "accent"}
    >
      <ul className="ov-active-list">
        {runs.map((run) => {
          const live = livenessLook(run.status);
          return (
            <li key={run.run_id} className="ov-active">
              <div className="ov-active-main">
                <Mono>{run.scenario_id}</Mono>
                <span className="muted small">
                  <Mono>{run.run_id}</Mono> · started{" "}
                  <time dateTime={run.started_at} title={fmtDateTime(run.started_at)}>
                    {fmtRelative(run.started_at, now)}
                  </time>
                </span>
              </div>
              <div className="chip-row">
                <StateToken state={run.state} size="sm" />
                {run.pending_approval && run.state !== "AWAITING_APPROVAL" ? (
                  <StatusToken tone="warn" icon="key" label="Approval request open" size="sm" />
                ) : null}
                <StatusToken tone={live.tone} icon={live.icon} label={live.label} title={live.title} size="sm" variant="outline" />
                <DataSourceBadge {...summarySource(run)} size="sm" />
              </div>
              <a className="btn btn-small" href={hrefFor("live", { run: run.run_id })}>
                Open in the Incident Lab <Icon name="chevronRight" size={13} />
              </a>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

// ======================================================================================
// Start the demo
// ======================================================================================

/** The recording the demo starts from: the launchable "golden" one, else the first launchable. */
export function demoFixture(view: dto.ReplaysView): dto.FixtureView | null {
  const launchable = view.fixtures.filter((f) => f.launchable);
  return launchable.find((f) => f.tags.includes("golden")) ?? launchable[0] ?? null;
}

function DemoFixture({ view, system }: { view: dto.ReplaysView; system: dto.SystemView | undefined }) {
  const fixture = demoFixture(view);
  const launchable = view.fixtures.filter((f) => f.launchable).length;
  if (!fixture) {
    const first = view.fixtures[0];
    return (
      <div className="ov-demo-fixture">
        <StatusToken tone="warn" icon="alert" label="No recording can be replayed here" size="sm" />
        {first?.not_launchable_because.length ? (
          <ul className="ov-plain-list small">
            {first.not_launchable_because.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        ) : null}
      </div>
    );
  }
  return (
    <div className="ov-demo-fixture">
      <DataSourceBadge
        provider={{ origin: "replay", kind: "replay", model: fixture.model, replay_of: { original_provider: fixture.provider, model: fixture.model } }}
        environment={system ? system.environment : undefined}
        size="sm"
        label="Demo data source"
      />
      <div className="ov-demo-title">{fixture.title}</div>
      <div className="ov-demo-meta">
        <span>{fixture.agents}</span>
        <span>
          scenario <Mono>{fixture.scenario}</Mono>
        </span>
        <span>
          recorded with <Mono>{fixture.prompt_version}</Mono>
        </span>
      </div>
      <p className="ov-demo-about">{fixture.description}</p>
      <dl className="ov-demo-facts">
        <div>
          <dt>Original fix approved by</dt>
          <dd>{fixture.approved_by}. The replay asks you again.</dd>
        </div>
        {fixture.exported_run ? (
          <div>
            <dt>The original run's export</dt>
            <dd>
              <ExternalDoc path={fixture.exported_run} />
            </dd>
          </div>
        ) : null}
      </dl>
      <div className="chip-row">
        <StatusToken
          tone={fixture.integrity === "intact" ? "ok" : "bad"}
          icon={fixture.integrity === "intact" ? "check" : "x"}
          label={fixture.integrity === "intact" ? "Recording intact" : `Recording ${fixture.integrity}`}
          title={`SHA-256 ${fixture.sha256}… checked against the manifest`}
          size="sm"
          variant="outline"
        />
        <span className="muted small">
          {fmtInt(launchable)} of {count(view.fixtures.length, "committed recording")} can be replayed here
        </span>
      </div>
    </div>
  );
}

export function DemoCard({ replays, system }: { replays: Loaded<dto.ReplaysView>; system: dto.SystemView | undefined }) {
  const fixture = replays.data ? demoFixture(replays.data) : null;
  return (
    <Card title="Start the demo" subtitle="Replay a recorded real-model incident, end to end" tone="accent">
      <p className="ov-lead">
        The recorded answers of an earlier real-model run drive RECAP against the simulated shop: no model is called now, every
        step shows up live, and you approve the fix yourself.
      </p>
      <PolledView
        polled={replays}
        loadingLabel="Loading the committed recordings…"
        errorTitle="Could not load the recordings (GET /replays)"
        emptyTitle="No recording to replay"
        empty={(view) =>
          view.fixtures.length ? null : (view.manifest_error ?? "fixtures/recordings/MANIFEST.json lists no committed recording.")
        }
      >
        {(view) => <DemoFixture view={view} system={system} />}
      </PolledView>
      <div className="ov-demo-actions">
        <a className="btn btn-primary btn-large" href={demoHref(fixture?.id)}>
          <Icon name="play" size={15} />
          Open the replay panel
        </a>
        <span className="muted small">Starting a replay needs the operator token (Sign in, top right).</span>
      </div>
      <p className="ov-demo-guide">
        Presenting it? The 90 to 180 second walkthrough: <ExternalDoc path={DOCS.demoWalkthrough} />
      </p>
    </Card>
  );
}

// ======================================================================================
// System readiness
// ======================================================================================

export function Readiness({ system }: { system: dto.SystemView }) {
  const tally = tallyComponents(system.components);
  const v = system.versions;
  return (
    <>
      <div className="ov-section">
        <h3 className="mini-title">Environment adapters</h3>
        <ul className="ov-adapters">
          {system.environment_adapters.map((adapter) => {
            const look = adapterLook(adapter);
            return (
              <li key={adapter.kind} className={adapter.status === "planned" ? "ov-adapter is-planned" : "ov-adapter"}>
                <div className="ov-adapter-head">
                  <span className="ov-adapter-name">{adapterName(adapter.kind)}</span>
                  {adapter.configured ? <span className="ov-tag">configured</span> : null}
                  <span className="ov-adapter-status chip-row">
                    <StatusToken tone={look.tone} icon={look.icon} label={look.label} title={look.title} size="sm" />
                    <StatusToken
                      tone={adapter.operational ? "ok" : "neutral"}
                      icon={adapter.operational ? "check" : "minus"}
                      label={adapter.operational ? "Operational" : "Not operational"}
                      size="sm"
                      variant="outline"
                    />
                  </span>
                </div>
                <div className="ov-adapter-detail">{adapter.detail}</div>
              </li>
            );
          })}
        </ul>
      </div>

      <div className="ov-section">
        <h3 className="mini-title">Components</h3>
        {system.components.length ? (
          <>
            <p className="ov-tally">
              {tally.counts.map(({ state, count: n }, i) => {
                const look = componentLook(state);
                return (
                  <span key={state} className="ov-tally-item">
                    {i ? <span className="ov-sep" aria-hidden="true">·</span> : null}
                    <StatusToken tone={look.tone} icon={look.icon} label={`${fmtInt(n)} ${look.label.toLowerCase()}`} title={look.title} size="sm" variant="plain" />
                  </span>
                );
              })}
            </p>
            {tally.notActive.length ? (
              <ul className="ov-components">
                {tally.notActive.map((c) => {
                  const look = componentLook(c.status);
                  return (
                    <li key={c.id}>
                      <span className="ov-component-name">
                        {c.implementation} <span className="muted small">({c.interface})</span>
                      </span>
                      <StatusToken tone={look.tone} icon={look.icon} label={look.label} title={look.title} size="sm" />
                      {c.detail ? <span className="ov-component-detail">{c.detail}</span> : null}
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="small muted">Every component this server lists is active.</p>
            )}
          </>
        ) : (
          <p className="small muted">This server does not report component statuses.</p>
        )}
        <a className="link small" href={hrefFor("architecture")}>
          How the components connect: Architecture & Learning <Icon name="chevronRight" size={12} />
        </a>
      </div>

      <p className="ov-versions">
        <span className="ov-versions-label">Versions</span>
        <span>
          RECAP <Mono>{v.recap}</Mono>
        </span>
        <span>
          prompts <Mono>{v.prompt_version}</Mono>
        </span>
        <span>
          policy <Mono>{v.policy_version}</Mono>
        </span>
        <span>
          API <Mono>{v.api_schema}</Mono>
        </span>
        <span>
          code <Mono>{v.code_version}</Mono>
        </span>
      </p>
    </>
  );
}

function ReadinessCard({ system }: { system: Loaded<dto.SystemView> }) {
  return (
    <Card title="System readiness" subtitle="What this server has plugged in, and what is only planned (GET /system)">
      <PolledView polled={system} loadingLabel="Loading GET /system…" errorTitle="Could not load GET /system">
        {(data) => <Readiness system={data} />}
      </PolledView>
    </Card>
  );
}

// ======================================================================================
// Recent runs
// ======================================================================================

function Duration({ run }: { run: dto.RunSummary }) {
  const duration = runDuration(run);
  if (duration.kind === "measured") return <span className="ov-num">{fmtMs(duration.ms)}</span>;
  if (duration.kind === "running") return <span className="muted">running</span>;
  return <span className="not-recorded">Not recorded</span>;
}

export function RecentRuns({ runs, now }: { runs: dto.RunSummary[]; now: number }) {
  return (
    <div className="ov-runs">
      <div className="ov-runs-head" aria-hidden="true">
        <span>Started</span>
        <span>Scenario · run</span>
        <span>Outcome</span>
        <span>Data source</span>
        <span className="ov-right">Duration</span>
      </div>
      <ol className="ov-runs-list">
        {runs.map((run) => {
          const outcome = outcomeLook(run.outcome);
          return (
            <li key={run.run_id} className="ov-run">
              <a className="ov-run-link" href={hrefFor("timeline", { run: run.run_id })}>
                <Mono>{run.scenario_id}</Mono>
                <span className="sr-only">, open run {run.run_id} in the Trace Explorer</span>
              </a>
              <span className="ov-run-id muted small">
                <Mono>{run.run_id}</Mono>
                {run.label ? <span className="ov-run-label">{run.label}</span> : null}
              </span>
              <span className="ov-run-time small">
                <span className="sr-only">Started </span>
                <time dateTime={run.started_at} title={fmtDateTime(run.started_at)}>
                  {fmtRelative(run.started_at, now)}
                </time>
              </span>
              <span className="ov-run-outcome">
                <span className="sr-only">Outcome: </span>
                <StatusToken
                  tone={outcome.tone}
                  icon={outcome.icon}
                  label={outcome.label}
                  title={outcome.title}
                  detail={run.terminal ? undefined : stateLabel(run.state)}
                  size="sm"
                />
              </span>
              <span className="ov-run-source">
                <DataSourceBadge {...summarySource(run)} size="sm" />
              </span>
              <span className="ov-run-duration small">
                <span className="sr-only">Duration: </span>
                <Duration run={run} />
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function RecentRunsCard({ runs, now }: { runs: Loaded<Page<dto.RunSummary>>; now: number }) {
  const page = runs.data;
  const shown = page ? Math.min(RECENT_RUNS, page.items.length) : 0;
  const subtitle = page
    ? page.total !== null
      ? `The ${fmtInt(shown)} newest of ${count(page.total, "run")}. Select a run to open it in the Trace Explorer.`
      : `The ${fmtInt(shown)} newest runs. Select a run to open it in the Trace Explorer.`
    : "The newest runs (GET /runs)";
  return (
    <Card
      title="Recent runs"
      subtitle={subtitle}
      actions={
        <a className="link small" href={hrefFor("timeline")}>
          Trace Explorer <Icon name="chevronRight" size={12} />
        </a>
      }
      flush
    >
      <div className="ov-flush-states">
        <PolledView
          polled={runs}
          loadingLabel="Loading runs…"
          errorTitle="Could not load runs (GET /runs)"
          emptyTitle="No runs yet"
          emptyIcon="live"
          empty={(p) =>
            p.items.length
              ? null
              : "This server's data folder holds no run yet. Start the demo above, or run a scenario in the Incident Lab."
          }
        >
          {(p) => <RecentRuns runs={p.items.slice(0, RECENT_RUNS)} now={now} />}
        </PolledView>
      </div>
    </Card>
  );
}

// ======================================================================================
// Outcomes by data source
// ======================================================================================

function SourceColumn({ group }: { group: SourceBreakdown }) {
  return (
    <div className={group.total ? "ov-source" : "ov-source is-empty"}>
      <h3 className="ov-source-title">
        <span className="sr-only">{SOURCE_LABELS[group.source]}</span>
        <span aria-hidden="true">
          <DataSourceBadge provider={group.source === "unknown" ? null : { origin: group.source }} size="sm" label={SOURCE_LABELS[group.source]} />
        </span>
      </h3>
      <div className="ov-source-total">
        <span className="ov-big">{fmtInt(group.total)}</span> {group.total === 1 ? "run" : "runs"}
        {group.total ? <span className="muted small"> · {count(group.scenarios, "scenario")}</span> : null}
      </div>
      {group.total ? (
        <ul className="ov-outcomes">
          {group.outcomes.map(({ outcome, count: n }) => {
            const look = outcomeLook(outcome);
            return (
              <li key={outcome}>
                <StatusToken tone={look.tone} icon={look.icon} label={look.label} title={look.title} size="sm" variant="plain" />
                <span className="ov-num">{fmtInt(n)}</span>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="ov-source-empty">No runs from this source yet.</p>
      )}
      <p className="ov-source-note">
        {SOURCE_NOTES[group.source]}
        {group.source === "replay" && group.recordings !== null && group.total > 0
          ? ` These ${count(group.total, "replay")} re-ran ${count(group.recordings, "recording")}: a recording replayed again is not a new model result.`
          : ""}
      </p>
    </div>
  );
}

export function OutcomeBreakdown({ page }: { page: Page<dto.RunSummary> }) {
  const groups = outcomesBySource(page.items);
  const note = scanNote(page.items.length, page.total);
  return (
    <>
      <div className="ov-sources">
        {groups.map((group) => (
          <SourceColumn key={group.source} group={group} />
        ))}
      </div>
      <p className="ov-footnote">
        Each figure counts runs; none is a success rate, and the sources are never added together. Resolved means the
        independent recovery oracle verified the recovery.{note ? ` ${note}` : ""}
      </p>
    </>
  );
}

function OutcomesCard({ runs }: { runs: Loaded<Page<dto.RunSummary>> }) {
  return (
    <Card
      title="Outcomes by data source"
      subtitle="How runs ended, counted separately for each kind of decision source: a scripted run, a replay and a new model run are different kinds of evidence"
      actions={
        <a className="link small" href={hrefFor("evals")}>
          Evaluations <Icon name="chevronRight" size={12} />
        </a>
      }
    >
      <PolledView
        polled={runs}
        loadingLabel="Counting runs…"
        errorTitle="Could not load runs (GET /runs)"
        emptyTitle="Nothing to count yet"
        empty={(p) => (p.items.length ? null : "No run has been recorded, so there is no outcome to count.")}
      >
        {(p) => <OutcomeBreakdown page={p} />}
      </PolledView>
    </Card>
  );
}

// ======================================================================================
// Access and M5 gaps
// ======================================================================================

export function AccessFacts({ system, session }: { system: dto.SystemView; session: SessionState }) {
  const facts = accessFacts(system.features);
  const you = sessionLook(session);
  return (
    <>
      <dl className="ov-facts">
        <div className="ov-fact">
          <dt>Reading every page</dt>
          <dd>
            <StatusToken tone={facts.reads.tone} icon={facts.reads.icon} label={facts.reads.label} title={facts.reads.title} size="sm" />
          </dd>
        </div>
        <div className="ov-fact">
          <dt>Acting: {OPERATOR_ACTIONS.join(", ")}</dt>
          <dd>
            <StatusToken tone={facts.writes.tone} icon={facts.writes.icon} label={facts.writes.label} title={facts.writes.title} size="sm" />
          </dd>
        </div>
        <div className="ov-fact">
          <dt>This browser tab</dt>
          <dd>
            <span role="status">
              <StatusToken tone={you.tone} icon={you.icon} label={you.label} title={you.title} size="sm" />
            </span>
          </dd>
        </div>
      </dl>
      <p className="small muted">
        {session.kind === "signed_in"
          ? "Your decisions are recorded under this operator. The token stays in this tab and is never shown."
          : "Sign in (top right) with the operator token the API printed when it started; it is also in data/operator-token. The token stays in this tab and is never shown."}
      </p>
    </>
  );
}

function AccessCard({ system, session }: { system: Loaded<dto.SystemView>; session: SessionState }) {
  return (
    <Card title="Access" subtitle="Reading needs no token; acting needs the operator token">
      <PolledView polled={system} loadingLabel="Loading GET /system…" errorTitle="Could not load GET /system">
        {(data) => <AccessFacts system={data} session={session} />}
      </PolledView>
    </Card>
  );
}

export function M5Gaps({ system }: { system: dto.SystemView }) {
  const gaps = plannedGaps(system);
  return (
    <>
      {gaps.length ? (
        <ul className="ov-gaps">
          {gaps.map((gap) => (
            <li key={gap.id} className="ov-gap">
              <div className="ov-gap-head">
                <span className="ov-gap-title">{gap.title}</span>
                <StatusToken tone={gap.look.tone} icon={gap.look.icon} label={gap.look.label} title={gap.look.title} size="sm" />
              </div>
              <div className="ov-gap-detail">{gap.detail}</div>
            </li>
          ))}
        </ul>
      ) : (
        <EmptyView title="No planned item listed" reason="GET /system reports no planned adapter, component or evaluation mode." icon="info" />
      )}
      <p className="small">
        The plan: <ExternalDoc path={DOCS.m5Plan} />. It is documentation only: nothing in it is built, installed or provisioned.
      </p>
    </>
  );
}

function GapsCard({ system }: { system: Loaded<dto.SystemView> }) {
  return (
    <Card title="Not built yet: M5" subtitle="Real-cluster targets the server lists as planned; none can run an incident">
      <PolledView polled={system} loadingLabel="Loading GET /system…" errorTitle="Could not load GET /system">
        {(data) => <M5Gaps system={data} />}
      </PolledView>
    </Card>
  );
}

// ======================================================================================
// Decision providers
// ======================================================================================

function freeTierLine(p: dto.ProviderView): string | null {
  if (p.provider_class !== "free_hosted_llm") return null;
  const parts: string[] = [];
  if (p.opted_in !== null) parts.push(`owner opt-in: ${p.opted_in ? "yes" : "no"}`);
  if (p.confirmation_fresh !== null) {
    parts.push(
      p.confirmed_on
        ? `free-plan confirmation: ${p.confirmed_on}${p.confirmation_fresh ? "" : " (too old)"}`
        : "free-plan confirmation: missing",
    );
  }
  return parts.length ? parts.join(" · ") : null;
}

/**
 * The model under a provider's class badge (a replay: the model its recording captured, from
 * `replay_of`), and its call mode. Nothing when the server names neither (the scripted policy).
 */
function ModelLine({ provider: p }: { provider: dto.ProviderView }) {
  const recorded = typeof p.replay_of?.model === "string" && p.replay_of.model.trim() ? p.replay_of.model : null;
  const model = p.model ?? recorded;
  if (!model && !p.mode) return null;
  return (
    <span className="ov-provider-note">
      {model ? (
        <>
          {p.model ? "model" : "recorded model"} <Mono>{model}</Mono>
        </>
      ) : null}
      {model && p.mode ? " · " : null}
      {p.mode ? `${p.mode} mode` : null}
    </span>
  );
}

export function ProviderRow({ provider: p }: { provider: dto.ProviderView }) {
  const ready = providerReadiness(p.status);
  const select = selectableLook(p.selectable);
  const key = keyStatus(p);
  const free = freeTierLine(p);
  const usage = p.usage;
  return (
    <li className="ov-provider">
      <div className="ov-provider-id">
        <span className="ov-provider-name">
          <Mono>{p.name}</Mono>
          {p.default ? <StatusToken tone="accent" icon="dot" label="Server default" size="sm" variant="outline" /> : null}
        </span>
        <span className="ov-provider-desc">{p.description}</span>
      </div>
      <div className="ov-provider-col">
        <span className="ov-col-label">Class</span>
        {/* The class alone keeps the badge compact in this narrow column; the model has its own line. */}
        <DataSourceBadge provider={{ origin: p.provider_class, kind: p.kind }} size="sm" label={`${p.name}: class`} />
        <ModelLine provider={p} />
      </div>
      <div className="ov-provider-col">
        <span className="ov-col-label">Readiness</span>
        <StatusToken tone={ready.tone} icon={ready.icon} label={ready.label} title={ready.title} size="sm" />
        {p.detail ? <span className="ov-provider-note">{p.detail}</span> : null}
        {key ? <span className="ov-provider-note">{key}</span> : null}
        {free ? <span className="ov-provider-note">{free}</span> : null}
      </div>
      <div className="ov-provider-col">
        <span className="ov-col-label">For one run</span>
        <StatusToken tone={select.tone} icon={select.icon} label={select.label} title={select.title} size="sm" variant="outline" />
        <span className="ov-provider-note">{p.selection_rule}</span>
      </div>
      {usage || p.limitations.length ? (
        <div className="ov-provider-foot">
          {usage ? (
            <span className="ov-provider-note">
              Last 24 h (RECAP's usage ledger):{" "}
              {typeof usage.requests_last_24h === "number" ? count(usage.requests_last_24h, "request") : "requests not recorded"}
              {typeof usage.tokens_last_24h === "number" ? ` · ${count(usage.tokens_last_24h, "token")} reported` : ""}
            </span>
          ) : null}
          {p.limitations.length ? (
            <details className="ov-details">
              <summary>Limitations ({fmtInt(p.limitations.length)})</summary>
              <ul className="ov-plain-list small">
                {p.limitations.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </details>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

function ProvidersCard({ providers }: { providers: Loaded<dto.ProviderView[]> }) {
  return (
    <Card
      title="Decision providers"
      subtitle="Who can drive the agents, whether each could run now, and whether a run may pick it (GET /providers). API keys are only ever reported as set or missing."
    >
      <PolledView
        polled={providers}
        loadingLabel="Loading GET /providers…"
        errorTitle="Could not load GET /providers"
        emptyTitle="No provider configured"
        empty={(list) => (list.length ? null : "configs/providers.yaml lists no decision provider.")}
      >
        {(list) => (
          <ul className="ov-providers">
            {orderProviders(list).map((p) => (
              <ProviderRow key={p.name} provider={p} />
            ))}
          </ul>
        )}
      </PolledView>
    </Card>
  );
}

// ======================================================================================
// The page
// ======================================================================================

export interface OverviewViewProps {
  system: Loaded<dto.SystemView>;
  providers: Loaded<dto.ProviderView[]>;
  runs: Loaded<Page<dto.RunSummary>>;
  replays: Loaded<dto.ReplaysView>;
  session: SessionState;
  now: number;
}

/** The whole Overview for given server answers (the page polls them; tests pass them). */
export function OverviewView({ system, providers, runs, replays, session, now }: OverviewViewProps) {
  const active = runs.data ? activeRuns(runs.data.items) : [];
  const fixture = replays.data ? demoFixture(replays.data) : null;
  return (
    <div className="ov">
      <PageHeader
        title="Overview"
        description="RECAP's readiness, the providers that can drive its agents, and what its recent runs did. Every value on this page comes from the RECAP API."
        actions={
          <div className="ov-header-actions">
            <ActivePill runs={runs} />
            <a className="btn btn-primary" href={demoHref(fixture?.id)}>
              <Icon name="play" size={14} />
              Start the demo
            </a>
          </div>
        }
      />

      <SimulationNotice system={system.data} />

      {active.length ? <ActiveIncidents runs={active} now={now} /> : null}

      <div className="ov-grid">
        <DemoCard replays={replays} system={system.data} />
        <ReadinessCard system={system} />
      </div>

      <RecentRunsCard runs={runs} now={now} />

      <OutcomesCard runs={runs} />

      <div className="ov-grid">
        <AccessCard system={system} session={session} />
        <GapsCard system={system} />
      </div>

      <ProvidersCard providers={providers} />
    </div>
  );
}

/** The sign-in state of this tab, from the stored token and GET /auth/session. */
export function sessionState(token: string | null, session: Loaded<{ operator: string }>): SessionState {
  if (!token) return { kind: "signed_out" };
  if (session.data) return { kind: "signed_in", operator: session.data.operator };
  if (session.error) {
    return session.error.status === 401 ? { kind: "rejected" } : { kind: "unverified", message: session.error.message };
  }
  return { kind: "checking" };
}

/** Polls GET /runs faster while an incident is in progress. */
function runsInterval(page: Page<dto.RunSummary> | undefined): number {
  return page && page.items.some((run) => !run.terminal) ? 4_000 : 15_000;
}

export function OverviewPage(_props: PageProps) {
  const system = usePolling(() => api.systemView(), 30_000, []);
  const providers = usePolling(() => api.providerViews(), 30_000, []);
  const runs = usePolling(() => api.runsPage({ limit: RUN_SCAN }), runsInterval, []);
  const replays = usePolling(() => api.replays(), 60_000, []);
  const token = useOperatorToken();
  // Re-checked when the tab signs in or out (and every minute: the API may have a new token).
  const session = usePolling(token ? () => api.session() : null, 60_000, [token !== null]);
  const now = useNow(15_000);
  return (
    <OverviewView
      system={system}
      providers={providers}
      runs={runs}
      replays={replays}
      session={sessionState(token, session)}
      now={now}
    />
  );
}
