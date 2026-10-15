import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api } from "./api";
import { BackendHint, ErrorBoundary } from "./components/EmptyState";
import { Icon } from "./components/Icon";
import type { IconName } from "./components/Icon";
import { OperatorSignIn } from "./components/OperatorSignIn";
import { environmentBadge, providerLabel } from "./format";
import { hrefFor, usePolling, useHashRoute } from "./hooks";
import type { PageId, PageProps } from "./hooks";
import { AgentTimelinePage } from "./pages/AgentTimeline";
import { ArchitecturePage } from "./pages/Architecture";
import { EvalsPage } from "./pages/Evals";
import { EvidencePage } from "./pages/Evidence";
import { LiveRunPage } from "./pages/LiveRun";
import { MemoryPage } from "./pages/Memory";
import { OverviewPage } from "./pages/Overview";
import { SafetyPage } from "./pages/Safety";

const NAV: { page: PageId; label: string; icon: IconName }[] = [
  { page: "overview", label: "Overview", icon: "overview" },
  { page: "live", label: "Live Incident Run", icon: "live" },
  { page: "timeline", label: "Agent Timeline", icon: "timeline" },
  { page: "evidence", label: "Evidence & RAG", icon: "evidence" },
  { page: "memory", label: "Experience Memory", icon: "memory" },
  { page: "safety", label: "Safety & Approvals", icon: "safety" },
  { page: "evals", label: "Evaluations", icon: "evals" },
  { page: "architecture", label: "Architecture", icon: "architecture" },
];

/** Pages that show one run; the selected run follows you between them. */
const RUN_PAGES: ReadonlySet<PageId> = new Set(["timeline", "evidence", "memory", "safety"]);

function renderPage(page: PageId, props: PageProps): ReactNode {
  switch (page) {
    case "overview":
      return <OverviewPage {...props} />;
    case "live":
      return <LiveRunPage {...props} />;
    case "timeline":
      return <AgentTimelinePage {...props} />;
    case "evidence":
      return <EvidencePage {...props} />;
    case "memory":
      return <MemoryPage {...props} />;
    case "safety":
      return <SafetyPage {...props} />;
    case "evals":
      return <EvalsPage />;
    case "architecture":
      return <ArchitecturePage {...props} />;
  }
}

export default function App() {
  const route = useHashRoute();
  const health = usePolling(() => api.health(), 5000, []);
  const system = usePolling(() => api.system(), 30000, []);
  const pending = usePolling(() => api.pendingApprovals(), 4000, []);

  // Remember the run being looked at (and the live run) so navigation keeps it selected.
  const [lastRun, setLastRun] = useState<string | null>(null);
  const [liveRun, setLiveRun] = useState<string | null>(null);
  const runParam = route.params.get("run");
  useEffect(() => {
    if (!runParam) return;
    if (RUN_PAGES.has(route.page) || route.page === "live") setLastRun(runParam);
    if (route.page === "live") setLiveRun(runParam);
  }, [route.page, runParam]);

  // A recovered API should refresh the wiring right away.
  const online = !!health.data && !health.error;
  const { refresh: refreshSystem } = system;
  useEffect(() => {
    if (online) refreshSystem();
  }, [online, refreshSystem]);

  useEffect(() => {
    const item = NAV.find((n) => n.page === route.page);
    document.title = `${item?.label ?? "RECAP"} · RECAP`;
  }, [route.page]);

  const waiting = pending.data?.length ?? 0;
  const provider = system.data?.provider;
  // Shown on every page: which environment actions would run against.
  const badge = environmentBadge(system.data?.environment, health.data?.label);

  const navHref = (page: PageId) => {
    if (RUN_PAGES.has(page)) return hrefFor(page, { run: lastRun });
    if (page === "live") return hrefFor(page, { run: liveRun });
    return hrefFor(page);
  };

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">
            R
          </div>
          <div>
            <div className="brand-name">RECAP</div>
            <div className="brand-sub">Experience-Learning Reliability Agent · prototype</div>
          </div>
        </div>
        <nav className="nav" aria-label="Sections">
          {NAV.map((item) => (
            <a
              key={item.page}
              href={navHref(item.page)}
              className={route.page === item.page ? "nav-item active" : "nav-item"}
              aria-current={route.page === item.page ? "page" : undefined}
            >
              <Icon name={item.icon} size={17} />
              <span>{item.label}</span>
              {item.page === "safety" && waiting > 0 ? (
                <span className="nav-badge" title={`${waiting} approval(s) waiting`}>
                  {waiting}
                </span>
              ) : null}
            </a>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div>RECAP V1{health.data ? ` · v${health.data.version}` : ""}</div>
          <div className="muted">Every view is built from the API — no hardcoded metrics.</div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <span className={`env-badge tone-${badge.tone}`} title={badge.title}>
            {badge.label}
          </span>
          <span className="provider-label" title={provider?.description}>
            <Icon name="cpu" size={15} />
            <span className="provider-text">
              {provider ? providerLabel(provider) : health.data ? `provider: ${health.data.provider}` : "provider: —"}
            </span>
          </span>
          <span className="topbar-spacer" />
          {waiting > 0 ? (
            <a className="topbar-approvals" href={hrefFor("safety", { run: lastRun })}>
              <Icon name="key" size={14} />
              {waiting} approval{waiting === 1 ? "" : "s"} waiting
            </a>
          ) : null}
          <OperatorSignIn />
          <span className={online ? "api-status ok" : "api-status bad"} role="status">
            <span className="api-dot" aria-hidden="true" />
            {online ? `API online${health.data ? ` · ${health.data.environment}` : ""}` : health.loading ? "API: checking…" : "API offline"}
          </span>
        </header>

        <main className="content" id="main">
          {health.error?.offline ? (
            <div className="offline-banner" role="alert">
              <Icon name="stop" size={18} />
              <div>
                <strong>The RECAP API is not reachable.</strong> Pages will fill in as soon as it is up.
                <BackendHint />
              </div>
            </div>
          ) : null}
          <ErrorBoundary resetKey={route.page}>{renderPage(route.page, { route, system })}</ErrorBoundary>
        </main>
      </div>
    </div>
  );
}
