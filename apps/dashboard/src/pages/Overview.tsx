import type { ReactNode } from "react";
import { api } from "../api";
import { Chip, Mono, SimulatedBadge, StateChip } from "../components/Badge";
import { Card, PageHeader, StatTile } from "../components/Card";
import { EmptyState, ErrorState, Loading, NoRuns } from "../components/EmptyState";
import { Icon } from "../components/Icon";
import type { IconName } from "../components/Icon";
import { DataTable } from "../components/Table";
import { fmtInt, fmtRelative, isTerminal, providerLabel, stateLabel, stateTone } from "../format";
import { hrefFor, navigate, useNow, usePolling } from "../hooks";
import type { PageProps } from "../hooks";
import type { EvalReport, RunSummary, SystemSummary } from "../types";

const FINAL_STATES = ["RESOLVED", "BLOCKED", "FAILED", "BUDGET_EXHAUSTED", "QUOTA_EXHAUSTED"] as const;

function SystemCard({ icon, label, value, detail }: { icon: IconName; label: string; value: ReactNode; detail?: ReactNode }) {
  return (
    <div className="sys-card">
      <div className="sys-label">
        <Icon name={icon} size={14} />
        {label}
      </div>
      <div className="sys-value">{value}</div>
      {detail ? <div className="sys-detail">{detail}</div> : null}
    </div>
  );
}

function SystemCards({ system }: { system: SystemSummary }) {
  const env = system.environment;
  return (
    <div className="sys-grid">
      <SystemCard
        icon="architecture"
        label="Environment adapter"
        value={
          <>
            <Mono>{system.environment_kind}</Mono> {env.is_simulated ? <SimulatedBadge /> : <Chip tone="bad">real system</Chip>}
          </>
        }
        detail={`${env.name} · namespace ${env.namespace}`}
      />
      <SystemCard
        icon="cpu"
        label="Decision provider"
        value={<Mono>{system.provider.name}</Mono>}
        detail={system.provider.origin === "scripted" || system.provider.kind === "scripted" ? "scripted policy — not an LLM, $0 cost" : providerLabel(system.provider)}
      />
      <SystemCard
        icon="book"
        label="Retriever (RAG)"
        value={<Mono>{system.retriever}</Mono>}
        detail={`${fmtInt(system.knowledge_chunks)} knowledge chunks indexed`}
      />
      <SystemCard
        icon="memory"
        label="Experience memory"
        value={`${fmtInt(system.memory_episodes)} episodes`}
        detail="successes only when the oracle verified recovery"
      />
      <SystemCard icon="route" label="Router" value={<Mono>{system.router}</Mono>} detail="single vs parallel from signals + budget" />
      <SystemCard icon="shield" label="Safety policy" value={<Mono>{system.safety_policy}</Mono>} detail="trusted code at the tool gateway" />
      <SystemCard icon="heart" label="Recovery oracle" value={<Mono>{system.oracle}</Mono>} detail="judges recovery from environment state" />
      <SystemCard icon="tool" label="Tool transport" value={system.tool_transport} />
    </div>
  );
}

function EvalHeadline({ report }: { report: EvalReport }) {
  const summaries = report.summaries ?? [];
  const tests = summaries.filter((s) => s.suite === "test");
  const main = tests.find((s) => s.name === "full_adaptive") ?? tests[0];
  const redteam = summaries.find((s) => s.name === "redteam_safety_on");
  return (
    <div className="eval-headline">
      <Chip tone="warn" outline>
        synthetic benchmark · simulated · small N
      </Chip>
      {main ? (
        <div className="eval-headline-main">
          <Mono>{main.name}</Mono>: outcome as expected{" "}
          <strong>
            {main.outcome_as_expected}/{main.n}
          </strong>{" "}
          · oracle-verified recovery{" "}
          <strong>
            {main.recovered}/{main.n}
          </strong>{" "}
          · correct diagnosis{" "}
          <strong>
            {main.diagnosis_correct}/{main.n}
          </strong>
        </div>
      ) : (
        <div className="muted">The report has no test-suite configurations.</div>
      )}
      {redteam ? (
        <div className="muted">
          Red team (<Mono>{redteam.name}</Mono>): {redteam.unsafe_attempts} unsafe attempt{redteam.unsafe_attempts === 1 ? "" : "s"},{" "}
          {redteam.unsafe_blocked} blocked, {redteam.unsafe_executed} executed
          {redteam.unsafe_refused ? `, ${redteam.unsafe_refused} refused by the sandbox` : ""}.
        </div>
      ) : null}
      <div className="muted">
        Report <Mono>{report.report_id}</Mono> · {fmtRelative(report.created_at)} · provider <Mono>{report.provider}</Mono>
      </div>
      <a className="link" href={hrefFor("evals")}>
        Open Evaluations <Icon name="chevronRight" size={12} />
      </a>
    </div>
  );
}

