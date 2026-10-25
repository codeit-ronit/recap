import type { ReactNode } from "react";
import { stateLabel, stateTone } from "../format";
import type { Tone } from "../format";
import { Icon } from "./Icon";
import type { IconName } from "./Icon";

/**
 * The icon each tone shows unless the caller picks one: a different SHAPE per meaning
 * (check, triangle, cross, "i", dot, dash), so a status never depends on color alone.
 */
export const TONE_ICONS: Readonly<Record<Tone, IconName>> = {
  ok: "check",
  warn: "alert",
  bad: "x",
  info: "info",
  accent: "dot",
  neutral: "minus",
};

/**
 * A status as icon + text, tinted by tone. The text carries the meaning; the icon and the
 * color repeat it. Use it for every status the server reports (run state, provider
 * readiness, verdicts, API health); never show a status as a colored dot alone.
 */
export function StatusToken({
  tone = "neutral",
  label,
  icon,
  detail,
  title,
  size = "md",
  variant = "soft",
  className,
}: {
  tone?: Tone;
  /** The status in words. Required: it is what a screen reader and a color-blind reader get. */
  label: ReactNode;
  /** Defaults to the tone's icon (TONE_ICONS). */
  icon?: IconName;
  /** Secondary text after the label, e.g. a model name or a version. */
  detail?: ReactNode;
  /** Extra explanation on hover (never the only place a fact is shown). */
  title?: string;
  size?: "sm" | "md";
  /** soft: tinted box (default) · outline: border only · plain: no box, for use inside text. */
  variant?: "soft" | "outline" | "plain";
  className?: string;
}) {
  const classes = ["status-token", `tone-${tone}`, `status-${variant}`, size === "sm" ? "status-sm" : "", className ?? ""]
    .filter(Boolean)
    .join(" ");
  const hasDetail = detail !== undefined && detail !== null && detail !== "";
  return (
    <span className={classes} title={title}>
      <Icon name={icon ?? TONE_ICONS[tone]} size={size === "sm" ? 12 : 14} />
      <span className="status-label">{label}</span>
      {hasDetail ? <span className="status-detail">{detail}</span> : null}
    </span>
  );
}

/** The icon of a lifecycle state: resolved, stopped, waiting for a person, or still in progress. */
export function stateIcon(state: string | null | undefined): IconName {
  switch (state) {
    case "RESOLVED":
      return "check";
    case "FAILED":
      return "x";
    case "BLOCKED":
    case "BUDGET_EXHAUSTED":
    case "QUOTA_EXHAUSTED":
      return "stop";
    case "AWAITING_APPROVAL":
      return "key";
    case "CREATED":
      return "dot";
    case null:
    case undefined:
    case "":
      return "minus";
    default:
      return "clock"; // RETRIEVING ... VERIFYING: in progress
  }
}

/** A run's lifecycle state (IncidentState from the server) as a status token. */
export function StateToken({ state, size }: { state: string | null | undefined; size?: "sm" | "md" }) {
  return <StatusToken tone={stateTone(state)} icon={stateIcon(state)} label={stateLabel(state)} title={state ?? undefined} size={size} />;
}
