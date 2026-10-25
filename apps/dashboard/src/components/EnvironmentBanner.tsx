import type { ReactNode } from "react";
import type { Tone } from "../format";
import type { EnvironmentInfo, HealthResponse } from "../types";
import { Icon } from "./Icon";
import type { IconName } from "./Icon";

/** The words of the persistent indicator while the server reports a simulated environment. */
export const SIMULATED_ENVIRONMENT = "SIMULATED ENVIRONMENT";

export interface EnvironmentStatus {
  /**
   * simulated / live: GET /system said so (`environment.is_simulated`) · reported: only
   * GET /health has answered (its server-made label is shown as is) · unknown: neither has.
   */
  state: "simulated" | "live" | "reported" | "unknown";
  label: string;
  detail: string;
  /** Server-written description (the adapter's own words), shown on wide screens. */
  description: string | null;
  tone: Tone;
  icon: IconName;
  title: string;
}

type EnvironmentFields = Partial<Pick<EnvironmentInfo, "kind" | "name" | "namespace" | "is_simulated" | "description">>;
type HealthFields = Partial<Pick<HealthResponse, "environment" | "label">>;

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/**
 * Which environment is plugged in, from the server only: GET /system (`environment`) first,
 * else the label GET /health made. Before either answers the banner says so instead of
 * guessing "simulated" or "live".
 */
export function environmentStatus(
  environment: EnvironmentFields | null | undefined,
  health?: HealthFields | null,
): EnvironmentStatus {
  if (environment && typeof environment.is_simulated === "boolean") {
    const kind = text(environment.kind);
    const facts = [text(environment.name), kind ? `kind ${kind}` : null, text(environment.namespace) ? `namespace ${text(environment.namespace)}` : null]
      .filter(Boolean)
      .join(" · ");
    const description = text(environment.description);
    if (environment.is_simulated) {
      return {
        state: "simulated",
        label: SIMULATED_ENVIRONMENT,
        detail: facts,
        description,
        tone: "warn",
        icon: "flask",
        title: description ?? "Incidents run against a simulated environment. No real system is changed.",
      };
    }
    return {
      state: "live",
      label: `LIVE ENVIRONMENT · ${kind ?? "kind not reported"}`,
      detail: facts,
      description,
      tone: "bad",
      icon: "alert",
      title: "The server reports a live environment adapter: approved mitigations change a real system.",
    };
  }
  const reported = text(health?.label);
  if (reported) {
    const kind = text(health?.environment);
    return {
      state: "reported",
      label: reported,
      detail: kind ? `kind ${kind} · confirming with GET /system…` : "confirming with GET /system…",
      description: null,
      tone: "neutral",
      icon: "clock",
      title: "Reported by GET /health; GET /system has not answered yet.",
    };
  }
  return {
    state: "unknown",
    label: "ENVIRONMENT NOT CONFIRMED",
    detail: "waiting for the RECAP API",
    description: null,
    tone: "neutral",
    icon: "clock",
    title: "Nothing is labeled simulated or live until the API reports which environment is plugged in.",
  };
}

/**
 * The full-width strip at the top of every page: which environment incidents run against,
 * as GET /system (or, until it answers, GET /health) reports it. `children` go on its right
 * (the shell puts the API status there).
 */
export function EnvironmentBanner({
  environment,
  health,
  children,
}: {
  environment?: EnvironmentFields | null;
  health?: HealthFields | null;
  children?: ReactNode;
}) {
  const status = environmentStatus(environment, health);
  return (
    <section className={`env-banner env-${status.state} tone-${status.tone}`} aria-label="Environment">
      <span className="env-main" role="status" title={status.title}>
        <Icon name={status.icon} size={16} />
        <strong className="env-label">{status.label}</strong>
        {status.detail ? <span className="env-detail">{status.detail}</span> : null}
        {status.description ? <span className="env-description">{status.description}</span> : null}
      </span>
      {children ? <span className="env-aside">{children}</span> : null}
    </section>
  );
}
