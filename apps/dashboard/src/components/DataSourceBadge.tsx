import type { Tone } from "../format";
import type { EnvironmentInfo, ProviderInfo } from "../types";
import type { IconName } from "./Icon";
import { StatusToken } from "./StatusToken";

/**
 * Where data on screen came from, read ONLY from fields the server reports, so a scripted or
 * replayed run can never pass for live model reasoning, and nothing suggests a real system:
 *
 *   decisions  ProviderInfo.origin (provider class): scripted · local_llm · free_hosted_llm ·
 *              paid_llm · replay (+ replay_of: what the recording captured)
 *   target     EnvironmentInfo.is_simulated / kind: the environment actions ran against
 *
 * Nothing here is inferred from a provider's or a run's name.
 */

export type SourceKind = "scripted" | "local_llm" | "free_hosted_llm" | "paid_llm" | "replay" | "unknown";

/** The provider fields a data source is read from (/system `provider`, a run's `provider_info`, ...). */
export type SourceFields = Partial<Pick<ProviderInfo, "name" | "kind" | "origin" | "model" | "replay_of" | "red_team">>;

/** The environment fields a target is read from (/system `environment`, a run's `environment`). */
export type TargetFields = Partial<Pick<EnvironmentInfo, "kind" | "name" | "is_simulated" | "description">>;

export interface SourceView {
  kind: SourceKind;
  label: string;
  /** The model the server named (for a replay: the recorded model), or null. */
  detail: string | null;
  tone: Tone;
  icon: IconName;
  title: string;
}

export interface TargetView {
  kind: "simulated" | "live" | "unknown";
  label: string;
  tone: Tone;
  icon: IconName;
  title: string;
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/** The decision source of a provider (or of a run's recorded provider). */
export function describeSource(provider: SourceFields | null | undefined): SourceView {
  // Runs recorded before provider origins existed (M2) have a kind at most.
  const origin = provider?.origin ?? (provider?.kind === "scripted" || provider?.kind === "replay" ? provider.kind : undefined);
  const model = text(provider?.model);
  switch (origin) {
    case "scripted":
      return {
        kind: "scripted",
        label: "SCRIPTED BASELINE",
        detail: null,
        tone: "neutral",
        icon: "code",
        title: "A scripted baseline policy (fixed playbooks), not an LLM. No model was called.",
      };
    case "local_llm":
      return {
        kind: "local_llm",
        label: "LOCAL LLM",
        detail: model,
        tone: "accent",
        icon: "cpu",
        title: "Real inference by a model running on this machine (Ollama). No paid or remote API.",
      };
    case "free_hosted_llm":
      return {
        kind: "free_hosted_llm",
        label: "FREE HOSTED LLM",
        detail: model,
        tone: "info",
        icon: "cloud",
        title: "Real inference on a free hosted plan, which RECAP checks before every request.",
      };
    case "paid_llm":
      return {
        kind: "paid_llm",
        label: "PAID LLM",
        detail: model,
        tone: "bad",
        icon: "alert",
        title: "A billed API. This project keeps it disabled ($0 budget).",
      };
    case "replay": {
      const of = provider?.replay_of ?? null;
      const recordedModel = text(of?.model) ?? model;
      const facts = [
        text(of?.original_provider) ? `recorded with ${text(of?.original_provider)}` : null,
        recordedModel ? `model ${recordedModel}` : null,
        text(of?.created_at) ? `on ${text(of?.created_at)}` : null,
        text(of?.recording_id) ? `recording ${text(of?.recording_id)}` : null,
      ].filter(Boolean);
      return {
        kind: "replay",
        label: "REPLAY · RECORDED REAL-LLM RUN",
        detail: recordedModel,
        tone: "warn",
        icon: "rewind",
        title: `The recorded answers of an earlier real-model run${facts.length ? ` (${facts.join(", ")})` : ""}, replayed offline: no model is called now.`,
      };
    }
    default:
      return {
        kind: "unknown",
        label: "SOURCE NOT RECORDED",
        detail: null,
        tone: "neutral",
        icon: "minus",
        title: "The server did not report where these decisions came from.",
      };
  }
}

/** The environment actions ran (or would run) against. */
export function describeTarget(environment: TargetFields | null | undefined): TargetView {
  if (!environment || typeof environment.is_simulated !== "boolean") {
    return {
      kind: "unknown",
      label: "TARGET NOT RECORDED",
      tone: "neutral",
      icon: "minus",
      title: "The server did not report which environment this ran against.",
    };
  }
  if (environment.is_simulated) {
    const about = [text(environment.name), text(environment.description)].filter(Boolean).join(": ");
    return {
      kind: "simulated",
      label: "SIMULATED TARGET",
      tone: "warn",
      icon: "flask",
      title: about || "A simulated environment: no real system is changed.",
    };
  }
  return {
    kind: "live",
    label: `LIVE TARGET · ${text(environment.kind) ?? "kind not reported"}`,
    tone: "bad",
    icon: "alert",
    title: "The server reports a live environment adapter: approved actions change a real system.",
  };
}

/** The fields of a typed run summary (GET /runs, GET /runs/{id} `summary`, M4) the badge reads. */
export interface SummarySourceFields {
  provider_class?: string | null;
  provider_kind?: string | null;
  model?: string | null;
  environment_kind?: string | null;
  environment_simulated?: boolean | null;
  replay?: {
    model?: string | null;
    original_provider?: string | null;
    recorded_at?: string | null;
    recording_id?: string | null;
  } | null;
}

/** DataSourceBadge props from a run summary: `<DataSourceBadge {...summarySource(run)} />`. */
export function summarySource(run: SummarySourceFields): { provider: SourceFields; environment: TargetFields | null } {
  const replay = run.replay ?? null;
  return {
    provider: {
      origin: (run.provider_class ?? undefined) as SourceFields["origin"],
      kind: (run.provider_kind ?? undefined) as SourceFields["kind"],
      model: run.model ?? null,
      replay_of: replay
        ? {
            model: replay.model ?? null,
            original_provider: replay.original_provider ?? null,
            created_at: replay.recorded_at ?? null,
            recording_id: replay.recording_id ?? null,
          }
        : null,
    },
    environment:
      typeof run.environment_simulated === "boolean"
        ? { kind: run.environment_kind ?? undefined, is_simulated: run.environment_simulated }
        : null,
  };
}

/**
 * The data source of a run (or of the server's default provider): decisions, then target.
 *
 * `environment`: leave it out to show the decision source alone; pass null when the record
 * has no environment ("TARGET NOT RECORDED").
 */
export function DataSourceBadge({
  provider,
  environment,
  size,
  label = "Data source",
}: {
  provider: SourceFields | null | undefined;
  environment?: TargetFields | null;
  size?: "sm" | "md";
  /** The group's accessible name. */
  label?: string;
}) {
  const source = describeSource(provider);
  const target = environment === undefined ? null : describeTarget(environment);
  return (
    <span className="data-source" role="group" aria-label={label}>
      <StatusToken tone={source.tone} icon={source.icon} label={source.label} detail={source.detail} title={source.title} size={size} />
      {provider?.red_team ? (
        <StatusToken
          tone="bad"
          icon="alert"
          label="RED-TEAM PLANNER"
          title="A deliberately compromised planner proposes whatever injected text asks for, to prove the gateway blocks it."
          size={size}
        />
      ) : null}
      {target ? (
        <>
          <span className="data-source-sep" aria-hidden="true">
            /
          </span>
          <StatusToken tone={target.tone} icon={target.icon} label={target.label} title={target.title} size={size} />
        </>
      ) : null}
    </span>
  );
}
