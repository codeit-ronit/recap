import { Fragment, useState } from "react";
import type { ReactNode } from "react";
import { Icon } from "./Icon";

export interface Column<T> {
  key: string;
  header: ReactNode;
  render: (row: T, index: number) => ReactNode;
  width?: number | string;
  /** Numbers: right-aligned, tabular figures. */
  numeric?: boolean;
  /** Keep the cell on one line (ids, digests). */
  nowrap?: boolean;
}

/**
 * A plain data table. With `expand`, every row gets a disclosure button and clicking the
 * row toggles a detail row (raw content, JSON, ...).
 */
export function DataTable<T>({
  columns,
  rows,
  rowKey,
  expand,
  initiallyOpen,
  rowClassName,
  onRowClick,
  empty,
  className,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T, index: number) => string;
  expand?: (row: T) => ReactNode;
  initiallyOpen?: string[];
  rowClassName?: (row: T) => string | undefined;
  onRowClick?: (row: T) => void;
  empty?: ReactNode;
  /** Extra table classes, e.g. "dense wrap-head" for wide numeric tables. */
  className?: string;
}) {
  const [open, setOpen] = useState<Set<string>>(() => new Set(initiallyOpen ?? []));
  const toggle = (key: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  const span = columns.length + (expand ? 1 : 0);

  return (
    <div className="table-wrap">
      <table className={className ? `table ${className}` : "table"}>
        <thead>
          <tr>
            {expand ? <th className="col-expand"><span className="sr-only">Details</span></th> : null}
            {columns.map((c) => (
              <th key={c.key} style={{ width: c.width, textAlign: c.numeric ? "right" : undefined }}>
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const key = rowKey(row, index);
            const isOpen = !!expand && open.has(key);
            const clickable = !!onRowClick || !!expand;
            const classes = [clickable ? "clickable" : "", isOpen ? "open" : "", rowClassName?.(row) ?? ""]
              .filter(Boolean)
              .join(" ");
            return (
              <Fragment key={key}>
                <tr
                  className={classes || undefined}
                  onClick={clickable ? () => (onRowClick ? onRowClick(row) : toggle(key)) : undefined}
                >
                  {expand ? (
                    <td className="col-expand">
                      <button
                        type="button"
                        className="icon-btn"
                        aria-expanded={isOpen}
                        aria-label={isOpen ? "Hide details" : "Show details"}
                        onClick={(e) => {
                          e.stopPropagation();
                          toggle(key);
                        }}
                      >
                        <Icon name={isOpen ? "chevronDown" : "chevronRight"} size={14} />
                      </button>
                    </td>
                  ) : null}
                  {columns.map((c) => (
                    <td key={c.key} className={[c.numeric ? "num" : "", c.nowrap ? "nowrap" : ""].filter(Boolean).join(" ") || undefined}>
                      {c.render(row, index)}
                    </td>
                  ))}
                </tr>
                {isOpen && expand ? (
                  <tr className="row-detail">
                    <td colSpan={span}>{expand(row)}</td>
                  </tr>
                ) : null}
              </Fragment>
            );
          })}
        </tbody>
      </table>
      {rows.length === 0 ? <div className="table-empty">{empty ?? "Nothing to show yet."}</div> : null}
    </div>
  );
}
