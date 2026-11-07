import { Fragment } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import { api } from "../api";
import { Mono } from "../components/Badge";
import { Card, PageHeader } from "../components/Card";
import { DataSourceBadge } from "../components/DataSourceBadge";
import { Icon } from "../components/Icon";
import { ErrorView, PolledView } from "../components/StateViews";
import { StatusToken } from "../components/StatusToken";
import type * as dto from "../dto";
import { PAGE_LABELS, hrefFor, navigate, usePolling } from "../hooks";
import type { PageProps, Polled } from "../hooks";
import "../styles/overview-architecture.css";
import {
  ADRS,
  AROUND_NODES,
  DOCS,
  FLOW_STEPS,
  KUBERNETES_AUTHORIZATION,
  TARGET_KINDS,
  TRUST_LABELS,
  adapterLook,
  adapterName,
  adrPath,
  archNode,
  componentById,
  componentLook,
  nextNodeId,
  nodeFacts,
  nodeStatus,
  operationalLook,
  providerSource,
  repoUrl,
  selectedNodeId,
  targetGuide,
} from "../view/system";
import type { ArchNode, ArchNodeId, StatusLook, Trust } from "../view/system";

/**
 * Architecture & Learning: the flow one incident takes through RECAP, drawn from what this
 * server runs. Each step shows the status GET /system reports for its components (active,
 * available, planned, ...); selecting it explains what it does, how each target (the
 * simulated shop, the Kind demo cluster, SREGym's cluster) does it, where its code lives, the
 * decisions (ADRs) behind it and the trace events it writes. Below the flow: how a write to a
 * real Kubernetes cluster is authorized. The explanations are RECAP's own documentation;
 * every status and fact is the server's.
 */

type Loaded<T> = Pick<Polled<T>, "data" | "error" | "loading" | "refresh">;

const PANEL_ID = "amap-panel";
const tabId = (id: ArchNodeId) => `amap-tab-${id}`;

function DocLink({ path, children }: { path: string; children?: ReactNode }) {
  return (
    <a className="amap-doc-link" href={repoUrl(path)} target="_blank" rel="noopener noreferrer">
      {children ?? <Mono>{path}</Mono>}
      <span className="sr-only"> (opens on GitHub)</span>
    </a>
  );
}

/** Long identifiers (interface.method, snake_case roles) may wrap after a "." or "_" only. */
export function Breakable({ text }: { text: string }) {
  const parts = text.split(/(?<=[._])/);
  return (
    <>
      {parts.map((part, i) => (
        <Fragment key={i}>
          {part}
          {i < parts.length - 1 ? <wbr /> : null}
        </Fragment>
      ))}
    </>
  );
}

function TrustTag({ trust }: { trust: Trust }) {
  return <span className={`amap-trust trust-${trust}`}>{TRUST_LABELS[trust]}</span>;
}

/** What a node's status token says while /system is loading, failed, or lists nothing for it. */
function statusOf(node: ArchNode, system: dto.SystemView | null, failed: boolean): StatusLook {
  if (!node.components.length) {
    return {
      label: "No status in /system",
      tone: "neutral",
      icon: "minus",
      title: "Not a swappable component: GET /system reports no separate status for it.",
    };
  }
  if (!system) {
    return failed
      ? { label: "Status unavailable", tone: "neutral", icon: "minus", title: "GET /system could not be loaded." }
      : { label: "Checking…", tone: "neutral", icon: "clock", title: "Waiting for GET /system." };
  }
  return nodeStatus(node, system)?.look ?? componentLook(null);
}

function partStatus(component: string | undefined, system: dto.SystemView | null): StatusLook | null {
  if (!component || !system) return null;
  const found = componentById(system, component);
  return found ? componentLook(found.status) : componentLook(null);
}

/** The implementation line under a node's title: the server's, else the node's own summary. */
function implementationOf(node: ArchNode, system: dto.SystemView | null): string {
  if (node.id === "environment" && system) return `${system.environment.name} · ${system.environment_kind}`;
  const first = node.components.length && system ? componentById(system, node.components[0]) : null;
  return first ? first.implementation : node.summary;
}

// ======================================================================================
// The flow map: a vertical tab list of steps, and the panel of the selected one
// ======================================================================================

