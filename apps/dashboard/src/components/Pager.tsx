import { fmtInt } from "../format";
import { Icon } from "./Icon";

/**
 * Offset/limit pagination for long lists (trace events, evidence, episodes), matching the
 * API's `offset` + `limit` parameters. With a known `total` the pager can jump to the last
 * page; without one (a server that only says whether more exist) it pages forward while
 * `hasMore` is true.
 */

export interface PageWindow {
  /** The offset actually shown: clamped to [0, last page]. Slice or request with this one. */
  offset: number;
  limit: number;
  total: number | null;
  /** 1-based position of the first and last item shown (both 0 when nothing is shown). */
  from: number;
  to: number;
  /** 1-based page number, and the page count when the total is known. */
  page: number;
  pages: number | null;
  hasPrev: boolean;
  hasNext: boolean;
  prevOffset: number;
  nextOffset: number;
  /** Offset of the last page (null when the total is unknown). */
  lastOffset: number | null;
}

/**
 * The window `offset`/`limit` shows of `total` items. `shown`/`hasMore` describe a page from a
 * server that reports no total (items returned, and whether more exist).
 */
export function pageWindow(
  offset: number,
  limit: number,
  total: number | null | undefined,
  page: { shown?: number; hasMore?: boolean } = {},
): PageWindow {
  const size = Math.max(1, Math.floor(Number.isFinite(limit) ? limit : 1));
  const known = typeof total === "number" && Number.isFinite(total) ? Math.max(0, Math.floor(total)) : null;
  const lastOffset = known === null ? null : known === 0 ? 0 : Math.floor((known - 1) / size) * size;
  let start = Math.max(0, Math.floor(Number.isFinite(offset) ? offset : 0));
  if (lastOffset !== null) start = Math.min(start, lastOffset);
  const shown = known !== null ? Math.max(0, Math.min(size, known - start)) : Math.max(0, Math.min(size, page.shown ?? size));
  return {
    offset: start,
    limit: size,
    total: known,
    from: shown > 0 ? start + 1 : 0,
    to: start + shown,
    page: Math.floor(start / size) + 1,
    pages: known === null ? null : Math.max(1, Math.ceil(known / size)),
    hasPrev: start > 0,
    hasNext: known !== null ? start + shown < known : !!page.hasMore,
    prevOffset: Math.max(0, start - size),
    nextOffset: start + size,
    lastOffset,
  };
}

/** One page of a list held in memory. */
export function paginate<T>(items: readonly T[], offset: number, limit: number): T[] {
  const w = pageWindow(offset, limit, items.length);
  return items.slice(w.offset, w.offset + w.limit);
}

export function Pager({
  offset,
  limit,
  total,
  shown,
  hasMore,
  onChange,
  pageSizes,
  onPageSizeChange,
  noun = "items",
  label = "Pagination",
}: {
  offset: number;
  limit: number;
  /** All items; null/undefined when the server does not report a total. */
  total?: number | null;
  /** Without a total: how many items this page has. */
  shown?: number;
  /** Without a total: whether the server has more after this page. */
  hasMore?: boolean;
  /** Called with the new offset. */
  onChange: (offset: number) => void;
  /** E.g. [50, 100, 200]: offers a page-size select. */
  pageSizes?: readonly number[];
  onPageSizeChange?: (limit: number) => void;
  /** What is counted, e.g. "events". */
  noun?: string;
  label?: string;
}) {
  const w = pageWindow(offset, limit, total, { shown, hasMore });
  const summary =
    w.from === 0
      ? `No ${noun}`
      : w.total !== null
        ? `${fmtInt(w.from)}–${fmtInt(w.to)} of ${fmtInt(w.total)} ${noun}`
        : `${fmtInt(w.from)}–${fmtInt(w.to)} ${noun}`;
  return (
    <nav className="pager" aria-label={label}>
      <span className="pager-summary" role="status">
        {summary}
      </span>
      <span className="pager-controls">
        <button type="button" className="btn btn-small pager-btn" aria-label="First page" disabled={!w.hasPrev} onClick={() => onChange(0)}>
          <Icon name="chevronsLeft" size={14} />
        </button>
        <button type="button" className="btn btn-small pager-btn" aria-label="Previous page" disabled={!w.hasPrev} onClick={() => onChange(w.prevOffset)}>
          <Icon name="chevronLeft" size={14} />
          <span className="pager-text">Previous</span>
        </button>
        <span className="pager-page">
          Page {fmtInt(w.page)}
          {w.pages !== null ? ` of ${fmtInt(w.pages)}` : ""}
        </span>
        <button type="button" className="btn btn-small pager-btn" aria-label="Next page" disabled={!w.hasNext} onClick={() => onChange(w.nextOffset)}>
          <span className="pager-text">Next</span>
          <Icon name="chevronRight" size={14} />
        </button>
        {w.lastOffset !== null ? (
          <button
            type="button"
            className="btn btn-small pager-btn"
            aria-label="Last page"
            disabled={!w.hasNext}
            onClick={() => onChange(w.lastOffset ?? 0)}
          >
            <Icon name="chevronsRight" size={14} />
          </button>
        ) : null}
      </span>
      {pageSizes?.length && onPageSizeChange ? (
        <label className="pager-size">
          <span className="field-label">Per page</span>
          <select value={w.limit} onChange={(event) => onPageSizeChange(Number(event.target.value))}>
            {(pageSizes.includes(w.limit) ? pageSizes : [...pageSizes, w.limit].sort((a, b) => a - b)).map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
      ) : null}
    </nav>
  );
}
