"use client";

import type { Action, Analysis, Gap, Priority } from "@/lib/api";
import { describeSlack, formatDate } from "@/lib/api";
import { ClaimTag } from "./ClaimTag";
import styles from "./PlanPane.module.css";

/*
 * The plan.
 *
 * The row's headline is not the deadline but the date work has to *start*,
 * because that is the number a reader can act on and the one no other tool
 * computes. A certificate due the 17th that takes a week is a job for the
 * 10th, and saying so is the whole point of the scheduler behind this view.
 */

const VERB_LABEL: Record<Action["verb"], string> = {
  obtain: "Obtain",
  prepare: "Prepare",
  submit: "Submit",
  attend: "Attend",
  confirm: "Check",
};

const PRIORITY_LABEL: Record<Priority, string> = {
  overdue: "Behind schedule",
  critical: "Start now",
  high: "Start soon",
  medium: "Scheduled",
  low: "No deadline",
};

export interface Selection {
  kind: "action" | "gap" | "deadline" | "title";
  id: string;
}

export function PlanPane({
  analysis,
  selection,
  onSelect,
  completed,
  onToggleComplete,
}: {
  analysis: Analysis;
  selection: Selection | null;
  onSelect: (selection: Selection) => void;
  completed: ReadonlySet<string>;
  onToggleComplete: (id: string) => void;
}) {
  const remaining = analysis.actions.filter((a) => !completed.has(a.id)).length;

  return (
    <div className={styles.pane}>
      <Summary analysis={analysis} selection={selection} onSelect={onSelect} />

      <section className={styles.section} aria-labelledby="plan-heading">
        <div className={styles.sectionHead}>
          <h2 id="plan-heading" className={styles.sectionTitle}>
            Action plan
          </h2>
          <p className={styles.count} aria-live="polite">
            {analysis.actions.length === 0
              ? "None found"
              : `${remaining} of ${analysis.actions.length} remaining`}
          </p>
        </div>

        {analysis.actions.length === 0 ? (
          <p className={styles.empty}>
            No instructions were found in this document. It may be informational
            rather than something you need to act on.
          </p>
        ) : (
          <ol className={styles.list}>
            {analysis.actions.map((action) => (
              <ActionRow
                key={action.id}
                action={action}
                actions={analysis.actions}
                selected={
                  selection?.kind === "action" && selection.id === action.id
                }
                onSelect={() => onSelect({ kind: "action", id: action.id })}
                done={completed.has(action.id)}
                onToggle={() => onToggleComplete(action.id)}
              />
            ))}
          </ol>
        )}
      </section>

      {analysis.gaps.length > 0 && (
        <GapSection gaps={analysis.gaps} selection={selection} onSelect={onSelect} />
      )}
    </div>
  );
}

function Summary({
  analysis,
  selection,
  onSelect,
}: {
  analysis: Analysis;
  selection: Selection | null;
  onSelect: (selection: Selection) => void;
}) {
  const deadline = analysis.primary_deadline;

  return (
    <section className={styles.summary} aria-label="Document summary">
      {analysis.title && (
        <button
          type="button"
          className={`${styles.titleButton} ${
            selection?.kind === "title" ? styles.titleSelected : ""
          }`}
          onClick={() => onSelect({ kind: "title", id: "title" })}
        >
          <h1 className={styles.docTitle}>{analysis.title.value}</h1>
          <ClaimTag
            classification={analysis.title.classification}
            confidence={analysis.title.confidence}
            size="small"
          />
        </button>
      )}

      <div className={styles.facts}>
        {deadline ? (
          <button
            type="button"
            className={`${styles.deadline} ${
              selection?.kind === "deadline" ? styles.deadlineSelected : ""
            }`}
            onClick={() => onSelect({ kind: "deadline", id: "primary" })}
          >
            <span className={styles.factLabel}>Deadline</span>
            <span className={styles.deadlineDate}>{formatDate(deadline.value)}</span>
            <ClaimTag
              classification={deadline.classification}
              confidence={deadline.confidence}
              size="small"
            />
          </button>
        ) : (
          <div className={styles.deadline}>
            <span className={styles.factLabel}>Deadline</span>
            <span className={styles.noDeadline}>None stated</span>
          </div>
        )}
      </div>

      {!analysis.is_feasible && (
        <p className={styles.infeasible} role="status">
          <strong>This plan cannot be completed in time.</strong> At least one
          step needs longer than the deadline allows. The steps marked{" "}
          <em>Behind schedule</em> below are the ones that no longer fit.
        </p>
      )}

      {analysis.needs_ocr && (
        <p className={styles.notice} role="status">
          <strong>This document has no readable text layer.</strong> It is
          probably a scan. Nothing below could be extracted from it.
        </p>
      )}

      {analysis.broken_cycles.length > 0 && (
        <p className={styles.notice} role="status">
          <strong>The extracted steps contradicted each other on ordering.</strong>{" "}
          {analysis.broken_cycles.length} link
          {analysis.broken_cycles.length === 1 ? " was" : "s were"} dropped to
          produce a workable sequence. Check the order below against the document.
        </p>
      )}
    </section>
  );
}

