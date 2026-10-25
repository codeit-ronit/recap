import type { ReactNode } from "react";
import type { ApiError } from "../api";
import type { Polled } from "../hooks";
import { Icon } from "./Icon";
import type { IconName } from "./Icon";

/**
 * The three states every data view has besides "here is the data": loading, empty (always
 * with the reason there is nothing) and error (the server's message, and a way to retry).
 * PolledView picks the right one for a usePolling result.
 */

/** A spinner and a label; announced politely to screen readers. */
export function LoadingView({ label = "Loading…", inline = false }: { label?: string; inline?: boolean }) {
  const Tag = inline ? "span" : "div";
  return (
    <Tag className={inline ? "loading inline" : "loading"} role="status">
      <span className="spinner" aria-hidden="true" />
      {label}
    </Tag>
  );
}

/** Shared markup of EmptyView and the older EmptyState. */
export function EmptyBlock({
  title,
  reason,
  icon = "info",
  action,
}: {
  title: ReactNode;
  reason?: ReactNode;
  icon?: IconName;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <Icon name={icon} size={22} />
      <div className="empty-title">{title}</div>
      {reason ? <div className="empty-body">{reason}</div> : null}
      {action ? <div className="empty-action">{action}</div> : null}
    </div>
  );
}

/** Nothing to show, and why (e.g. "No runs have been recorded yet"). The reason is required. */
export function EmptyView({
  title,
  reason,
  icon,
  action,
}: {
  title: ReactNode;
  reason: ReactNode;
  icon?: IconName;
  action?: ReactNode;
}) {
  return <EmptyBlock title={title} reason={reason} icon={icon} action={action} />;
}

type ErrorLike = ApiError | Error | string;

function messageOf(error: ErrorLike): string {
  return typeof error === "string" ? error : error.message;
}

function httpStatusOf(error: ErrorLike): number | null {
  if (typeof error === "string" || !("status" in error)) return null;
  return typeof error.status === "number" && error.status > 0 ? error.status : null;
}

function isOffline(error: ErrorLike): boolean {
  return typeof error !== "string" && "offline" in error && error.offline === true;
}

function RetryButton({ onRetry, label }: { onRetry: () => void; label: string }) {
  return (
    <button type="button" className="btn btn-small state-retry" onClick={onRetry}>
      <Icon name="refresh" size={13} />
      {label}
    </button>
  );
}

/**
 * A failed request: its message (the server's own `detail` when it sent one) and, with
 * `onRetry`, a Retry button. When the API cannot be reached at all the view waits instead
 * (the shell shows the one "API not reachable" banner with the start command).
 */
export function ErrorView({
  error,
  title,
  onRetry,
  retryLabel = "Retry",
}: {
  error: ErrorLike;
  title?: string;
  onRetry?: () => void;
  retryLabel?: string;
}) {
  const message = messageOf(error);
  if (isOffline(error)) {
    return (
      <div className="error-state waiting" role="status">
        <Icon name="clock" size={18} />
        <div className="error-text">
          <div className="error-title">{title ?? "Waiting for the RECAP API"}</div>
          <div className="error-message">
            {title ? `${message} ` : ""}This view loads as soon as the backend responds.
          </div>
        </div>
        {onRetry ? <RetryButton onRetry={onRetry} label={retryLabel} /> : null}
      </div>
    );
  }
  const status = httpStatusOf(error);
  return (
    <div className="error-state" role="alert">
      <Icon name="alert" size={18} />
      <div className="error-text">
        <div className="error-title">
          {title ?? "Request failed"}
          {status ? <span className="mono muted"> · HTTP {status}</span> : null}
        </div>
        <div className="error-message">{message}</div>
      </div>
      {onRetry ? <RetryButton onRetry={onRetry} label={retryLabel} /> : null}
    </div>
  );
}

/** Shown above data that is still displayed after a refresh failed. */
function StaleNotice({ error, onRetry }: { error: ErrorLike; onRetry: () => void }) {
  return (
    <div className="stale-note" role="status">
      <Icon name="alert" size={14} />
      <span>
        Showing the last data received.{" "}
        {isOffline(error) ? "The API is not reachable right now." : `Refreshing failed: ${messageOf(error)}`}
      </span>
      <RetryButton onRetry={onRetry} label="Retry" />
    </div>
  );
}

/**
 * Renders a usePolling result: Loading until the first answer, Error (with Retry = refresh)
 * if there is none, Empty when `empty(data)` gives a reason, else `children(data)`. Data
 * that is already shown stays on screen when a later refresh fails (with a notice).
 */
export function PolledView<T>({
  polled,
  children,
  empty,
  emptyTitle = "Nothing to show",
  emptyIcon,
  loadingLabel,
  errorTitle,
  idle = null,
}: {
  polled: Pick<Polled<T>, "data" | "error" | "loading" | "refresh">;
  children: (data: T) => ReactNode;
  /** Why there is nothing to show (rendered as an EmptyView), or null when there is something. */
  empty?: (data: T) => ReactNode | null | undefined;
  emptyTitle?: ReactNode;
  emptyIcon?: IconName;
  loadingLabel?: string;
  errorTitle?: string;
  /** Shown when nothing was requested (usePolling with a null fetcher). */
  idle?: ReactNode;
}) {
  const { data, error, loading, refresh } = polled;
  if (data === undefined) {
    if (error) return <ErrorView error={error} title={errorTitle} onRetry={refresh} />;
    return loading ? <LoadingView label={loadingLabel} /> : <>{idle}</>;
  }
  const reason = empty?.(data);
  const body = reason ? <EmptyView title={emptyTitle} reason={reason} icon={emptyIcon} /> : children(data);
  return (
    <>
      {error ? <StaleNotice error={error} onRetry={refresh} /> : null}
      {body}
    </>
  );
}
