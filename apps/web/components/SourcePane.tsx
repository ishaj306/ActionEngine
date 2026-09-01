"use client";

import { useEffect, useMemo, useRef } from "react";
import type { Evidence } from "@/lib/api";
import styles from "./SourcePane.module.css";

/*
 * The document, with the cited span marked.
 *
 * This pane is what makes a citation checkable rather than decorative: the
 * offsets the engine stored index this exact string, so the highlight is the
 * claim's evidence and not an approximation of it. If an offset were ever
 * wrong, it would be obvious here -- which is the point of showing it.
 */

export function SourcePane({
  text,
  active,
  label,
}: {
  text: string;
  active: Evidence | null;
  label: string | null;
}) {
  const markRef = useRef<HTMLElement>(null);

  const segments = useMemo(() => {
    if (!active) return null;
    const start = Math.max(0, Math.min(active.char_start, text.length));
    const end = Math.max(start, Math.min(active.char_end, text.length));
    return {
      before: text.slice(0, start),
      match: text.slice(start, end),
      after: text.slice(end),
    };
  }, [text, active]);

  useEffect(() => {
    if (!markRef.current) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    markRef.current.scrollIntoView({
      behavior: reduced ? "auto" : "smooth",
      block: "center",
    });
  }, [active]);

  return (
    <section className={styles.pane} aria-label="Source document">
      <header className={styles.head}>
        <h2 className={styles.title}>Source</h2>
        {active ? (
          <p className={styles.status}>
            <span className={styles.page}>Page {active.page}</span>
            <span className={styles.dot} aria-hidden="true" />
            <span className={styles.offsets}>
              {active.char_start.toLocaleString()}–{active.char_end.toLocaleString()}
            </span>
            {active.match_score < 1 && (
              <>
                <span className={styles.dot} aria-hidden="true" />
                <span className={styles.approx}>
                  {Math.round(active.match_score * 100)}% match
                </span>
              </>
            )}
          </p>
        ) : (
          <p className={styles.status}>
            <span className={styles.hint}>Select a finding to see its evidence</span>
          </p>
        )}
      </header>

      <div
        className={`${styles.scroll} ${active ? styles.dimmed : ""}`}
        tabIndex={0}
        role="region"
        aria-label={
          label ? `Document text, showing evidence for ${label}` : "Document text"
        }
      >
        <div className={styles.text}>
          {segments ? (
            <>
              {segments.before}
              <mark ref={markRef} className={styles.mark}>
                {segments.match}
              </mark>
              {segments.after}
            </>
          ) : (
            text
          )}
        </div>
      </div>
    </section>
  );
}
