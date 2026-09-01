"use client";

import { useMemo, useState } from "react";
import type {
  Analysis,
  Attribute,
  Condition,
  MatchResult,
  Profile,
  RelevanceVerdict,
} from "@/lib/api";
import { ClaimTag } from "./ClaimTag";
import styles from "./EligibilityPane.module.css";

/*
 * Eligibility.
 *
 * The form asks only for the attributes this document actually restricts. A
 * seven-field profile in front of a notice that mentions one condition is a
 * tax on the reader for the convenience of the schema, and most people would
 * abandon it — so the fields are derived from the conditions found in the text.
 *
 * The verdict never hides the plan. The extraction underneath is lexical and
 * can miss; a plan withheld on a false negative is a worse failure than a plan
 * shown under a warning.
 */

const FIELDS: Record<
  Attribute,
  { label: string; hint: string; type: "number" | "text" | "select"; options?: string[]; step?: string }
> = {
  year: { label: "Year of study", hint: "1–10", type: "number" },
  programme: { label: "Programme or department", hint: "e.g. Computer Science", type: "text" },
  category: {
    label: "Category",
    hint: "As stated on your certificate",
    type: "select",
    options: ["General", "OBC", "SC", "ST", "EWS", "PwD"],
  },
  domicile: { label: "Domicile", hint: "State or region", type: "text" },
  score: { label: "Aggregate", hint: "Percentage", type: "number", step: "0.1" },
  cgpa: { label: "CGPA", hint: "Out of 10", type: "number", step: "0.01" },
  age: { label: "Age", hint: "In years", type: "number" },
};

const VERDICT: Record<RelevanceVerdict, { label: string; tone: string }> = {
  applies: { label: "You meet the stated conditions", tone: "yes" },
  does_not_apply: { label: "You do not meet a stated condition", tone: "no" },
  undetermined: { label: "Not enough about you to tell", tone: "open" },
  not_restricted: { label: "The document never says who it is for", tone: "absent" },
};

const MATCH_LABEL: Record<MatchResult, string> = {
  matches: "Met",
  conflicts: "Not met",
  unknown: "Open",
};

