"use client";

import type { Comparison, Portfolio, Severity } from "@/lib/api";
import { describeSlack, formatDate } from "@/lib/api";
import styles from "./CrossDocument.module.css";

/*
 * Reading more than one document.
 *
 * Two surfaces, both showing the same kind of thing: a claim that only exists
 * because two readings were held side by side. A revision banner says what
 * moved; a portfolio says where two documents disagree.
 *
 * Neither is allowed to be quietly wrong. When the two documents look
 * unrelated, that is said before anything else on the screen, because fifty
 * spurious changes presented as a revision history is worse than no comparison
 * at all.
 */

const SEVERITY_LABEL: Record<Severity, string> = {
  critical: "Costs you time",
  notable: "Changes the plan",
  minor: "Worth knowing",
};

export function ChangesBanner({
  comparison,
  previousName,
  onDismiss,
}: {
  comparison: Comparison;
  previousName: string;
  onDismiss: () => void;
}) {
  // The server ranks causes above their consequences and the headline is the
  // first of them, so listing it again below is the same sentence twice.
  const [lead, ...rest] = comparison.changes;
  const critical = comparison.changes.filter((c) => c.severity === "critical");

  return (
    <section
      className={styles.banner}
      data-tone={critical.length > 0 ? "critical" : "calm"}
      aria-labelledby="changes-heading"
    >
      <div className={styles.bannerHead}>
        <h2 id="changes-heading" className={styles.bannerTitle}>
          Compared with {previousName}
        </h2>
        <button type="button" className={styles.dismiss} onClick={onDismiss}>
          Dismiss
        </button>
      </div>

      {comparison.warning && (
        <p className={styles.warning} role="alert">
          {comparison.warning}
        </p>
      )}

      <p className={styles.headline}>{comparison.headline}</p>

      {lead?.before && lead.after && (
        <p className={styles.delta}>
          <span className={styles.was}>{lead.before}</span>
          <span aria-hidden="true">→</span>
          <span className={styles.now}>{lead.after}</span>
        </p>
      )}

      {rest.length > 0 && (
        <ul className={styles.changes}>
          {rest.map((change, index) => (
            <li key={`${change.kind}-${index}`} className={styles.change}>
              <span className={styles.severity} data-severity={change.severity}>
                {SEVERITY_LABEL[change.severity]}
              </span>
              <span className={styles.changeText}>{change.summary}</span>
              {change.before && change.after && (
                <span className={styles.delta}>
                  <span className={styles.was}>{change.before}</span>
                  <span aria-hidden="true">→</span>
                  <span className={styles.now}>{change.after}</span>
                </span>
              )}
            </li>
          ))}
        </ul>
      )}

      <p className={styles.footnote}>
        Findings were compared, not text. A reissued notice is retyped
        throughout, so a line-by-line diff would bury these under formatting.
      </p>
    </section>
  );
}

export function PortfolioPanel({
  portfolio,
  onClose,
}: {
  portfolio: Portfolio;
  onClose: () => void;
}) {
  return (
    <section className={styles.portfolio} aria-labelledby="portfolio-heading">
      <div className={styles.bannerHead}>
        <h2 id="portfolio-heading" className={styles.bannerTitle}>
          {portfolio.timeline.length + portfolio.undated.length} steps across your
          documents
        </h2>
        <button type="button" className={styles.dismiss} onClick={onClose}>
          Close
        </button>
      </div>

      {portfolio.conflicts.length > 0 ? (
        <ul className={styles.conflicts}>
          {portfolio.conflicts.map((conflict, index) => (
            <li key={index} className={styles.conflict}>
              <span className={styles.conflictTag}>They disagree</span>
              <p className={styles.conflictSummary}>{conflict.summary}</p>
              <p className={styles.conflictResolution}>{conflict.resolution}</p>
            </li>
          ))}
        </ul>
      ) : (
        <p className={styles.agree}>
          Nothing these documents both state disagrees. Where only one of them
          says something, that is a difference between documents rather than a
          contradiction, and is not reported as one.
        </p>
      )}

      {portfolio.next_due && (
        <p className={styles.nextDue}>
          <span className={styles.nextLabel}>Soonest thing to finish</span>
          <span className={styles.nextDate}>{formatDate(portfolio.next_due)}</span>
          {portfolio.next_stated_deadline &&
            portfolio.next_stated_deadline !== portfolio.next_due && (
              <span className={styles.nextNote}>
                Earlier than the soonest date any of them states (
                {formatDate(portfolio.next_stated_deadline)}), because a
                prerequisite inherits the deadline of what depends on it.
              </span>
            )}
        </p>
      )}

      <ol className={styles.timeline}>
        {portfolio.timeline.map((item) => (
          <li key={`${item.document_id}-${item.action.id}`} className={styles.step}>
            <span className={styles.stepDate}>
              {formatDate(item.action.latest_start)}
            </span>
            <span className={styles.stepBody}>
              <span className={styles.stepText}>{item.action.description}</span>
              <span className={styles.stepMeta}>
                <span className={styles.source}>{item.document_name}</span>
                {describeSlack(item.action.slack_days) && (
                  <span className={styles.stepSlack}>
                    {describeSlack(item.action.slack_days)}
                  </span>
                )}
              </span>
            </span>
          </li>
        ))}
      </ol>

      {portfolio.undated.length > 0 && (
        <div className={styles.undated}>
          <h3 className={styles.undatedTitle}>
            No date governs {portfolio.undated.length === 1 ? "this step" : "these steps"}
          </h3>
          <ul className={styles.undatedList}>
            {portfolio.undated.map((item) => (
              <li key={`${item.document_id}-${item.action.id}`}>
                {item.action.description}
                <span className={styles.source}>{item.document_name}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <p className={styles.footnote}>
        Steps keep the document they came from. Whether one document&rsquo;s step
        blocks another&rsquo;s is a question about the world rather than about the
        text, so no dependency is inferred across documents.
      </p>
    </section>
  );
}
