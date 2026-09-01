import type { ClaimClass } from "@/lib/api";
import styles from "./ClaimTag.module.css";

/*
 * The most repeated element in the product, so it carries the most care.
 *
 * Three redundant channels encode the same thing: a hue, a word, and a glyph
 * whose fill level tracks certainty (solid, half, hollow, struck through).
 * Any one of them alone is enough to read the state, which is what makes it
 * survive greyscale, colour blindness, and a screen reader.
 *
 * The labels are English, not the internal enum. A reader recognises "Stated"
 * and "Needs checking"; FACT and UNCERTAIN are our vocabulary, not theirs.
 */

interface Meaning {
  label: string;
  description: string;
}

const MEANING: Record<ClaimClass, Meaning> = {
  FACT: {
    label: "Stated",
    description: "Written in the document. Quoted below.",
  },
  INFERENCE: {
    label: "Inferred",
    description: "Worked out from what the document says, not stated outright.",
  },
  UNCERTAIN: {
    label: "Needs checking",
    description: "Ambiguous or incomplete. Confirm before you rely on it.",
  },
  MISSING: {
    label: "Not stated",
    description: "The document does not answer this.",
  },
};

/** Certainty glyph: a disc that empties as confidence falls. */
function Glyph({ kind }: { kind: ClaimClass }) {
  const common = { cx: 6, cy: 6, r: 4.25 };
  return (
    <svg
      className={styles.glyph}
      viewBox="0 0 12 12"
      width="12"
      height="12"
      aria-hidden="true"
      focusable="false"
    >
      <circle {...common} fill="none" stroke="currentColor" strokeWidth="1.4" />
      {kind === "FACT" && <circle {...common} r="4.25" fill="currentColor" />}
      {kind === "INFERENCE" && (
        <path d="M6 1.75a4.25 4.25 0 0 1 0 8.5Z" fill="currentColor" />
      )}
      {kind === "MISSING" && (
        <line
          x1="2.9"
          y1="9.1"
          x2="9.1"
          y2="2.9"
          stroke="currentColor"
          strokeWidth="1.4"
          strokeLinecap="round"
        />
      )}
    </svg>
  );
}

export function ClaimTag({
  classification,
  confidence,
  size = "default",
}: {
  classification: ClaimClass;
  confidence?: number;
  size?: "default" | "small";
}) {
  const meaning = MEANING[classification];
  const percent = confidence === undefined ? null : Math.round(confidence * 100);

  return (
    <span
      className={`${styles.tag} ${styles[classification]} ${
        size === "small" ? styles.small : ""
      }`}
      title={meaning.description}
    >
      <Glyph kind={classification} />
      <span className={styles.label}>{meaning.label}</span>
      {percent !== null && (
        <>
          <span className={styles.divider} aria-hidden="true" />
          <span className={styles.percent}>{percent}%</span>
          <span className="visually-hidden">confidence</span>
        </>
      )}
    </span>
  );
}

export function claimDescription(classification: ClaimClass): string {
  return MEANING[classification].description;
}

export function claimLabel(classification: ClaimClass): string {
  return MEANING[classification].label;
}
