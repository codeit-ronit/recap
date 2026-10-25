import { useId } from "react";
import type { ReactNode } from "react";

/**
 * Filters for long lists (trace events by agent, round, tool, ...). Options are built from
 * the data itself (filterOptions), so a filter never offers a value the server did not send.
 * A filter with few options shows as toggle chips, a longer one as a select.
 */

export interface FilterOption {
  value: string;
  /** Shown instead of the raw value. */
  label?: string;
  /** How many items have this value (shown next to it). */
  count?: number;
}

export interface FilterSpec {
  key: string;
  label: string;
  options: readonly FilterOption[];
  /** Default: chips up to CHIP_LIMIT options, a select beyond. */
  display?: "chips" | "select";
  /** The "no filter" choice; default "All". */
  allLabel?: string;
}

/** The chosen value per filter key; null, undefined or "" = no filter. */
export type FilterValues = Readonly<Record<string, string | null | undefined>>;

/** What an item has for one filter: one value, several (an event that names two tools), or none. */
export type FilterField = string | number | null | undefined | readonly (string | number | null | undefined)[];

export const CHIP_LIMIT = 6;

function fieldValues(field: FilterField): string[] {
  const list = Array.isArray(field) ? field : [field];
  return list.filter((v) => v !== null && v !== undefined && v !== "").map((v) => String(v));
}

function isActive(value: string | null | undefined): value is string {
  return value !== null && value !== undefined && value !== "";
}

/**
 * The distinct values items have for one field, with how many items have each. Numbers
 * (rounds) sort numerically, text alphabetically; empty values are left out.
 */
export function filterOptions<T>(
  items: readonly T[],
  read: (item: T) => FilterField,
  label?: (value: string) => string,
): FilterOption[] {
  const counts = new Map<string, number>();
  for (const item of items) {
    for (const value of new Set(fieldValues(read(item)))) counts.set(value, (counts.get(value) ?? 0) + 1);
  }
  const values = [...counts.keys()];
  const numeric = values.every((v) => v.trim() !== "" && Number.isFinite(Number(v)));
  values.sort(numeric ? (a, b) => Number(a) - Number(b) : (a, b) => a.localeCompare(b));
  return values.map((value) => ({ value, label: label ? label(value) : value, count: counts.get(value) ?? 0 }));
}

/** The items that match every active filter (an item with several values matches if any does). */
export function applyFilters<T>(
  items: readonly T[],
  values: FilterValues,
  readers: Readonly<Record<string, (item: T) => FilterField>>,
): T[] {
  const active = Object.entries(values).filter(
    (entry): entry is [string, string] => isActive(entry[1]) && typeof readers[entry[0]] === "function",
  );
  if (!active.length) return items.slice();
  return items.filter((item) => active.every(([key, wanted]) => fieldValues(readers[key](item)).includes(wanted)));
}

/** How many of `filters` have a value chosen. */
export function activeFilterCount(values: FilterValues, filters?: readonly Pick<FilterSpec, "key">[]): number {
  const keys = filters ? filters.map((f) => f.key) : Object.keys(values);
  return keys.filter((key) => isActive(values[key])).length;
}

/** A value chosen through a deep link but absent from the data stays visible (and selected). */
function withChosen(options: readonly FilterOption[], chosen: string | null): readonly FilterOption[] {
  if (chosen === null || options.some((o) => o.value === chosen)) return options;
  return [...options, { value: chosen, label: `${chosen} (not in this list)`, count: 0 }];
}

export function FilterBar({
  filters,
  values,
  onChange,
  onClear,
  summary,
  label = "Filters",
}: {
  filters: readonly FilterSpec[];
  values: FilterValues;
  onChange: (key: string, value: string | null) => void;
  /** Shows "Clear filters" while any filter is active. */
  onClear?: () => void;
  /** E.g. "Showing 40 of 1,204 events" (announced politely when it changes). */
  summary?: ReactNode;
  /** Accessible name of the whole bar. */
  label?: string;
}) {
  const id = useId();
  const active = activeFilterCount(values, filters);
  return (
    <div className="filter-bar" role="group" aria-label={label}>
      {filters.map((filter) => {
        const chosen = isActive(values[filter.key]) ? (values[filter.key] as string) : null;
        const options = withChosen(filter.options, chosen);
        const display = filter.display ?? (options.length <= CHIP_LIMIT ? "chips" : "select");
        const all = filter.allLabel ?? "All";
        if (display === "select") {
          return (
            <label key={filter.key} className="filter-group">
              <span className="field-label">{filter.label}</span>
              <select value={chosen ?? ""} onChange={(event) => onChange(filter.key, event.target.value || null)}>
                <option value="">{all}</option>
                {options.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label ?? option.value}
                    {option.count !== undefined ? ` (${option.count})` : ""}
                  </option>
                ))}
              </select>
            </label>
          );
        }
        const labelId = `${id}-${filter.key}`;
        return (
          <div key={filter.key} className="filter-group" role="group" aria-labelledby={labelId}>
            <span id={labelId} className="field-label">
              {filter.label}
            </span>
            <span className="filter-chips">
              <button type="button" className="filter-chip" aria-pressed={chosen === null} onClick={() => onChange(filter.key, null)}>
                {all}
              </button>
              {options.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  className="filter-chip"
                  aria-pressed={chosen === option.value}
                  onClick={() => onChange(filter.key, chosen === option.value ? null : option.value)}
                >
                  {option.label ?? option.value}
                  {option.count !== undefined ? <span className="filter-count">{option.count}</span> : null}
                </button>
              ))}
            </span>
          </div>
        );
      })}
      {onClear && active > 0 ? (
        <button type="button" className="btn btn-ghost btn-small filter-clear" onClick={onClear}>
          Clear filters ({active})
        </button>
      ) : null}
      {summary ? (
        <span className="filter-summary" role="status">
          {summary}
        </span>
      ) : null}
    </div>
  );
}