function NodeButton({
  node,
  system,
  failed,
  selected,
  onSelect,
  onKeyDown,
}: {
  node: ArchNode;
  system: dto.SystemView | null;
  failed: boolean;
  selected: boolean;
  onSelect: (id: ArchNodeId, how: "pointer" | "keyboard") => void;
  onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => void;
}) {
  const look = statusOf(node, system, failed);
  const planned = nodeStatus(node, system)?.component?.status === "planned";
  const classes = ["amap-node", `trust-${node.trust}`, selected ? "is-selected" : "", planned ? "is-planned" : ""].filter(Boolean).join(" ");
  return (
    <button
      type="button"
      role="tab"
      id={tabId(node.id)}
      className={classes}
      aria-selected={selected}
      aria-controls={PANEL_ID}
      aria-labelledby={`${tabId(node.id)}-title ${tabId(node.id)}-status`}
      tabIndex={selected ? 0 : -1}
      onClick={(event) => onSelect(node.id, event.detail === 0 ? "keyboard" : "pointer")}
      onKeyDown={onKeyDown}
    >
      <span className="amap-node-head">
        <span className="amap-step" aria-hidden="true">
          {node.step ?? <Icon name={node.icon} size={13} />}
        </span>
        <span className="amap-node-title" id={`${tabId(node.id)}-title`}>
          {node.step ? <span className="sr-only">Step {node.step}: </span> : null}
          {node.title}
        </span>
        <TrustTag trust={node.trust} />
        <span className="amap-node-status" id={`${tabId(node.id)}-status`}>
          <StatusToken tone={look.tone} icon={look.icon} label={look.label} title={look.title} size="sm" />
        </span>
      </span>
      <span className="amap-node-impl">{implementationOf(node, system)}</span>
      {node.parts.length ? (
        <span className="amap-parts">
          {node.parts.map((part) => {
            const status = partStatus(part.component, system);
            return (
              <span key={part.label} className="amap-part">
                <span className="amap-part-label">
                  <Breakable text={part.label} />
                </span>
                {part.note ? <span className="amap-part-note">{part.note}</span> : null}
                {status ? <StatusToken tone={status.tone} icon={status.icon} label={status.label} size="sm" variant="plain" /> : null}
              </span>
            );
          })}
        </span>
      ) : null}
      {node.id === "environment" && system ? (
        <span className="amap-parts">
          {system.environment_adapters.map((adapter) => {
            const status = adapterLook(adapter);
            return (
              <span key={adapter.kind} className={adapter.status === "planned" ? "amap-part is-planned" : "amap-part"}>
                <span className="amap-part-label">{adapterName(adapter.kind)}</span>
                <StatusToken tone={status.tone} icon={status.icon} label={status.label} size="sm" variant="plain" />
              </span>
            );
          })}
        </span>
      ) : null}
    </button>
  );
}

function Connector({ label, loop, end = false }: { label?: string; loop?: string; end?: boolean }) {
  return (
    <div className={end ? "amap-connector is-end" : "amap-connector"} aria-hidden="true">
      {end ? null : <span className="amap-connector-line" />}
      {label ? <span className="amap-connector-label">{label}</span> : null}
      {loop ? (
        <span className="amap-connector-loop">
          <Icon name="refresh" size={12} /> {loop}
        </span>
      ) : null}
    </div>
  );
}

export function FlowMap({
  system,
  failed = false,
  selected,
  onSelect,
}: {
  system: dto.SystemView | null;
  failed?: boolean;
  selected: ArchNodeId;
  onSelect: (id: ArchNodeId, how: "pointer" | "keyboard") => void;
}) {
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const current = archNode(event.currentTarget.id.replace("amap-tab-", ""))?.id ?? selected;
    const next = nextNodeId(current, event.key);
    if (!next) return;
    event.preventDefault();
    onSelect(next, "keyboard");
    document.getElementById(tabId(next))?.focus();
  };
  const node = archNode(selected) ?? FLOW_STEPS[0];
  return (
    <div className="amap">
      <div className="amap-graph" role="tablist" aria-orientation="vertical" aria-label="RECAP's flow, step by step">
        {FLOW_STEPS.map((step, i) => (
          <div key={step.id} className="amap-cell">
            <NodeButton node={step} system={system} failed={failed} selected={step.id === selected} onSelect={onSelect} onKeyDown={onKeyDown} />
            {i < FLOW_STEPS.length - 1 ? <Connector label={step.next} loop={step.loop} /> : step.loop ? <Connector loop={step.loop} end /> : null}
          </div>
        ))}
        <div className="amap-around" aria-hidden="true">
          Around every step
        </div>
        {AROUND_NODES.map((around) => (
          <div key={around.id} className="amap-cell">
            <NodeButton node={around} system={system} failed={failed} selected={around.id === selected} onSelect={onSelect} onKeyDown={onKeyDown} />
          </div>
        ))}
      </div>
      <section id={PANEL_ID} className="amap-panel" role="tabpanel" aria-labelledby={`${tabId(node.id)}-title`}>
        <NodeDetail node={node} system={system} failed={failed} />
      </section>
    </div>
  );
}