function ActionRow({
  action,
  actions,
  selected,
  onSelect,
  done,
  onToggle,
}: {
  action: Action;
  actions: Action[];
  selected: boolean;
  onSelect: () => void;
  done: boolean;
  onToggle: () => void;
}) {
  const slack = describeSlack(action.slack_days);
  const inherited =
    action.effective_deadline !== null &&
    action.effective_deadline !== action.deadline;
  const blockers = action.blocked_by
    .map((id) => actions.find((candidate) => candidate.id === id)?.description)
    .filter((value): value is string => Boolean(value));

  return (
    <li className={`${styles.row} ${done ? styles.done : ""}`} data-priority={action.priority}>
      <span className={styles.stripe} aria-hidden="true" />

      <div className={styles.check}>
        <input
          type="checkbox"
          id={`done-${action.id}`}
          className={styles.checkbox}
          checked={done}
          onChange={onToggle}
        />
        <label htmlFor={`done-${action.id}`} className={styles.checkLabel}>
          <span className="visually-hidden">
            Mark “{action.description}” as done
          </span>
        </label>
      </div>

      <div className={styles.body}>
        <button type="button" className={styles.rowButton} onClick={onSelect} aria-pressed={selected}>
          <span className={styles.rowHead}>
            <span className={styles.verb}>{VERB_LABEL[action.verb]}</span>
            <span className={styles.priority}>{PRIORITY_LABEL[action.priority]}</span>
            <ClaimTag
              classification={action.claim.classification}
              confidence={action.claim.confidence}
              size="small"
            />
          </span>

          <span className={styles.description}>{action.description}</span>

          {action.latest_start && (
            <span className={styles.schedule}>
              <span className={styles.startBy}>
                Start by {formatDate(action.latest_start)}
              </span>
              {slack && <span className={styles.slack}>{slack}</span>}
              {inherited && (
                <span className={styles.inherited}>
                  Due {formatDate(action.effective_deadline)} — inherited from a
                  later step, not stated for this one
                </span>
              )}
            </span>
          )}

          <span className={styles.evidenceCue}>
            {selected ? "Showing evidence" : "Show evidence"}
          </span>
        </button>

        {action.requires.length > 0 && (
          <p className={styles.requires}>
            <span className={styles.metaLabel}>Needs</span>
            {action.requires.join(" · ")}
          </p>
        )}

        {blockers.length > 0 && (
          <p className={styles.blocked}>
            <span className={styles.metaLabel}>Waiting on</span>
            {blockers.join(" · ")}
          </p>
        )}
      </div>
    </li>
  );
}

function GapSection({
  gaps,
  selection,
  onSelect,
}: {
  gaps: Gap[];
  selection: Selection | null;
  onSelect: (selection: Selection) => void;
}) {
  return (
    <section className={styles.section} aria-labelledby="gaps-heading">
      <div className={styles.sectionHead}>
        <h2 id="gaps-heading" className={styles.sectionTitle}>
          The document does not say
        </h2>
        <p className={styles.count}>{gaps.length}</p>
      </div>

      <ul className={styles.gapList}>
        {gaps.map((gap, index) => {
          const id = `gap-${index}`;
          const isSelected = selection?.kind === "gap" && selection.id === id;
          return (
            <li key={id} className={styles.gapItem}>
              <button
                type="button"
                className={`${styles.gapButton} ${isSelected ? styles.gapSelected : ""}`}
                onClick={() => onSelect({ kind: "gap", id })}
                aria-pressed={isSelected}
              >
                <span className={styles.gapHead}>
                  <ClaimTag classification="MISSING" size="small" />
                  <span className={styles.gapQuestion}>{gap.question}</span>
                </span>
                <span className={styles.gapWhy}>{gap.why_it_matters}</span>
                {gap.suggested_resolution && (
                  <span className={styles.gapNext}>
                    <span className={styles.metaLabel}>Next step</span>
                    {gap.suggested_resolution}
                  </span>
                )}
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
