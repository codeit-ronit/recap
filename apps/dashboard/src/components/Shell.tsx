import { useEffect, useRef, useState } from "react";
import type { FocusEvent, KeyboardEvent, MouseEvent, ReactNode } from "react";
import type { ApiError } from "../api";
import { NAV_GROUPS, PAGES, PAGE_LABELS } from "../hooks";
import type { PageId, Polled } from "../hooks";
import type { HealthResponse, RunDetail, SystemSummary } from "../types";
import { DataSourceBadge } from "./DataSourceBadge";
import { BackendHint } from "./EmptyState";
import { EnvironmentBanner } from "./EnvironmentBanner";
import { Icon } from "./Icon";
import type { IconName } from "./Icon";
import { LoadingView } from "./StateViews";
import { StatusToken } from "./StatusToken";

/**
 * The RECAP Mission Control frame around every page: a skip link, the environment strip
 * (which environment incidents run against, and the API status), the primary navigation
 * (a sidebar on wide screens, a menu button on narrow ones), the top bar (the selected
 * run's data source, approvals waiting, sign-in) and the <main> landmark.
 *
 * It only renders what App passes in; every status in it comes from the server.
 */

export const PRODUCT_NAME = "RECAP Mission Control";

const NAV_ICONS: Readonly<Record<PageId, IconName>> = {
  overview: "overview",
  demo: "play",
  live: "live",
  timeline: "timeline",
  evidence: "evidence",
  knowledge: "book",
  memory: "memory",
  safety: "safety",
  evals: "evals",
  architecture: "architecture",
};

export interface NavItem {
  page: PageId;
  label: string;
  icon: IconName;
}

/** The navigation, in route order (PAGES), labeled with PAGE_LABELS. */
export const NAV: readonly NavItem[] = PAGES.map((page) => ({ page, label: PAGE_LABELS[page], icon: NAV_ICONS[page] }));

/** The navigation in its groups (NAV_GROUPS). */
export const NAV_SECTIONS: readonly { label: string; items: readonly NavItem[] }[] = NAV_GROUPS.map((group) => ({
  label: group.label,
  items: group.pages.map((page) => NAV.find((item) => item.page === page) as NavItem),
}));

/** A server answer as usePolling gives it (refresh is not needed here). */
export type Loaded<T> = Pick<Polled<T>, "data" | "error" | "loading">;

/** The run a run-scoped page shows (`?run=`), and GET /runs/{id} for it. */
export interface SelectedRun {
  runId: string;
  detail: Loaded<RunDetail>;
}

export interface AppShellProps {
  page: PageId;
  /** The link for each navigation entry (keeps the selected run on run-scoped pages). */
  navHref: (page: PageId) => string;
  health: Loaded<HealthResponse>;
  system: Loaded<SystemSummary>;
  /** Approval requests waiting (GET /approvals/pending). */
  waitingApprovals: number;
  approvalsHref: string;
  /** Set on run-scoped pages: the top bar shows that run's data source. */
  selectedRun: SelectedRun | null;
  /** The sign-in control (OperatorSignIn). */
  account?: ReactNode;
  children: ReactNode;
}

/** GET /health as a status token: online (with the server's version), offline, or checking. */
export function ApiStatus({ health }: { health: Loaded<HealthResponse> }) {
  let token: ReactNode;
  if (health.data && !health.error) {
    token = (
      <StatusToken
        tone="ok"
        variant="plain"
        size="sm"
        label="API online"
        detail={health.data.version ? `v${health.data.version}` : undefined}
        title="GET /health answered"
      />
    );
  } else if (health.error) {
    token = (
      <StatusToken tone="bad" variant="plain" size="sm" label={health.error.offline ? "API offline" : "API error"} title={health.error.message} />
    );
  } else {
    token = <StatusToken tone="neutral" icon="clock" variant="plain" size="sm" label="Checking the API…" />;
  }
  return (
    <span className="api-status" role="status">
      {token}
    </span>
  );
}

function providerProblem(provider: unknown): string | null {
  if (!provider || typeof provider !== "object") return null;
  const error = (provider as { error?: unknown }).error;
  return typeof error === "string" && error ? error : null;
}

/** The selected run and where its data came from (its recorded provider and environment). */
export function RunSource({ run }: { run: SelectedRun }) {
  const { runId, detail } = run;
  const record = detail.data?.record ?? null;
  let source: ReactNode;
  if (record) {
    source = <DataSourceBadge provider={record.provider_info ?? null} environment={record.environment ?? null} size="sm" label="Selected run data source" />;
  } else if (detail.data) {
    source = (
      <span className="muted small">{detail.data.status === "running" ? "starting: no record stored yet" : "no record stored for this run"}</span>
    );
  } else if (detail.error) {
    source = <StatusToken tone="bad" size="sm" label="data source unavailable" title={detail.error.message} />;
  } else {
    source = <LoadingView inline label="loading data source…" />;
  }
  return (
    <div className="run-context">
      <span className="run-context-label">Selected run</span>
      <code className="mono run-context-id" title={runId}>
        {runId}
      </code>
      {source}
    </div>
  );
}