// ======================================================================================
// The selected step's explanation
// ======================================================================================

function ComponentLine({ id, system }: { id: string; system: dto.SystemView }) {
  const component = componentById(system, id);
  if (!component) {
    return (
      <li className="amap-component">
        <span className="amap-component-head">
          <Mono>{id}</Mono>
          <StatusToken tone="neutral" icon="minus" label="Not reported" size="sm" />
        </span>
        <span className="amap-component-detail">This server's GET /system does not list this component.</span>
      </li>
    );
  }
  const look = componentLook(component.status);
  return (
    <li className="amap-component">
      <span className="amap-component-head">
        <Mono>
          <Breakable text={component.interface} />
        </Mono>
        <StatusToken tone={look.tone} icon={look.icon} label={look.label} title={look.title} size="sm" />
      </span>
      <span className="amap-component-impl">{component.implementation}</span>
      {component.detail ? <span className="amap-component-detail">{component.detail}</span> : null}
    </li>
  );
}

/** Where the real-cluster targets are set up, and what M5 verified on them. */
function TargetDocs({ kind }: { kind: string | null }) {
  const guide = targetGuide(kind);
  return (
    <p className="amap-muted">
      {guide ? (
        <>
          This server's target: <DocLink path={guide.path}>{guide.title}</DocLink>.{" "}
        </>
      ) : null}
      What was built and verified on each real cluster, run by run, failures included: <DocLink path={DOCS.m5Progress} />.
    </p>
  );
}

