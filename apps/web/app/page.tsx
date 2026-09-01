"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import {
  ApiError,
  analyseFile,
  analyseText,
  type Analysis,
  type Evidence,
} from "@/lib/api";
import { SAMPLES } from "@/lib/samples";
import { PlanPane, type Selection } from "@/components/PlanPane";
import { SourcePane } from "@/components/SourcePane";
import styles from "./page.module.css";

type Status = "idle" | "working" | "ready" | "error";

interface Failure {
  message: string;
  remedy: string | undefined;
}

export default function Page() {
  const [status, setStatus] = useState<Status>("idle");
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [completed, setCompleted] = useState<ReadonlySet<string>>(new Set());
  const inFlight = useRef<AbortController | null>(null);

  const run = useCallback(async (work: (signal: AbortSignal) => Promise<Analysis>) => {
    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;

    setStatus("working");
    setFailure(null);
    try {
      const result = await work(controller.signal);
      if (controller.signal.aborted) return;
      setAnalysis(result);
      setSelection(null);
      setCompleted(new Set());
      setStatus("ready");
    } catch (cause) {
      if (controller.signal.aborted) return;
      const error =
        cause instanceof ApiError
          ? cause
          : new ApiError("Something went wrong while analysing the document.");
      setFailure({ message: error.message, remedy: error.remedy });
      setStatus("error");
    }
  }, []);

  const toggleComplete = useCallback((id: string) => {
    setCompleted((current) => {
      const next = new Set(current);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }, []);

  const activeEvidence = useMemo<Evidence | null>(() => {
    if (!analysis || !selection) return null;
    switch (selection.kind) {
      case "action":
        return (
          analysis.actions.find((a) => a.id === selection.id)?.claim.evidence ?? null
        );
      case "gap": {
        const index = Number.parseInt(selection.id.replace("gap-", ""), 10);
        return analysis.gaps[index]?.evidence ?? null;
      }
      case "deadline":
        return analysis.primary_deadline?.evidence ?? null;
      case "title":
        return analysis.title?.evidence ?? null;
      case "type":
        return analysis.document_type.evidence ?? null;
      default:
        return null;
    }
  }, [analysis, selection]);

  const activeLabel = useMemo(() => {
    if (!analysis || !selection) return null;
    if (selection.kind === "action") {
      return analysis.actions.find((a) => a.id === selection.id)?.description ?? null;
    }
    if (selection.kind === "gap") {
      const index = Number.parseInt(selection.id.replace("gap-", ""), 10);
      return analysis.gaps[index]?.question ?? null;
    }
    if (selection.kind === "deadline") return "the deadline";
    if (selection.kind === "type") return "the document type";
    return "the title";
  }, [analysis, selection]);

  return (
    <div className={styles.shell}>
      <Header
        analysis={analysis}
        busy={status === "working"}
        onReset={() => {
          inFlight.current?.abort();
          setAnalysis(null);
          setSelection(null);
          setStatus("idle");
          setFailure(null);
        }}
      />

      <main id="main" className={styles.main}>
        {status === "ready" && analysis ? (
          <div className={styles.workspace}>
            <PlanPane
              analysis={analysis}
              selection={selection}
              onSelect={setSelection}
              completed={completed}
              onToggleComplete={toggleComplete}
            />
            <SourcePane
              text={analysis.text}
              active={activeEvidence}
              label={activeLabel}
            />
          </div>
        ) : (
          <Composer
            status={status}
            failure={failure}
            onFile={(file) => run((signal) => analyseFile(file, signal))}
            onText={(text, name) => run((signal) => analyseText(text, name, signal))}
          />
        )}
      </main>
    </div>
  );
}

function Header({
  analysis,
  busy,
  onReset,
}: {
  analysis: Analysis | null;
  busy: boolean;
  onReset: () => void;
}) {
  return (
    <header className={styles.header}>
      <div className={styles.brand}>
        <span className={styles.mark} aria-hidden="true" />
        <span className={styles.brandName} translate="no">
          Document&nbsp;→&nbsp;Action
        </span>
      </div>

      {analysis && (
        <>
          <p className={styles.filename} title={analysis.filename}>
            {analysis.filename}
          </p>
          <p className={styles.timing}>
            {analysis.actions.length} step
            {analysis.actions.length === 1 ? "" : "s"} ·{" "}
            {analysis.unresolved_count} to check ·{" "}
            <span className={styles.ms}>{Math.round(analysis.duration_ms)}&nbsp;ms</span>
          </p>
          <button type="button" className={styles.reset} onClick={onReset} disabled={busy}>
            New document
          </button>
        </>
      )}
    </header>
  );
}

function Composer({
  status,
  failure,
  onFile,
  onText,
}: {
  status: Status;
  failure: Failure | null;
  onFile: (file: File) => void;
  onText: (text: string, name: string) => void;
}) {
  const [draft, setDraft] = useState("");
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const busy = status === "working";

  const submitDraft = () => {
    const trimmed = draft.trim();
    if (trimmed && !busy) onText(trimmed, "Pasted text");
  };

  return (
    <div className={styles.composer}>
      <div className={styles.intro}>
        <h1 className={styles.headline}>
          What does this document actually require of you?
        </h1>
        <p className={styles.lede}>
          Upload a notice and get the steps in the order they have to happen,
          each one traced to the sentence it came from — and a plain list of
          what the document never says.
        </p>
      </div>

      <div
        className={`${styles.drop} ${dragging ? styles.dropActive : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          const file = event.dataTransfer.files[0];
          if (file && !busy) onFile(file);
        }}
      >
        <input
          ref={fileInput}
          type="file"
          accept=".pdf,.txt,.md,.png,.jpg,.jpeg,.tif,.tiff,.webp,text/plain,application/pdf,image/*"
          className="visually-hidden"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) onFile(file);
            event.target.value = "";
          }}
        />

        <p className={styles.dropText}>
          Drop a PDF, image, or text file here
        </p>
        <button
          type="button"
          className={styles.browse}
          onClick={() => fileInput.current?.click()}
          disabled={busy}
        >
          {busy ? (
            <>
              <span className={styles.spinner} aria-hidden="true" />
              Choose file
            </>
          ) : (
            "Choose file"
          )}
        </button>
        <p className={styles.limit}>
          PDF, photo or scan, or plain text — up to 20&nbsp;MB. Scans are read by
          OCR where it is installed.
        </p>
      </div>

      <div className={styles.orRow}>
        <span className={styles.rule} aria-hidden="true" />
        <span className={styles.or}>or paste the text</span>
        <span className={styles.rule} aria-hidden="true" />
      </div>

      <div className={styles.pasteBlock}>
        <label htmlFor="paste" className="visually-hidden">
          Paste document text
        </label>
        <textarea
          id="paste"
          className={styles.paste}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
              event.preventDefault();
              submitDraft();
            }
          }}
          placeholder="Paste a notice, circular, or job description…"
          rows={5}
          spellCheck={false}
        />
        <div className={styles.pasteFoot}>
          <span className={styles.shortcut}>
            <kbd>⌘</kbd>
            <kbd>↵</kbd> to analyse
          </span>
          <button
            type="button"
            className={styles.primary}
            onClick={submitDraft}
            disabled={busy || draft.trim().length === 0}
          >
            {busy && <span className={styles.spinner} aria-hidden="true" />}
            Analyse
          </button>
        </div>
      </div>

      <div className={styles.samples}>
        <p className={styles.samplesLabel}>Or try one of these</p>
        <div className={styles.sampleRow}>
          {SAMPLES.map((sample) => (
            <button
              key={sample.id}
              type="button"
              className={styles.sample}
              onClick={() => onText(sample.text, sample.name)}
              disabled={busy}
            >
              <span className={styles.sampleName}>{sample.name}</span>
              <span className={styles.sampleSummary}>{sample.summary}</span>
            </button>
          ))}
        </div>
      </div>

      <div aria-live="polite" className={styles.live}>
        {busy && <p className={styles.working}>Analysing the document…</p>}
        {status === "error" && failure && (
          <div className={styles.failure} role="alert">
            <p className={styles.failureMessage}>{failure.message}</p>
            {failure.remedy && <p className={styles.failureRemedy}>{failure.remedy}</p>}
          </div>
        )}
      </div>
    </div>
  );
}
