import type { ReactNode } from "react";
import type { Tone } from "../format";
import { Icon } from "./Icon";
import type { IconName } from "./Icon";

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        {description ? <p className="page-description">{description}</p> : null}
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </header>
  );
}

export function Card({
  title,
  subtitle,
  actions,
  children,
  flush = false,
  tone,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
  /** No body padding (for tables that run edge to edge). */
  flush?: boolean;
  /** Colored border for cards that demand attention (e.g. a pending approval). */
  tone?: Tone;
}) {
  return (
    <section className={tone ? `card card-${tone}` : "card"}>
      {title || actions ? (
        <div className="card-head">
          <div className="card-titles">
            {title ? <h2 className="card-title">{title}</h2> : null}
            {subtitle ? <div className="card-subtitle">{subtitle}</div> : null}
          </div>
          {actions ? <div className="card-actions">{actions}</div> : null}
        </div>
      ) : null}
      <div className={flush ? "card-body flush" : "card-body"}>{children}</div>
    </section>
  );
}

/** A labeled figure (label, value, optional note). Values use proportional figures. */
export function StatTile({
  label,
  value,
  note,
  tone = "neutral",
}: {
  label: ReactNode;
  value: ReactNode;
  note?: ReactNode;
  tone?: Tone;
}) {
  return (
    <div className={`stat-tile tone-${tone}`}>
      <div className="stat-label">
        <span className="stat-dot" aria-hidden="true" />
        {label}
      </div>
      <div className="stat-value">{value}</div>
      {note ? <div className="stat-note">{note}</div> : null}
    </div>
  );
}

/** A thin horizontal meter (value / max). The number is always printed next to it. */
export function Meter({
  value,
  max,
  label,
  width = 96,
}: {
  value: number;
  max: number;
  label?: ReactNode;
  width?: number;
}) {
  const pct = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
  return (
    <span className="meter-row">
      <span className="meter tone-accent" style={{ width }} role="presentation">
        <span className="meter-fill" style={{ width: `${pct * 100}%` }} />
      </span>
      {label !== undefined ? <span className="meter-label">{label}</span> : null}
    </span>
  );
}

/** A two-column list of labeled values. */
export function KeyValues({ rows, className }: { rows: [ReactNode, ReactNode][]; className?: string }) {
  return (
    <dl className={className ? `kv ${className}` : "kv"}>
      {rows.map(([k, v], i) => (
        <div className="kv-row" key={i}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A highlighted note (explanations, honesty labels, warnings). */
export function Notice({
  tone = "info",
  icon = "info",
  title,
  children,
}: {
  tone?: Tone;
  icon?: IconName;
  title?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className={`notice tone-${tone}`}>
      <Icon name={icon} size={16} />
      <div>
        {title ? <div className="notice-title">{title}</div> : null}
        {children ? <div className="notice-body">{children}</div> : null}
      </div>
    </div>
  );
}

/** A segmented control (radio group styled as buttons). */
export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
  vertical = false,
}: {
  value: T;
  options: { value: T; label: ReactNode }[];
  onChange: (value: T) => void;
  label: string;
  /** Stack long options as a list instead of wrapping them. */
  vertical?: boolean;
}) {
  return (
    <div className={vertical ? "segmented vertical" : "segmented"} role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={o.value === value}
          className={o.value === value ? "active" : ""}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** An accessible on/off switch. */
export function Toggle({
  checked,
  onChange,
  label,
  hint,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: ReactNode;
  hint?: ReactNode;
}) {
  return (
    <label className="toggle">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="toggle-track" aria-hidden="true">
        <span className="toggle-thumb" />
      </span>
      <span className="toggle-text">
        <span>{label}</span>
        {hint ? <span className="toggle-hint">{hint}</span> : null}
      </span>
    </label>
  );
}
