"use client";

import { useCallback, useState } from "react";
import type { Analysis, EnquiryDraft } from "@/lib/api";
import { downloadCalendar, fetchEnquiry } from "@/lib/api";
import styles from "./ExportBar.module.css";

/*
 * Taking the plan somewhere else.
 *
 * The calendar file is honest about what it can carry: only steps with a
 * governing date become events, and the count says so rather than letting the
 * reader assume the whole plan is in there.
 *
 * Both exports are files and drafts rather than live integrations. Writing to
 * somebody's real calendar on the strength of an extraction is a bigger claim
 * than this engine is willing to make.
 */

export function ExportBar({ analysis }: { analysis: Analysis }) {
  const [draft, setDraft] = useState<EnquiryDraft | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "error">("idle");
  const [copied, setCopied] = useState(false);

  const dated = analysis.actions.filter((action) => action.latest_start).length;
  const total = analysis.actions.length;

  const openDraft = useCallback(async () => {
    if (draft) {
      setDraft(null);
      return;
    }
    setState("loading");
    try {
      setDraft(await fetchEnquiry(analysis.document_id));
      setState("idle");
    } catch {
      setState("error");
    }
  }, [analysis.document_id, draft]);

  const copy = useCallback(async () => {
    if (!draft) return;
    try {
      await navigator.clipboard.writeText(`${draft.subject}\n\n${draft.body}`);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2400);
    } catch {
      setCopied(false);
    }
  }, [draft]);

  return (
    <div className={styles.bar}>
      <div className={styles.row}>
        {dated > 0 ? (
          <button
            type="button"
            className={styles.action}
            onClick={() => {
              void downloadCalendar(analysis.document_id, analysis.filename);
            }}
          >
            Add {dated} dated step{dated === 1 ? "" : "s"} to a calendar
          </button>
        ) : (
          <span className={styles.disabled}>
            No step has a date to put in a calendar
          </span>
        )}

        <button
          type="button"
          className={styles.action}
          onClick={openDraft}
          aria-expanded={draft !== null}
        >
          {draft ? "Hide the draft email" : "Draft the email that asks"}
        </button>
      </div>

      {dated < total && dated > 0 && (
        <p className={styles.caveat}>
          {total - dated} step{total - dated === 1 ? "" : "s"} had no date to
          schedule and {total - dated === 1 ? "is" : "are"} left out rather than
          given a guessed one.
        </p>
      )}

      {state === "error" && (
        <p className={styles.caveat} role="alert">
          The draft could not be fetched. The analysis service may have stopped.
        </p>
      )}

      {draft && (
        <div className={styles.draft}>
          <p className={styles.draftMeta}>
            {draft.question_count === 0
              ? "Nothing in this document is left open, so the draft asks nothing."
              : `${draft.question_count} open question${
                  draft.question_count === 1 ? "" : "s"
                }, each quoting the phrase that raised it.`}
          </p>

          <label htmlFor="enquiry-subject" className={styles.fieldLabel}>
            Subject
          </label>
          <input
            id="enquiry-subject"
            className={styles.subject}
            value={draft.subject}
            readOnly
          />

          <label htmlFor="enquiry-body" className={styles.fieldLabel}>
            Message
          </label>
          <textarea
            id="enquiry-body"
            className={styles.body}
            value={draft.body}
            readOnly
            rows={12}
          />

          <div className={styles.draftFoot}>
            <button type="button" className={styles.copy} onClick={copy}>
              {copied ? "Copied" : "Copy"}
            </button>
            <a
              className={styles.mail}
              href={`mailto:?subject=${encodeURIComponent(
                draft.subject,
              )}&body=${encodeURIComponent(draft.body)}`}
            >
              Open in your mail app
            </a>
          </div>
          <p className={styles.live} aria-live="polite">
            {copied ? "Draft copied to the clipboard." : ""}
          </p>
        </div>
      )}
    </div>
  );
}
