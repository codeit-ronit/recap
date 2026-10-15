import { useMemo, useState } from "react";
import { api, toApiError } from "../api";
import type { ApiError } from "../api";
import { Chip, Mono, StateChip, YesNoChip } from "../components/Badge";
import { Card, KeyValues, Meter, Notice, PageHeader } from "../components/Card";
import { EmptyState, ErrorState, Loading } from "../components/EmptyState";
import { Icon } from "../components/Icon";
import { DataTable } from "../components/Table";
import { fmtDateTime, fmtInt, fmtNum } from "../format";
import { usePolling } from "../hooks";
import type { CaseResult, ConfigSummary, EvalReport } from "../types";

function Ratio({ value, n }: { value: number; n: number }) {
  return <Meter value={value} max={n} label={`${value}/${n}`} width={48} />;
}

function TestSuiteTable({ rows }: { rows: ConfigSummary[] }) {
  return (
    <DataTable
      rows={rows}
      rowKey={(s) => s.name}
      className="dense wrap-head"
      empty="No test-suite configurations in this report."
      columns={[
        {
          key: "name",
          header: "Configuration",
          render: (s) => (
            <span className="cell-stack">
              <Mono>{s.name}</Mono>
              <span className="muted small">{s.description}</span>
            </span>
          ),
          width: "27%",
        },
        { key: "n", header: "N", numeric: true, render: (s) => s.n },
        { key: "expected", header: "Outcome as expected", render: (s) => <Ratio value={s.outcome_as_expected} n={s.n} /> },
        { key: "recovered", header: "Oracle-verified recovery", render: (s) => <Ratio value={s.recovered} n={s.n} /> },
        { key: "diagnosis", header: "Correct diagnosis", render: (s) => <Ratio value={s.diagnosis_correct} n={s.n} /> },
        {
          key: "tools",
          header: "Mean tool calls · steps",
          numeric: true,
          nowrap: true,
          render: (s) => `${fmtNum(s.mean_tool_calls)} · ${fmtNum(s.mean_steps)}`,
        },
        { key: "tokens", header: "Mean est. tokens", numeric: true, render: (s) => fmtInt(s.mean_estimated_tokens) },
        { key: "latency", header: "Mean latency (ms)", numeric: true, render: (s) => fmtInt(s.mean_latency_ms) },
        { key: "parallel", header: "Parallel rounds", numeric: true, render: (s) => s.parallel_rounds },
      ]}
    />
  );
}

function RedTeamTable({ rows }: { rows: ConfigSummary[] }) {
  return (
    <DataTable
      rows={rows}
      rowKey={(s) => s.name}
      rowClassName={(s) => (s.unsafe_executed > 0 ? "row-bad" : undefined)}
      className="dense wrap-head"
      empty="No red-team configurations in this report."
      columns={[
        {
          key: "name",
          header: "Configuration",
          render: (s) => (
            <span className="cell-stack">
              <Mono>{s.name}</Mono>
              <span className="muted small">{s.description}</span>
            </span>
          ),
          width: "40%",
        },
        { key: "n", header: "N", numeric: true, render: (s) => s.n },
        { key: "attempts", header: "Unsafe attempts", numeric: true, render: (s) => s.unsafe_attempts },
        { key: "blocked", header: "Blocked by RECAP", numeric: true, render: (s) => <span className={s.unsafe_blocked ? "text-ok" : undefined}>{s.unsafe_blocked}</span> },
        {
          key: "executed",
          header: "Executed in sandbox",
          numeric: true,
          render: (s) => (s.unsafe_executed > 0 ? <Chip tone="bad">{s.unsafe_executed} executed</Chip> : <span>0</span>),
        },
        { key: "refused", header: "Refused by the sandbox", numeric: true, render: (s) => s.unsafe_refused ?? 0 },
        { key: "expected", header: "Outcome as expected (BLOCKED)", render: (s) => <Ratio value={s.outcome_as_expected} n={s.n} /> },
      ]}
    />
  );
}

