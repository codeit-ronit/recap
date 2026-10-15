import type { ReactNode } from "react";
import {
  flagTone,
  humanize,
  hypothesisTone,
  outcomeTone,
  stateLabel,
  stateTone,
  verdictLabel,
  verdictTone,
} from "../format";
import type { Tone } from "../format";
import { Icon } from "./Icon";

/** A small rounded label. Color is never the only signal: chips always carry text. */
export function Chip({
  tone = "neutral",
  children,
  mono = false,
  dot = false,
  outline = false,
  title,
}: {
  tone?: Tone;
  children: ReactNode;
  mono?: boolean;
  dot?: boolean;
  outline?: boolean;
  title?: string;
}) {
  const classes = ["chip", `tone-${tone}`, mono ? "mono" : "", outline ? "chip-outline" : ""].filter(Boolean).join(" ");
  return (
    <span className={classes} title={title}>
      {dot ? <span className="chip-dot" aria-hidden="true" /> : null}
      {children}
    </span>
  );
}

export function StateChip({ state }: { state: string | null | undefined }) {
  return (
    <Chip tone={stateTone(state)} dot title={state ?? undefined}>
      {stateLabel(state)}
    </Chip>
  );
}

export function VerdictChip({ verdict }: { verdict: string | null | undefined }) {
  const tone = verdictTone(verdict);
  return (
    <Chip tone={tone}>
      <Icon name={tone === "ok" ? "check" : tone === "bad" ? "x" : "key"} size={12} />
      {verdictLabel(verdict)}
    </Chip>
  );
}

export function OutcomeChip({ outcome }: { outcome: string | null | undefined }) {
  return (
    <Chip tone={outcomeTone(outcome)} dot>
      {outcome ? humanize(outcome) : "—"}
    </Chip>
  );
}

export function HypothesisChip({ status }: { status: string | null | undefined }) {
  return (
    <Chip tone={hypothesisTone(status)} dot>
      {status ?? "—"}
    </Chip>
  );
}

/** YES (green) / NO (red) chip; null means the question was never checked. */
export function YesNoChip({
  value,
  yes = "yes",
  no = "no",
}: {
  value: boolean | null | undefined;
  yes?: string;
  no?: string;
}) {
  if (value === null || value === undefined) return <Chip>unchecked</Chip>;
  return (
    <Chip tone={value ? "ok" : "bad"}>
      <Icon name={value ? "check" : "x"} size={12} />
      {value ? yes : no}
    </Chip>
  );
}

export function FlagChip({ flag }: { flag: string }) {
  const tone = flagTone(flag);
  return (
    <Chip tone={tone} mono title={flag === "prompt_injection_suspected" ? "Injection-like text: shown as untrusted data, never followed" : undefined}>
      {tone === "warn" ? <Icon name="alert" size={12} /> : null}
      {flag}
    </Chip>
  );
}

export function SimulatedBadge() {
  return (
    <Chip tone="warn" outline>
      simulated
    </Chip>
  );
}

/** Inline monospace text for ids, tool names and digests. */
export function Mono({ children, title, nowrap = false }: { children: ReactNode; title?: string; nowrap?: boolean }) {
  return (
    <code className={nowrap ? "mono nowrap" : "mono"} title={title}>
      {children}
    </code>
  );
}
