import { Fragment } from "react";
import type { ReactNode } from "react";
import { api } from "../api";
import { Chip, Mono, OriginChip, SimulatedBadge } from "../components/Badge";
import { Card, KeyValues, Notice, PageHeader } from "../components/Card";
import { ErrorState, Loading } from "../components/EmptyState";
import { Icon } from "../components/Icon";
import { DataTable } from "../components/Table";
import { fmtInt, hybridReadiness, searchModeLabel } from "../format";
import type { Tone } from "../format";
import { usePolling } from "../hooks";
import type { PageProps } from "../hooks";
import type { ProviderStatus, SystemSummary } from "../types";

type Trust = "trusted" | "untrusted" | "readonly";

/** Let long snake_case names (infrastructure_investigator) wrap after an underscore. */
function breakable(text: string): ReactNode {
  const parts = text.split("_");
  return parts.map((part, i) => (
    <Fragment key={i}>
      {part}
      {i < parts.length - 1 ? (
        <>
          _<wbr />
        </>
      ) : null}
    </Fragment>
  ));
}

const TRUST_TAG: Record<Trust, string> = {
  trusted: "trusted code",
  untrusted: "untrusted data",
  readonly: "read-only agents",
};

function Layer({
  n,
  title,
  trust,
  children,
  subs,
}: {
  n: number;
  title: string;
  trust: Trust;
  children?: ReactNode;
  subs?: { label: string; detail?: string; dim?: boolean; active?: boolean }[];
}) {
  return (
    <div className={`layer trust-${trust}`}>
      <div className="layer-head">
        <span className="layer-num">{n}</span>
        <span className="layer-title">{title}</span>
        <span className={`trust-tag trust-${trust}`}>{TRUST_TAG[trust]}</span>
      </div>
      {children ? <div className="layer-desc">{children}</div> : null}
      {subs ? (
        <div className="layer-subs">
          {subs.map((s) => (
            <div key={s.label} className={`layer-sub${s.dim ? " dim" : ""}${s.active ? " active" : ""}`}>
              <span className="layer-sub-label">{breakable(s.label)}</span>
              {s.detail ? <span className="layer-sub-detail">{s.detail}</span> : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function Arrow({ label }: { label?: string }) {
  return (
    <div className="arch-arrow" aria-hidden="true">
      <span className="arch-arrow-line" />
      {label ? <span className="arch-arrow-label">{label}</span> : null}
    </div>
  );
}

function Diagram() {
  return (
    <div className="arch">
      <Layer n={1} title="Alert / scenario" trust="untrusted">
        A simulated fault replayed from fixtures now; an SREGym problem later. Alert text is data, never instructions.
      </Layer>
      <Arrow label="typed Incident" />
      <Layer n={2} title="Incident controller" trust="trusted">
        Lifecycle state machine, a checkpoint after every transition, bounded retries and hard budgets. The only component
        that can execute a write.
      </Layer>
      <Arrow />
      <Layer
        n={3}
        title="Context & experience"
        trust="untrusted"
        subs={[
          {
            label: "Runbook RAG",
            detail: "BM25 keywords by default; hybrid adds meaning from a local embedder (nomic-embed-text); agents search too; cited chunks",
          },
          { label: "Episodic memory", detail: "past runs: DEMO partition for scripted runs, LIVE for real models; similar ≠ safe to reuse" },
          { label: "Evidence registry", detail: "immutable observations with ids" },
        ]}
      >
        The context packer builds bounded, ranked views within an estimated-token budget.
      </Layer>
      <Arrow />
      <Layer n={4} title="Adaptive router" trust="trusted">
        Single generalist or parallel specialists, chosen from observable signals and the remaining budget; the rule that fired
        is logged. A disagreement no evidence rule settles sends the verifier's question to one follow-up round (the
        disagreement signal: RT3-disagreement, unless an earlier rule such as RT2-cross-service fires first); if it stays
        open, a person decides. The verifier's leads (where to look next, with no conflict) go to the next round too.
      </Layer>
      <Arrow label="asyncio.gather when parallel" />
      <Layer
        n={5}
        title="Investigation agents"
        trust="readonly"
        subs={[
          { label: "investigator" },
          { label: "telemetry_investigator" },
          { label: "infrastructure_investigator" },
          { label: "evidence_verifier", detail: "no tools; the merger pools claims, rules V1–V7 judge them, VP1–VP4 the fix" },
        ]}
      >
        observe → reason → act. The provider (the scripted policy, a local or free hosted model on prompts-v4.5, or a replay)
        sees only packed evidence; every hypothesis must cite evidence ids. In a parallel round each specialist sees the
        evidence from before the round plus its own, never the other's. Disagreements are settled by evidence rules, never by
        votes.
      </Layer>
      <Arrow label="tool calls" />
      <Layer n={6} title="MCP tool gateway" trust="trusted">
        A real MCP client and server over stdio. Discovery is untrusted; typed arguments, the risk policy on every call, bounded
        outputs and an audit trail. One read tool, search_runbooks, is answered by RECAP itself on the same path.
      </Layer>
      <Arrow />
      <Layer
        n={7}
        title="Target environment adapter"
        trust="untrusted"
        subs={[
          { label: "Simulator", detail: "V1 · active", active: true },
          { label: "SREGym / Kubernetes", detail: "V2 · adapter interface only", dim: true },
        ]}
      >
        Tool outputs and logs come back as untrusted data.
      </Layer>
      <Arrow label="mitigation proposal" />
      <div className="arch-row">
        <Layer n={8} title="Safety policy" trust="trusted">
          ALLOW · APPROVAL_REQUIRED · DENY, from trusted inputs only.
        </Layer>
        <span className="arch-row-arrow" aria-hidden="true">
          <Icon name="arrowRight" size={16} />
        </span>
        <Layer n={9} title="Approval" trust="trusted">
          Bound to the exact proposal digest, expiring, single use.
        </Layer>
        <span className="arch-row-arrow" aria-hidden="true">
          <Icon name="arrowRight" size={16} />
        </span>
        <Layer n={10} title="Mitigation" trust="trusted">
          Executed by the controller through the gateway.
        </Layer>
      </div>
      <Arrow />
      <Layer n={11} title="Independent recovery oracle" trust="trusted">
        Ground-truth health before vs after, read from environment state — never from agent text.
      </Layer>
      <Arrow />
      <Layer n={12} title="Memory consolidation" trust="trusted">
        A finished run becomes an episode in its own partition (DEMO for the scripted policy, LIVE for a real model; replays and
        held-out test scenarios write none), summarized by deterministic extraction (consolidator-v2); <strong>success</strong>{" "}
        only if the oracle verified recovery.
        <span className="arch-loop">
          <Icon name="refresh" size={13} /> feeds the next incident's context (3)
        </span>
      </Layer>

      <div className="arch-cross">
        <div className="arch-cross-title">Cross-cutting</div>
        <div className="arch-cross-items">
          <span>Telemetry: structured trace events → SQLite, JSONL, live SSE</span>
          <span>Budgets: steps, tool calls, estimated tokens, model tokens the server reports, run clock (approval waits excluded)</span>
          <span>Evaluations: fixed train/test split, baselines, ablations, red team</span>
          <span>
            API + dashboard: no endpoint executes an action directly; clients start simulated runs and evaluations and approve
            or reject one exact proposal
          </span>
        </div>
      </div>
    </div>
  );
}

function Wiring({ system }: { system: SystemSummary }) {
  const env = system.environment;
  return (
    <>
      <KeyValues
        className="wiring"
        rows={[
          [
            "7 · Environment adapter",
            <span className="cell-stack">
              <span className="chip-row">
                <Mono>{system.environment_kind}</Mono>
                {env.is_simulated ? <SimulatedBadge /> : <Chip tone="bad">real system</Chip>}
              </span>
              <span>
                {env.name} · namespace <Mono>{env.namespace}</Mono>
              </span>
              {env.supported_actions.length ? (
                <span className="muted small">supported actions: {env.supported_actions.join(", ")}</span>
              ) : null}
              {env.description ? <span className="muted small">{env.description}</span> : null}
            </span>,
          ],
          ["6 · Tool transport", system.tool_transport],
          [
            "5 · Decision provider",
            <span className="cell-stack">
              <span className="chip-row">
                <Mono>{system.provider.name}</Mono>
                <OriginChip provider={system.provider} />
                {system.provider.mode ? <span className="muted small">{system.provider.mode} mode</span> : null}
                {system.provider.prompt_version ? <span className="muted small">{system.provider.prompt_version}</span> : null}
              </span>
              {system.provider.description ? <span className="muted small">{system.provider.description}</span> : null}
            </span>,
          ],
          ["5 · Evidence verifier", <Mono>{system.verifier}</Mono>],
          [
            "3 · Retriever",
            <span className="cell-stack">
              <span>
                <Mono>{system.retriever}</Mono> · {fmtInt(system.knowledge_chunks)} chunks
              </span>
              {/* M3: the retriever is what a run that does not choose searches with; hybrid_ready says whether hybrid can run. */}
              {system.retrieval_mode ? (
                <span className="muted small">
                  default search: {searchModeLabel(system.retrieval_mode, true)}
                  {hybridReadiness(system) === "ready" ? " · hybrid can run" : " · hybrid cannot run: stored vectors missing or incomplete"}
                  {system.embedder ? (
                    <>
                      {" "}· stored vectors <Mono>{system.embedder}</Mono>
                    </>
                  ) : null}
                </span>
              ) : null}
            </span>,
          ],
          ["3 · Experience memory", `${fmtInt(system.memory_episodes)} episodes`],
          ["4 · Router", <Mono>{system.router}</Mono>],
          ["8 · Safety policy", <Mono>{system.safety_policy}</Mono>],
          ["11 · Recovery oracle", <Mono>{system.oracle}</Mono>],
          [
            "Trace sinks",
            <span className="chip-row">
              {system.trace_sinks.map((s) => (
                <Chip key={s} mono>
                  {s}
                </Chip>
              ))}
            </span>,
          ],
          ["Config hash", <Mono>{system.config_hash}</Mono>],
          ["Fixture hash", <Mono>{system.fixture_hash}</Mono>],
        ]}
      />
      <Notice tone="accent" icon="architecture" title="Swapping the infrastructure">
        Swap the infrastructure by writing one <Mono>TargetEnvironment</Mono> adapter and changing{" "}
        <Mono>configs/environment.yaml</Mono>: the controller, gateway, RAG, memory, router and orchestrator code stay
        unchanged. Still simulator-specific (data, not code): the log conventions the scripted policy and verifier parse, the
        policy allowlists and the runbooks.
      </Notice>
    </>
  );
}

export function ArchitecturePage({ system }: PageProps) {
  return (
    <>
      <PageHeader
        title="Architecture"
        description="How an alert flows through RECAP, where the trust boundaries are, and which implementation is plugged into each interface right now."
      />
      <div className="arch-layout">
        <Card
          title="RECAP flow"
          subtitle={
            <span className="chip-row">
              <span className="trust-tag trust-trusted">trusted code</span>
              <span className="trust-tag trust-untrusted">untrusted data</span>
              <span className="trust-tag trust-readonly">read-only agents</span>
            </span>
          }
        >
          <Diagram />
        </Card>
        <Card title="Live wiring" subtitle="GET /system — numbers refer to the diagram">
          {system.data ? <Wiring system={system.data} /> : system.error ? <ErrorState error={system.error} /> : <Loading />}
        </Card>
      </div>
      <ProvidersCard />
    </>
  );
}

const STATUS_TONE: Record<ProviderStatus["status"], Tone> = {
  ready: "ok",
  needs_setup: "warn",
  stopped: "bad",
  disabled: "neutral",
  unavailable: "warn",
};

/**
 * Every configured decision provider and whether it could run now (GET /providers). The
 * server never calls a hosted API or a model to answer this, and a key is shown only as
 * present or missing. The server's default provider is the one new runs use.
 */
function ProvidersCard() {
  const providers = usePolling(() => api.providers(), 15000, []);
  return (
    <Card
      title="Decision providers"
      subtitle="GET /providers — readiness, free-tier guard and usage; this project's budget is $0"
    >
      {providers.data ? (
        <DataTable
          rows={providers.data}
          rowKey={(row) => row.name}
          columns={[
            {
              key: "name",
              header: "Provider",
              render: (row) => (
                <span className="cell-stack">
                  <span className="chip-row">
                    <Mono>{row.name}</Mono>
                    {row.default ? <Chip tone="accent">server default</Chip> : null}
                  </span>
                  <span className="muted small">{row.description}</span>
                </span>
              ),
            },
            { key: "origin", header: "Origin", render: (row) => <OriginChip provider={row} /> },
            {
              key: "model",
              header: "Model",
              render: (row) =>
                row.model ? (
                  <span className="cell-stack">
                    <Mono>{row.model}</Mono>
                    {row.mode ? <span className="muted small">{row.mode} mode</span> : null}
                  </span>
                ) : (
                  <span className="muted">—</span>
                ),
            },
            {
              key: "status",
              header: "Status",
              render: (row) => (
                <span className="cell-stack">
                  <Chip tone={STATUS_TONE[row.status]} dot>
                    {row.status.replace("_", " ")}
                  </Chip>
                  <span className="muted small">{row.detail}</span>
                  {row.key_env ? (
                    <span className="muted small">
                      key <Mono>{row.key_env}</Mono>: {row.key_present ? "present (value never shown)" : "missing"}
                    </span>
                  ) : null}
                </span>
              ),
            },
            {
              key: "usage",
              header: "Requests (24 h)",
              numeric: true,
              render: (row) =>
                row.usage ? (
                  <span className="cell-stack">
                    <span>{fmtInt(row.usage.requests_last_24h)}</span>
                    <span className="muted small">{fmtInt(row.usage.tokens_last_24h)} tokens</span>
                  </span>
                ) : (
                  <span className="muted">—</span>
                ),
            },
          ]}
        />
      ) : providers.error ? (
        <ErrorState error={providers.error} />
      ) : (
        <Loading />
      )}
    </Card>
  );
}
