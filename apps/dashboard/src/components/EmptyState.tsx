import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";
import { API_BASE, BACKEND_COMMAND } from "../api";
import type { ApiError } from "../api";
import { hrefFor } from "../hooks";
import { Icon } from "./Icon";
import type { IconName } from "./Icon";

export function EmptyState({
  title,
  children,
  icon = "info",
  action,
}: {
  title: ReactNode;
  children?: ReactNode;
  icon?: IconName;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <Icon name={icon} size={22} />
      <div className="empty-title">{title}</div>
      {children ? <div className="empty-body">{children}</div> : null}
      {action ? <div className="empty-action">{action}</div> : null}
    </div>
  );
}

export function NoRuns() {
  return (
    <EmptyState
      title="No runs yet — start one on Live Incident Run"
      icon="live"
      action={
        <a className="btn btn-primary" href={hrefFor("live")}>
          <Icon name="play" size={14} /> Open Live Incident Run
        </a>
      }
    >
      Every view on this dashboard is built from real runs recorded by the backend.
    </EmptyState>
  );
}

/** The backend start command, shown whenever the API cannot be reached. */
export function BackendHint() {
  return (
    <div className="backend-hint">
      <span>Start the API from the repository root:</span>
      <code className="mono cmd">{BACKEND_COMMAND}</code>
      <span className="muted">
        It listens on http://127.0.0.1:8000
        {API_BASE === "/api" ? " (the dev server proxies /api to it)." : "."}
      </span>
    </div>
  );
}

export function ErrorState({ error, title }: { error: ApiError | Error; title?: string }) {
  const offline = "offline" in error && error.offline;
  const status = "status" in error && typeof error.status === "number" && error.status > 0 ? error.status : null;
  if (offline) {
    // The shell shows one banner with the start command; each view just waits.
    return (
      <div className="error-state waiting" role="status">
        <Icon name="clock" size={18} />
        <div>
          <div className="error-title">{title ?? "Waiting for the RECAP API"}</div>
          <div className="error-message">
            {title ? `${error.message} ` : ""}This view loads as soon as the backend responds.
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className="error-state" role="alert">
      <Icon name="alert" size={18} />
      <div>
        <div className="error-title">
          {title ?? "Request failed"}
          {status ? <span className="mono muted"> · HTTP {status}</span> : null}
        </div>
        <div className="error-message">{error.message}</div>
      </div>
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="loading" role="status">
      <span className="spinner" aria-hidden="true" />
      {label}
    </div>
  );
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
      return <ErrorState error={this.state.error} title="This view hit an unexpected error" />;
    }
    return this.props.children;
  }
}