function RunCounts({ runs }: { runs: RunSummary[] }) {
  const count = (state: string) => runs.filter((r) => r.state === state).length;
  const active = runs.filter((r) => !isTerminal(r.state)).length;
  const waiting = runs.filter((r) => r.pending_approval).length;
  return (
    <div className="stat-grid">
      {FINAL_STATES.map((s) => (
        <StatTile key={s} label={stateLabel(s)} value={fmtInt(count(s))} tone={stateTone(s)} />
      ))}
      <StatTile label="In progress" value={fmtInt(active)} tone="accent" note={waiting ? `${waiting} awaiting approval` : undefined} />
    </div>
  );
}

export function OverviewPage({ system }: PageProps) {
  const runs = usePolling(() => api.runs(200), 5000, []);
  const evals = usePolling(() => api.evalResults(), 20000, []);
  const now = useNow(15000);
  const list = runs.data ?? [];

  return (
    <>
      <PageHeader
        title="Overview"
        description="What is plugged in right now, and what RECAP has done. Every number on this page comes from the API."
      />

      <Card title="Live wiring" subtitle="GET /system — the concrete implementation behind each interface">
        {system.data ? (
          <SystemCards system={system.data} />
        ) : system.error ? (
          <ErrorState error={system.error} />
        ) : (
          <Loading />
        )}
      </Card>

      <div className="grid-2">
        <Card title="Runs by final state" subtitle={runs.data ? `latest ${list.length} runs from GET /runs` : undefined}>
          {runs.error && !runs.data ? <ErrorState error={runs.error} /> : runs.data ? <RunCounts runs={list} /> : <Loading />}
        </Card>
        <Card title="Latest evaluation" subtitle="GET /evals/results">
          {evals.data?.report ? (
            <EvalHeadline report={evals.data.report} />
          ) : evals.error && !evals.data ? (
            <ErrorState error={evals.error} />
          ) : evals.data ? (
            <EmptyState
              title="No evaluation report yet"
              icon="evals"
              action={
                <a className="btn" href={hrefFor("evals")}>
                  Open Evaluations
                </a>
              }
            >
              {evals.data.running
                ? "An evaluation is running now."
                : evals.data.error
                  ? `The last evaluation failed: ${evals.data.error}`
                  : "Run the synthetic benchmark from the Evaluations page."}
            </EmptyState>
          ) : (
            <Loading />
          )}
        </Card>
      </div>

      <Card title="Recent runs" subtitle="Click a run to open its agent timeline" flush>
        {runs.error && !runs.data ? (
          <ErrorState error={runs.error} />
        ) : !runs.data ? (
          <Loading />
        ) : list.length === 0 ? (
          <NoRuns />
        ) : (
          <DataTable
            rows={list.slice(0, 15)}
            rowKey={(r) => r.run_id}
            onRowClick={(r) => navigate("timeline", { run: r.run_id })}
            columns={[
              {
                key: "scenario",
                header: "Scenario",
                render: (r) => (
                  <span className="cell-stack">
                    <Mono>{r.scenario_id}</Mono>
                    {r.label ? <span className="muted">{r.label}</span> : null}
                  </span>
                ),
              },
              { key: "state", header: "State", render: (r) => <StateChip state={r.state} /> },
              {
                key: "modes",
                header: "Routing",
                render: (r) =>
                  r.modes.length ? (
                    <span className="chip-row">
                      {r.modes.map((m, i) => (
                        <Chip key={i} tone="accent" mono>
                          {m}
                        </Chip>
                      ))}
                    </span>
                  ) : (
                    <span className="muted">—</span>
                  ),
              },
              {
                key: "recovered",
                header: "Recovered",
                render: (r) =>
                  r.recovered ? (
                    <Chip tone="ok">
                      <Icon name="check" size={12} /> oracle-verified
                    </Chip>
                  ) : (
                    <span className="muted">no</span>
                  ),
              },
              { key: "tools", header: "Tool calls", numeric: true, render: (r) => fmtInt(r.tool_calls) },
              {
                key: "pending",
                header: "",
                render: (r) => (r.pending_approval ? <Chip tone="warn">awaiting approval</Chip> : null),
              },
              { key: "time", header: "Started", render: (r) => <span title={r.created_at}>{fmtRelative(r.created_at, now)}</span> },
              { key: "id", header: "Run", nowrap: true, render: (r) => <Mono>{r.run_id}</Mono> },
            ]}
          />
        )}
      </Card>
    </>
  );
}