/** How each target does a step, where they differ; the configured one is marked (GET /system). */
function TargetNotes({ node, system }: { node: ArchNode; system: dto.SystemView | null }) {
  const notes = node.onTarget;
  if (!notes) return null;
  const configured = system?.environment_kind ?? null;
  return (
    <section className="amap-section">
      <h4 className="mini-title">On each target</h4>
      <dl className="amap-targets">
        {TARGET_KINDS.filter((kind) => notes[kind]).map((kind) => (
          <div key={kind} className={kind === configured ? "amap-target is-current" : "amap-target"}>
            <dt>
              {adapterName(kind)}
              {kind === configured ? <span className="amap-tag">this server</span> : null}
            </dt>
            <dd>{notes[kind]}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function StatusSection({ node, system, failed }: { node: ArchNode; system: dto.SystemView | null; failed: boolean }) {
  let body: ReactNode;
  if (!node.components.length) {
    body = (
      <p className="amap-muted">
        The controller is not a swappable interface, so GET /system reports no separate status for it. Every run goes through it.
      </p>
    );
  } else if (!system) {
    body = <p className="amap-muted">{failed ? "GET /system could not be loaded, so no status is shown." : "Waiting for GET /system…"}</p>;
  } else {
    body = (
      <ul className="amap-components">
        {node.components.map((id) => (
          <ComponentLine key={id} id={id} system={system} />
        ))}
      </ul>
    );
  }
  return (
    <section className="amap-section">
      <h4 className="mini-title">Status in GET /system</h4>
      {body}
      {node.id === "environment" && system ? (
        <ul className="amap-components">
          {system.environment_adapters.map((adapter) => {
            const look = adapterLook(adapter);
            const operational = operationalLook(adapter);
            return (
              <li key={adapter.kind} className={adapter.status === "planned" ? "amap-component is-planned" : "amap-component"}>
                <span className="amap-component-head">
                  <span className="amap-adapter-name">
                    {adapterName(adapter.kind)} adapter{adapter.configured ? <span className="amap-tag">configured</span> : null}
                  </span>
                  <span className="chip-row">
                    <StatusToken tone={look.tone} icon={look.icon} label={look.label} title={look.title} size="sm" />
                    <StatusToken
                      tone={operational.tone}
                      icon={operational.icon}
                      label={operational.label}
                      title={operational.title}
                      size="sm"
                      variant="outline"
                    />
                  </span>
                </span>
                {adapter.adapter ? (
                  <span className="amap-component-impl">
                    class <Mono>{adapter.adapter}</Mono>
                  </span>
                ) : null}
                <span className="amap-component-detail">{adapter.detail}</span>
              </li>
            );
          })}
        </ul>
      ) : null}
      {node.id === "environment" ? <TargetDocs kind={system?.environment_kind ?? null} /> : null}
      {node.id === "specialists" && system ? <DefaultProvider system={system} /> : null}
    </section>
  );
}

function DefaultProvider({ system }: { system: dto.SystemView }) {
  const source = providerSource(system.provider);
  return (
    <div className="amap-provider">
      <span className="amap-muted">New runs use, unless they pick another provider:</span>
      {source.error ? (
        <StatusToken tone="bad" icon="x" label="Default provider cannot be built" title={source.error} size="sm" />
      ) : (
        <DataSourceBadge provider={{ origin: source.origin, kind: source.kind, model: source.model }} size="sm" label="Default provider" />
      )}
      {source.name ? <Mono>{source.name}</Mono> : null}
    </div>
  );
}

export function NodeDetail({ node, system, failed = false }: { node: ArchNode; system: dto.SystemView | null; failed?: boolean }) {
  const facts = system ? nodeFacts(node, system) : [];
  return (
    <div className="amap-detail">
      <div className="amap-detail-head">
        <span className="amap-kicker">
          {node.step ? `Step ${node.step} of ${FLOW_STEPS.length}` : "Around every step"}
          <TrustTag trust={node.trust} />
        </span>
        <h3 className="amap-detail-title">
          <Icon name={node.icon} size={18} />
          {node.title}
        </h3>
        <p className="amap-detail-summary">{node.summary}</p>
      </div>

      <StatusSection node={node} system={system} failed={failed} />

      <section className="amap-section">
        <h4 className="mini-title">What it does</h4>
        {node.what.map((paragraph) => (
          <p key={paragraph} className="amap-text">
            {paragraph}
          </p>
        ))}
        {node.loop ? (
          <p className="amap-loop">
            <Icon name="refresh" size={13} />
            {node.loop}
          </p>
        ) : null}
      </section>

      <TargetNotes node={node} system={system} />

      {facts.length ? (
        <section className="amap-section">
          <h4 className="mini-title">This server (GET /system)</h4>
          <dl className="amap-facts">
            {facts.map(([label, value]) => (
              <div key={label} className="amap-fact">
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        </section>
      ) : null}

      <section className="amap-section">
        <h4 className="mini-title">Code</h4>
        <ul className="amap-links">
          {node.modules.map((path) => (
            <li key={path}>
              <Icon name="code" size={13} />
              <DocLink path={path} />
            </li>
          ))}
        </ul>
      </section>

      <section className="amap-section">
        <h4 className="mini-title">Decisions (ADRs)</h4>
        <ul className="amap-links">
          {node.adrs.map((id) => {
            const adr = ADRS.get(id);
            if (!adr) return null;
            return (
              <li key={id}>
                <Icon name="book" size={13} />
                <DocLink path={adrPath(adr)}>
                  <span className="amap-adr-id">ADR {adr.id}</span> {adr.title}
                </DocLink>
              </li>
            );
          })}
        </ul>
      </section>

      {node.events.length ? (
        <section className="amap-section">
          <h4 className="mini-title">In a run's trace</h4>
          <span className="chip-row">
            {node.events.map((event) => (
              <code key={event} className="amap-event">
                {event}
              </code>
            ))}
          </span>
        </section>
      ) : null}

      <div className="amap-detail-actions">
        {node.page ? (
          <a className="btn btn-small" href={hrefFor(node.page)}>
            See it in {PAGE_LABELS[node.page]} <Icon name="chevronRight" size={13} />
          </a>
        ) : null}
        <button
          type="button"
          className="btn btn-ghost btn-small amap-back"
          onClick={() => document.getElementById(tabId(node.id))?.focus()}
        >
          Back to the flow
        </button>
      </div>
    </div>
  );
}

// ======================================================================================
// Live wiring and learning links
// ======================================================================================

export function Wiring({ system }: { system: dto.SystemView }) {
  const v = system.versions;
  return (
    <>
      <ul className="amap-wiring">
        {system.components.map((c) => {
          const look = componentLook(c.status);
          return (
            <li key={c.id} className={c.status === "planned" ? "amap-wire is-planned" : "amap-wire"}>
              <span className="amap-wire-interface">
                <Mono>
                  <Breakable text={c.interface} />
                </Mono>
              </span>
              <span className="amap-wire-impl">
                <span>{c.implementation}</span>
                {c.detail ? <span className="amap-wire-detail">{c.detail}</span> : null}
              </span>
              <span className="amap-wire-status">
                <StatusToken tone={look.tone} icon={look.icon} label={look.label} title={look.title} size="sm" />
              </span>
            </li>
          );
        })}
      </ul>
      <dl className="amap-versions">
        {(
          [
            ["RECAP", v.recap],
            ["API schema", v.api_schema],
            ["Trace schema", v.trace_schema],
            ["Prompts", v.prompt_version],
            ["Policy", v.policy_version],
            ["Router", v.router],
            ["Verifier", v.verifier],
            ["Config hash", v.config_hash],
            ["Fixture hash", v.fixture_hash],
            ["Code version", v.code_version],
          ] as const
        ).map(([label, value]) => (
          <div key={label} className="amap-version">
            <dt>{label}</dt>
            <dd>
              <Mono>{value}</Mono>
            </dd>
          </div>
        ))}
      </dl>
    </>
  );
}

const LEARN: readonly { path: string; title: string; about: string }[] = [
  { path: DOCS.flow, title: "End-to-end walkthrough", about: "One incident, stage by stage: what comes in, which code handles it, what comes out." },
  { path: DOCS.adrIndex, title: "Decision records (ADRs)", about: "Why each part is built the way it is, one short record per decision." },
  { path: DOCS.architecture, title: "Architecture", about: "The components in depth, their trust boundaries, and how to swap the infrastructure." },
  { path: DOCS.threatModel, title: "Threat model", about: "Every threat RECAP defends against, where the defense lives and the test that proves it." },
  { path: DOCS.demoWalkthrough, title: "Demo walkthrough", about: "A short tour of the recorded real-model replay, and what it does and does not prove." },
  {
    path: DOCS.m5Progress,
    title: "M5: RECAP on a real Kubernetes target",
    about: "What was built and verified on the Kind demo cluster and on SREGym's cluster, run by run, failures included, and its limits.",
  },
  { path: DOCS.kindTarget, title: "The Kind demo cluster", about: "Its demo services, RECAP's four identities, the RBAC, the admission policy and the kill switch." },
  {
    path: DOCS.sregymTarget,
    title: "The SREGym adapter",
    about: "How RECAP works a SREGym problem: SREGym's harness owns the fault, RECAP has three identities of its own, SREGym's grader gives the verdict.",
  },
  { path: DOCS.m5Plan, title: "M5 integration plan", about: "The design for a real Kubernetes target, Kind first, then SREGym, written before the work." },
];

/** Whether the steps below apply to this server's target, in one sentence (GET /system). */
function chainScope(system: dto.SystemView): string {
  const env = system.environment;
  if (env.is_simulated) {
    return `This server's target is simulated (${env.name}): a proposal, the verifier's checks, a person's approval (or, in a simulation only, the auto-approver when a run asks for it) and a signed capability apply there too, then the simulator's own write boundary changes only its own state.`;
  }
  if (targetGuide(system.environment_kind)) {
    return `This server's target is a real cluster (${env.name}, namespace ${env.namespace}): every approved write on it passes each step below.`;
  }
  return `This server's target (${env.name}) is not simulated, and it is not one of this repository's Kubernetes targets: the steps below describe the Kind demo and SREGym only.`;
}

/**
 * How a write to a real cluster is authorized, step by step (RECAP's documentation), and
 * whether this server's target is one (GET /system).
 */
export function AuthorizationChain({ system }: { system: dto.SystemView | null }) {
  return (
    <>
      {system ? <p className="amap-muted">{chainScope(system)}</p> : null}
      <ol className="amap-auth">
        {KUBERNETES_AUTHORIZATION.map((step, i) => (
          <li key={step.title} className="amap-auth-step">
            <span className="amap-step" aria-hidden="true">
              {i + 1}
            </span>
            <div className="amap-auth-body">
              <span className="amap-auth-title">{step.title}</span>
              <span className="amap-auth-text">{step.text}</span>
              <span className="amap-auth-links">
                {step.links.map((path) => (
                  <DocLink key={path} path={path} />
                ))}
              </span>
            </div>
          </li>
        ))}
      </ol>
    </>
  );
}

export function LearnLinks() {
  return (
    <ul className="amap-learn">
      {LEARN.map((doc) => (
        <li key={doc.path} className="amap-learn-item">
          <DocLink path={doc.path}>
            <span className="amap-learn-title">{doc.title}</span>
          </DocLink>
          <span className="amap-learn-about">{doc.about}</span>
          <Mono>{doc.path}</Mono>
        </li>
      ))}
    </ul>
  );
}

// ======================================================================================
// The page
// ======================================================================================

export function ArchitectureView({
  system,
  selected,
  onSelect,
}: {
  system: Loaded<dto.SystemView>;
  selected: ArchNodeId;
  onSelect: (id: ArchNodeId, how: "pointer" | "keyboard") => void;
}) {
  const failed = !system.data && !!system.error;
  return (
    <div className="amap-page">
      <PageHeader
        title="Architecture & Learning"
        description="How one incident moves through RECAP, drawn from what this server runs. Select a step to see what it does, where its code lives, the decisions behind it and its live status."
        actions={
          <div className="amap-header-links">
            <DocLink path={DOCS.flow}>
              <Icon name="book" size={14} /> Walkthrough
            </DocLink>
            <DocLink path={DOCS.adrIndex}>
              <Icon name="book" size={14} /> ADR index
            </DocLink>
          </div>
        }
      />

      <Card
        title="How one incident flows through RECAP"
        subtitle="Each step's status comes from GET /system. Use the arrow keys to move between steps."
      >
        <div className="amap-legend" aria-label="Legend">
          <span className="amap-legend-group">
            <TrustTag trust="trusted" />
            <TrustTag trust="untrusted" />
            <TrustTag trust="readonly" />
            <TrustTag trust="human" />
          </span>
          <span className="amap-legend-note">A part the server lists as planned is dashed and never shown as active.</span>
        </div>
        {failed && system.error ? (
          <ErrorView error={system.error} title="Could not load GET /system: statuses are not shown" onRetry={system.refresh} />
        ) : null}
        <FlowMap system={system.data ?? null} failed={failed} selected={selected} onSelect={onSelect} />
      </Card>

      <Card
        title="How a real Kubernetes action is authorized"
        subtitle="On the Kind demo cluster and on SREGym's cluster, an approved fix reaches the cluster only through each of these steps, in order"
      >
        <AuthorizationChain system={system.data ?? null} />
      </Card>

      <Card title="Live wiring" subtitle="Every interface, the implementation behind it and its status, as GET /system reports them">
        <PolledView polled={system} loadingLabel="Loading GET /system…" errorTitle="Could not load GET /system">
          {(data) => <Wiring system={data} />}
        </PolledView>
      </Card>

      <Card title="Learn more" subtitle="The documents behind this page, in the repository (links open on GitHub)">
        <LearnLinks />
      </Card>
    </div>
  );
}

/** On a narrow screen the panel sits under the flow: bring it into view after a tap. */
function revealPanel(how: "pointer" | "keyboard") {
  if (how !== "pointer" || typeof window === "undefined" || !window.matchMedia) return;
  if (!window.matchMedia("(max-width: 1099px)").matches) return;
  window.setTimeout(() => document.getElementById(PANEL_ID)?.scrollIntoView({ block: "start", behavior: "smooth" }), 30);
}

export function ArchitecturePage({ route }: PageProps) {
  const system = usePolling(() => api.systemView(), 30_000, []);
  const selected = selectedNodeId(route.params.get("node"));
  const onSelect = (id: ArchNodeId, how: "pointer" | "keyboard") => {
    if (id !== selected) navigate("architecture", { node: id }, true);
    revealPanel(how);
  };
  return <ArchitectureView system={system} selected={selected} onSelect={onSelect} />;
}
