import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";
import { API_BASE, BACKEND_COMMAND } from "../api";
import type { ApiError } from "../api";
import { PAGE_LABELS, hrefFor } from "../hooks";
import { Icon } from "./Icon";
import type { IconName } from "./Icon";
import { EmptyBlock, ErrorView, LoadingView } from "./StateViews";

/*
 * The state views pages have used since V1. They render the shared StateViews
 * (src/components/StateViews.tsx); new code can use those directly (EmptyView requires a
 * reason, ErrorView takes onRetry, PolledView handles a whole usePolling result).
 */

export function EmptyState({
  title,
  children,
  icon = "info",
  action,
}: {
  title: ReactNode;
  /** Why there is nothing to show. */
  children?: ReactNode;
  icon?: IconName;
  action?: ReactNode;
}) {
  return <EmptyBlock title={title} reason={children} icon={icon} action={action} />;
}

export function NoRuns() {
  const lab = PAGE_LABELS.live;
  return (
    <EmptyState
      title={`No runs yet. Start one in the ${lab}`}
      icon="live"
      action={
        <span className="chip-row">
          <a className="btn btn-primary" href={hrefFor("live")}>
            <Icon name="play" size={14} /> Open the {lab}
          </a>
          <a className="btn" href={hrefFor("published")}>
            <Icon name="package" size={14} /> Open a published real-cluster run
          </a>
        </span>
      }
    >
      Every view on this dashboard is built from runs the backend recorded; this server has none yet. The real Kubernetes runs published with the
      repository can be opened offline.
    </EmptyState>
  );
}

/** The backend start command, shown whenever the API cannot be reached. */
/** Where this page expects the API: the dev server's proxy, the server that served the page, or VITE_API_URL. */
export function apiWhere(base: string = API_BASE, origin: string | null = typeof window === "undefined" ? null : window.location.origin): string {
  if (base === "/api") return "It listens on http://127.0.0.1:8000 (the dev server proxies /api to it).";
  if (base === "") return origin ? `It served this page, so it is expected back at ${origin}.` : "It serves this page.";
  return `This page expects it at ${base} (VITE_API_URL).`;
}

export function BackendHint() {
  return (
    <div className="backend-hint">
      <span>Start the API from the repository root:</span>
      <code className="mono cmd">{BACKEND_COMMAND}</code>
      <span className="muted">{apiWhere()}</span>
    </div>
  );
}

export function ErrorState({ error, title, onRetry }: { error: ApiError | Error; title?: string; onRetry?: () => void }) {
  return <ErrorView error={error} title={title} onRetry={onRetry} />;
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return <LoadingView label={label} />;
}

/** Keeps one broken view (e.g. an unexpected payload) from blanking the whole dashboard. */
export class ErrorBoundary extends Component<{ children: ReactNode; resetKey?: string }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("RECAP dashboard view crashed", error, info.componentStack);
  }

  componentDidUpdate(prev: { resetKey?: string }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
  }

  render() {
    if (this.state.error) {
      return (
        <ErrorView
          error={this.state.error}
          title="This view hit an unexpected error"
          retryLabel="Try again"
          onRetry={() => this.setState({ error: null })}
        />
      );
    }
    return this.props.children;
  }
}