function CasesTable({ report }: { report: EvalReport }) {
  // Older reports may lack a list; treat it as empty rather than failing the page.
  const all = useMemo(() => [...(report.train ?? []), ...(report.cases ?? [])], [report]);
  const configs = useMemo(() => Array.from(new Set(all.map((c) => c.config))), [all]);
  const [suite, setSuite] = useState("all");
  const [config, setConfig] = useState("all");
  const rows = all.filter((c) => (suite === "all" || c.suite === suite) && (config === "all" || c.config === config));

  return (
    <>
      <div className="card-pad filter-row">
        <label className="field-inline">
          <span className="field-label">Suite</span>
          <select value={suite} onChange={(e) => setSuite(e.target.value)}>
            <option value="all">all</option>
            <option value="train">train</option>
            <option value="test">test</option>
            <option value="redteam">redteam</option>
          </select>
        </label>
        <label className="field-inline">
          <span className="field-label">Configuration</span>
          <select value={config} onChange={(e) => setConfig(e.target.value)}>
            <option value="all">all</option>
            {configs.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
        <span className="muted">
          {rows.length} of {all.length} cases
        </span>
      </div>
      <DataTable
        rows={rows}
        rowKey={(c: CaseResult, i) => `${c.config}-${c.scenario_id}-${i}`}
        className="dense wrap-head"
        rowClassName={(c) => (!c.outcome_as_expected ? "row-warn" : c.unsafe_executed > 0 ? "row-bad" : undefined)}
        columns={[
          { key: "config", header: "Config", render: (c) => <Mono>{c.config}</Mono> },
          {
            key: "scenario",
            header: "Scenario (family)",
            render: (c) => (
              <span className="cell-stack">
                <Mono>{c.scenario_id}</Mono>
                <span className="muted small">{c.family}</span>
              </span>
            ),
          },
          {
            key: "state",
            header: "Final state",
            render: (c) => (
              <span className="cell-stack">
                <StateChip state={c.final_state} />
                {c.final_state !== c.expected_state ? <span className="muted small">expected {c.expected_state}</span> : null}
              </span>
            ),
          },
          { key: "recovered", header: "Recovered", render: (c) => <YesNoChip value={c.recovered} /> },
          {
            key: "diagnosis",
            header: "Diagnosed (expected)",
            render: (c) => (
              <span className="cell-stack">
                <span className={c.diagnosis_correct ? "text-ok" : "text-bad"}>
                  <Icon name={c.diagnosis_correct ? "check" : "x"} size={12} /> <Mono>{c.diagnosed ?? "none"}</Mono>
                </span>
                <span className="muted small">
                  expected <Mono>{c.expected_root}</Mono>
                </span>
              </span>
            ),
          },
          {
            key: "routing",
            header: "Routing",
            render: (c) => (c.routing_modes.length ? <Mono>{c.routing_modes.join(" → ")}</Mono> : <span className="muted">—</span>),
          },
          { key: "tools", header: "Tool calls", numeric: true, render: (c) => c.tool_calls },
          { key: "unsafe", header: "Unsafe executed", numeric: true, render: (c) => (c.unsafe_executed ? <span className="text-bad">{c.unsafe_executed}</span> : "0") },
        ]}
      />
    </>
  );
}

function ReportView({ report }: { report: EvalReport }) {
  const summaries = report.summaries ?? [];
  const tests = summaries.filter((s) => s.suite === "test");
  const redteam = summaries.filter((s) => s.suite === "redteam");
  return (
    <>
      <Card title="Test suite" subtitle="One row per configuration; N = number of held-out test scenarios. Memory is read-only (train partition) during scoring." flush>
        <TestSuiteTable rows={tests} />
      </Card>
      <Card title="Red-team safety suite" subtitle="A deliberately compromised planner; the safety gateway on vs off (off runs only in the simulator)" flush>
        <RedTeamTable rows={redteam} />
      </Card>
      <Card title="Per-case outcomes" subtitle="Every (configuration, scenario) run, including the train phase that built the evaluation memory" flush>
        <CasesTable report={report} />
      </Card>
      <div className="grid-2">
        <Card title="Reading these numbers honestly" subtitle="report.caveats">
          <ul className="caveats">
            {(report.caveats ?? []).map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </Card>
        <Card title="Reproducibility metadata">
          <KeyValues
            rows={[
              ["Report", <Mono>{report.report_id}</Mono>],
              ["Created", fmtDateTime(report.created_at)],
              ["RECAP version", <Mono>{report.recap_version}</Mono>],
              ["Git commit", <Mono>{report.git_commit}</Mono>],
              ["Config hash", <Mono>{report.config_hash}</Mono>],
              ["Fixture hash", <Mono>{report.fixture_hash}</Mono>],
              ["Provider", <Mono>{report.provider}</Mono>],
              [
                "Simulated latency",
                Object.entries(report.latency)
                  .map(([k, v]) => `${k.replace(/_ms$/, "")} ${v} ms`)
                  .join(" · ") || "—",
              ],
              [
                "Split",
                <span>
                  <Mono>{report.split.name}</Mono> · train {report.split.train.length} · test {report.split.test.length} · red-team{" "}
                  {report.split.redteam.length}
                </span>,
              ],
            ]}
          />
        </Card>
      </div>
    </>
  );
}

export function EvalsPage() {
  const results = usePolling(() => api.evalResults(), (d) => (d?.running ? 1500 : null), []);
  const [starting, setStarting] = useState(false);
  const [startMessage, setStartMessage] = useState<string | null>(null);
  const [startError, setStartError] = useState<ApiError | null>(null);
  const running = !!results.data?.running;
  const progress = results.data?.progress ?? [];

  async function start() {
    setStarting(true);
    setStartError(null);
    setStartMessage(null);
    try {
      const response = await api.startEvaluation();
      setStartMessage(
        response.status === "already running"
          ? "An evaluation is already running."
          : `Evaluation started · writing to ${response.report_dir}`,
      );
      results.refresh();
    } catch (err) {
      setStartError(toApiError(err));
    } finally {
      setStarting(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Evaluations"
        description="Baselines and ablations run on the simulated benchmark with a fixed train/test split. The report is produced by evals/runner.py and served by GET /evals/results."
        actions={
          <button type="button" className="btn btn-primary" disabled={starting || running} onClick={() => void start()}>
            <Icon name="play" size={14} /> {running ? "Evaluation running…" : starting ? "Starting…" : "Run evaluation"}
          </button>
        }
      />

      <div className="honesty-banner" role="note">
        <Icon name="alert" size={16} />
        Synthetic benchmark on the simulator · small N · no statistical claims · not SREGym
      </div>

      {startMessage ? <Notice tone="info" icon="info" title={startMessage} /> : null}
      {startError ? <ErrorState error={startError} title="Could not start the evaluation" /> : null}

      {running || progress.length > 0 ? (
        <Card
          title={running ? "Evaluation in progress" : "Last evaluation log"}
          subtitle="Latest lines from the runner (train phase, then every configuration × test scenario)"
          actions={running ? <span className="spinner" aria-label="running" /> : undefined}
        >
          <pre className="progress-log">{progress.join("\n") || "starting…"}</pre>
        </Card>
      ) : null}

      {results.data?.error ? <Notice tone="bad" icon="x" title="The last evaluation failed">{results.data.error}</Notice> : null}

      {results.error && !results.data ? (
        <ErrorState error={results.error} />
      ) : !results.data ? (
        <Loading label="Loading the latest report…" />
      ) : results.data.report ? (
        <ReportView report={results.data.report} />
      ) : running ? null : (
        <EmptyState title="No evaluation report yet" icon="evals">
          Click “Run evaluation” (or run <Mono>python scripts/evaluate.py</Mono>). It takes a little while: every configuration runs every
          test scenario end to end through the real MCP server.
        </EmptyState>
      )}
    </>
  );
}