export function EligibilityPane({
  analysis,
  busy,
  onSubmit,
  onClear,
  onSelectCondition,
  selectedRequirement,
}: {
  analysis: Analysis;
  busy: boolean;
  onSubmit: (profile: Profile) => void;
  onClear: () => void;
  onSelectCondition: (requirement: string) => void;
  selectedRequirement: string | null;
}) {
  const { conditions, relevance, relevance_verdict: verdict } = analysis;
  const asked = useMemo(() => askedFields(conditions), [conditions]);
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>({});

  if (conditions.length === 0) {
    return (
      <section className={styles.pane} aria-labelledby="eligibility-heading">
        <h2 id="eligibility-heading" className={styles.title}>
          Who this is for
        </h2>
        <p className={styles.unstated}>
          <ClaimTag classification="MISSING" size="small" />
          <span>
            This document states no eligibility condition anywhere. It may well
            apply to you — but an unstated restriction is the one thing that
            cannot be checked here.
          </span>
        </p>
      </section>
    );
  }

  const answered = verdict !== null;

  return (
    <section className={styles.pane} aria-labelledby="eligibility-heading">
      <div className={styles.head}>
        <h2 id="eligibility-heading" className={styles.title}>
          Who this is for
        </h2>
        {answered && verdict && (
          <span className={styles.verdict} data-tone={VERDICT[verdict].tone}>
            {VERDICT[verdict].label}
          </span>
        )}
      </div>

      {answered && relevance && (
        <p className={styles.rationale}>
          <ClaimTag
            classification={relevance.classification}
            confidence={relevance.confidence}
            size="small"
          />
          <span>{relevance.rationale}</span>
        </p>
      )}

      <ul className={styles.conditions}>
        {conditions.map((condition) => (
          <li key={`${condition.attribute}-${condition.requirement}`}>
            <button
              type="button"
              className={`${styles.condition} ${
                selectedRequirement === condition.requirement ? styles.conditionOn : ""
              }`}
              onClick={() => onSelectCondition(condition.requirement)}
              aria-pressed={selectedRequirement === condition.requirement}
            >
              <span className={styles.conditionHead}>
                <span className={styles.requirement}>{condition.requirement}</span>
                {condition.match && (
                  <span className={styles.match} data-match={condition.match}>
                    <span className={styles.matchGlyph} aria-hidden="true">
                      {condition.match === "matches"
                        ? "✓"
                        : condition.match === "conflicts"
                          ? "✕"
                          : "?"}
                    </span>
                    {MATCH_LABEL[condition.match]}
                  </span>
                )}
              </span>
              {condition.explanation && (
                <span className={styles.explanation}>{condition.explanation}</span>
              )}
            </button>
          </li>
        ))}
      </ul>

      {!open && !answered && (
        <button type="button" className={styles.open} onClick={() => setOpen(true)}>
          Check whether this applies to you
        </button>
      )}

      {(open || answered) && (
        <form
          className={styles.form}
          onSubmit={(event) => {
            event.preventDefault();
            onSubmit(toProfile(draft));
          }}
        >
          <p className={styles.formNote}>
            Only what this document actually asks about. Nothing is stored beyond
            this session, and nothing is sent anywhere else.
          </p>

          <div className={styles.fields}>
            {asked.map((attribute) => {
              const field = FIELDS[attribute];
              const id = `profile-${attribute}`;
              return (
                <div key={attribute} className={styles.field}>
                  <label htmlFor={id} className={styles.label}>
                    {field.label}
                    <span className={styles.hint}>{field.hint}</span>
                  </label>
                  {field.type === "select" ? (
                    <select
                      id={id}
                      className={styles.input}
                      value={draft[attribute] ?? ""}
                      onChange={(event) =>
                        setDraft((d) => ({ ...d, [attribute]: event.target.value }))
                      }
                    >
                      <option value="">Prefer not to say</option>
                      {field.options?.map((option) => (
                        <option key={option} value={option}>
                          {option}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input
                      id={id}
                      className={styles.input}
                      type={field.type}
                      step={field.step}
                      inputMode={field.type === "number" ? "decimal" : undefined}
                      value={draft[attribute] ?? ""}
                      onChange={(event) =>
                        setDraft((d) => ({ ...d, [attribute]: event.target.value }))
                      }
                    />
                  )}
                </div>
              );
            })}
          </div>

          <div className={styles.formFoot}>
            <button type="submit" className={styles.check} disabled={busy}>
              {answered ? "Check again" : "Check"}
            </button>
            {answered && (
              <button
                type="button"
                className={styles.forget}
                onClick={() => {
                  setDraft({});
                  setOpen(false);
                  onClear();
                }}
                disabled={busy}
              >
                Forget what I entered
              </button>
            )}
          </div>
        </form>
      )}
    </section>
  );
}

/** The attributes this document restricts, in the order it raises them. */
function askedFields(conditions: Condition[]): Attribute[] {
  const seen: Attribute[] = [];
  for (const condition of conditions) {
    if (!seen.includes(condition.attribute)) seen.push(condition.attribute);
  }
  return seen;
}

/**
 * An empty field means "prefer not to say", not zero. Sending 0 for a blank
 * age would fail every age condition ever written.
 */
function toProfile(draft: Record<string, string>): Profile {
  const profile: Profile = {};
  for (const [key, raw] of Object.entries(draft)) {
    const value = raw.trim();
    if (!value) continue;
    const attribute = key as Attribute;
    if (FIELDS[attribute].type === "number") {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) {
        (profile as Record<string, unknown>)[attribute] = parsed;
      }
    } else {
      (profile as Record<string, unknown>)[attribute] = value;
    }
  }
  return profile;
}