/** Pages that show no single run: what a new run would use (GET /system `provider`). */
export function DefaultSource({ system }: { system: Loaded<SystemSummary> }) {
  const provider = system.data?.provider;
  const problem = providerProblem(provider);
  let source: ReactNode;
  if (provider && problem) {
    source = <StatusToken tone="bad" size="sm" label="PROVIDER NOT READY" detail={provider.name} title={problem} />;
  } else if (provider) {
    source = <DataSourceBadge provider={provider} size="sm" label="Default provider" />;
  } else if (system.error) {
    source = <StatusToken tone="neutral" size="sm" label="not reported" title={system.error.message} />;
  } else {
    source = <LoadingView inline label="checking…" />;
  }
  return (
    <div className="run-context">
      <span className="run-context-label">Default provider</span>
      {source}
    </div>
  );
}

function OfflineBanner({ error }: { error: ApiError }) {
  return (
    <div className="offline-banner" role="alert">
      <Icon name="stop" size={18} />
      <div>
        <strong>The RECAP API is not reachable.</strong> Pages fill in as soon as it is up.
        <span className="sr-only"> ({error.message})</span>
        <BackendHint />
      </div>
    </div>
  );
}

export function AppShell({
  page,
  navHref,
  health,
  system,
  waitingApprovals,
  approvalsHref,
  selectedRun,
  account,
  children,
}: AppShellProps) {
  // The menu on narrow screens (on wide screens the navigation is always shown).
  const [navOpen, setNavOpen] = useState(false);
  const navRef = useRef<HTMLElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);

  useEffect(() => setNavOpen(false), [page]);

  useEffect(() => {
    if (!navOpen) return;
    const outside = (event: PointerEvent) => {
      if (navRef.current && event.target instanceof Node && !navRef.current.contains(event.target)) setNavOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [navOpen]);

  const onNavKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape" && navOpen) {
      setNavOpen(false);
      toggleRef.current?.focus();
    }
  };
  // Tabbing out of the open menu closes it.
  const onNavBlur = (event: FocusEvent<HTMLElement>) => {
    const next = event.relatedTarget;
    if (navOpen && next instanceof Node && !event.currentTarget.contains(next)) setNavOpen(false);
  };
  // The router owns the hash, so the skip link moves focus itself instead of changing it.
  const skipToMain = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    document.getElementById("main")?.focus();
  };

  const waitingText = `${waitingApprovals} approval${waitingApprovals === 1 ? "" : "s"} waiting`;

  return (
    <div className="app">
      <a className="skip-link" href="#main" onClick={skipToMain}>
        Skip to main content
      </a>

      <EnvironmentBanner environment={system.data?.environment} health={health.data}>
        <ApiStatus health={health} />
      </EnvironmentBanner>

      <nav ref={navRef} className={navOpen ? "sidebar nav-open" : "sidebar"} aria-label="Primary" onKeyDown={onNavKeyDown} onBlur={onNavBlur}>
        <div className="sidebar-head">
          <a className="brand" href={navHref("overview")}>
            <span className="brand-mark" aria-hidden="true">
              R
            </span>
            <span className="brand-text">
              <span className="brand-name">RECAP</span> <span className="brand-sub">Mission Control</span>
            </span>
          </a>
          <button
            ref={toggleRef}
            type="button"
            className="btn btn-ghost nav-toggle"
            aria-expanded={navOpen}
            aria-controls="primary-nav-list"
            onClick={() => setNavOpen((open) => !open)}
          >
            <Icon name={navOpen ? "x" : "menu"} size={18} />
            Menu
          </button>
        </div>
        <div id="primary-nav-list" className="nav-sections">
          {NAV_SECTIONS.map((section, index) => (
            <div key={section.label} className="nav-section">
              <h2 className="nav-section-label" id={`nav-section-${index}`}>
                {section.label}
              </h2>
              <ul className="nav-list" aria-labelledby={`nav-section-${index}`}>
                {section.items.map((item) => {
                  const current = item.page === page;
                  return (
                    <li key={item.page}>
                      <a className={current ? "nav-item active" : "nav-item"} href={navHref(item.page)} aria-current={current ? "page" : undefined}>
                        <Icon name={item.icon} size={17} />
                        <span className="nav-label">{item.label}</span>
                        {item.page === "safety" && waitingApprovals > 0 ? (
                          <span className="nav-badge" title={waitingText}>
                            <span aria-hidden="true">{waitingApprovals}</span>
                            <span className="sr-only">, {waitingText}</span>
                          </span>
                        ) : null}
                      </a>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </div>
        <div className="sidebar-foot">
          <div>
            {PRODUCT_NAME}
            {health.data?.version ? ` · v${health.data.version}` : ""}
          </div>
          <div className="muted">Every view is built from the API: no hardcoded metrics, no sample runs.</div>
        </div>
      </nav>

      <div className="main">
        <header className="topbar">
          {selectedRun ? <RunSource run={selectedRun} /> : <DefaultSource system={system} />}
          <span className="topbar-spacer" />
          {waitingApprovals > 0 ? (
            <a className="topbar-approvals" href={approvalsHref}>
              <Icon name="key" size={14} />
              {waitingText}
            </a>
          ) : null}
          {account}
        </header>
        <main id="main" className="content" tabIndex={-1}>
          {health.error?.offline ? <OfflineBanner error={health.error} /> : null}
          {children}
        </main>
      </div>
    </div>
  );
}
