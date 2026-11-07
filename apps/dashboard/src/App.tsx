import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api } from "./api";
import { ErrorBoundary } from "./components/EmptyState";
import { OperatorSignIn } from "./components/OperatorSignIn";
import { AppShell, PRODUCT_NAME } from "./components/Shell";
import { APPROVALS_CHANGED, PAGE_LABELS, RUN_SCOPED_PAGES, hrefFor, usePolling, useHashRoute } from "./hooks";
import type { PageId, PageProps } from "./hooks";
import { AgentTimelinePage } from "./pages/AgentTimeline";
import { ArchitecturePage } from "./pages/Architecture";
import { EvalsPage } from "./pages/Evals";
import { EvidencePage } from "./pages/Evidence";
import { GuidedDemoPage } from "./pages/GuidedDemo";
import { KnowledgePage } from "./pages/Knowledge";
import { LiveRunPage } from "./pages/LiveRun";
import { MemoryPage } from "./pages/Memory";
import { OverviewPage } from "./pages/Overview";
import { SafetyPage } from "./pages/Safety";
import type { RunDetail } from "./types";

function renderPage(page: PageId, props: PageProps): ReactNode {
  switch (page) {
    case "overview":
      return <OverviewPage {...props} />;
    case "demo":
      return <GuidedDemoPage {...props} />;
    case "live":
      return <LiveRunPage {...props} />;
    case "timeline":
      return <AgentTimelinePage {...props} />;
    case "evidence":
      return <EvidencePage {...props} />;
    case "knowledge":
      return <KnowledgePage {...props} />;
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

/** Poll a selected run until its record (with provider and environment) exists; retry while unreachable. */
function runDetailInterval(detail: RunDetail | undefined): number | null {
  if (detail === undefined) return 10_000;
  return detail.record === null && detail.status === "running" ? 3_000 : null;
}

export default function App() {
  const route = useHashRoute();
  const health = usePolling(() => api.health(), 5000, []);
  const system = usePolling(() => api.system(), 30000, []);
  const pending = usePolling(() => api.pendingApprovals(), 4000, []);
  // A decision made on any page updates the "approvals waiting" count right away.
  const { refresh: refreshPending } = pending;
  useEffect(() => {
    window.addEventListener(APPROVALS_CHANGED, refreshPending);
    return () => window.removeEventListener(APPROVALS_CHANGED, refreshPending);
  }, [refreshPending]);

  // Remember the run being looked at (and the live run) so navigation keeps it selected.
  const [lastRun, setLastRun] = useState<string | null>(null);
  const [liveRun, setLiveRun] = useState<string | null>(null);
  const runParam = route.params.get("run");
  useEffect(() => {
    if (!runParam) return;
    if (RUN_SCOPED_PAGES.has(route.page) || route.page === "live" || route.page === "demo") setLastRun(runParam);
    if (route.page === "live") setLiveRun(runParam);
  }, [route.page, runParam]);

  // The run whose data source the top bar shows: the one a run-scoped page (or the lab) has open.
  const selectedRun = runParam && (RUN_SCOPED_PAGES.has(route.page) || route.page === "live" || route.page === "demo") ? runParam : null;
  const runDetail = usePolling(selectedRun ? () => api.run(selectedRun) : null, runDetailInterval, [selectedRun]);

  // A recovered API should refresh the wiring right away.
  const online = !!health.data && !health.error;
  const { refresh: refreshSystem } = system;
  useEffect(() => {
    if (online) refreshSystem();
  }, [online, refreshSystem]);

  useEffect(() => {
    document.title = `${PAGE_LABELS[route.page]} · ${PRODUCT_NAME}`;
  }, [route.page]);

  const navHref = (page: PageId) => {
    if (RUN_SCOPED_PAGES.has(page)) return hrefFor(page, { run: lastRun });
    if (page === "live") return hrefFor(page, { run: liveRun });
    return hrefFor(page);
  };

  return (
    <AppShell
      page={route.page}
      navHref={navHref}
      health={health}
      system={system}
      waitingApprovals={pending.data?.length ?? 0}
      approvalsHref={hrefFor("safety", { run: lastRun })}
      selectedRun={selectedRun ? { runId: selectedRun, detail: runDetail } : null}
      account={<OperatorSignIn />}
    >
      <ErrorBoundary resetKey={route.page}>{renderPage(route.page, { route, system })}</ErrorBoundary>
    </AppShell>
  );
}
