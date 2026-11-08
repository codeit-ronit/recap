import type * as dto from "../dto";
import { hrefFor } from "../hooks";
import { HISTORICAL_LABEL, historicalWords } from "../view/published";
import { Icon } from "./Icon";

/**
 * The banner a published run carries on every page that shows it (the shell puts it above a
 * run page whose run GET /runs/{id} reports as "published"): what it is, where and when it ran,
 * and that nothing runs now. The facts are the run's own summary; the label is the same
 * everywhere (src/view/published.ts HISTORICAL_LABEL).
 */
export function HistoricalBanner({ summary }: { summary: dto.RunSummary | null | undefined }) {
  const provenance = summary?.published ?? null;
  return (
    <div className="historical-banner" role="note" aria-label="A published historical run">
      <Icon name="package" size={20} />
      <div className="historical-body">
        <div className="historical-label">{HISTORICAL_LABEL}</div>
        <p className="historical-text">
          {summary ? historicalWords(summary) : "A run published with the repository: historical evidence. Nothing runs now."}
        </p>
        {provenance ? (
          <p className="historical-meta">
            <span>{provenance.title}</span>
            <span>
              files in <code className="mono">{provenance.folder}</code>, SHA-256 verified
            </span>
            <a href={hrefFor("published")}>All published runs</a>
          </p>
        ) : null}
      </div>
    </div>
  );
}
