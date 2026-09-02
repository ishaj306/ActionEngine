"use client";

// Clerk Core 3 removed <SignedIn> / <SignedOut>: they are still exported but
// throw at render. The hook is the supported path, and it also gives us
// `isLoaded`, which the components never exposed -- so the signed-out view no
// longer flashes for a moment before the session resolves.
import { SignInButton, UserButton, useAuth } from "@clerk/nextjs";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  analyseFile,
  analyseText,
  fetchAnalysis,
  fetchChanges,
  fetchPortfolio,
  updatePlan,
  type Analysis,
  type Comparison,
  type Evidence,
  type Portfolio,
  type Profile,
} from "@/lib/api";
import { SAMPLES } from "@/lib/samples";
import { ChangesBanner, PortfolioPanel } from "@/components/CrossDocument";
import { PlanPane, type Selection } from "@/components/PlanPane";
import { SourcePane } from "@/components/SourcePane";
import styles from "./page.module.css";

type Status = "idle" | "working" | "ready" | "error";

interface Failure {
  message: string;
  remedy: string | undefined;
}

/** Documents read in this session, so they can be compared with each other. */
interface Seen {
  id: string;
  name: string;
}

const LAST_DOCUMENT = "document-action:last";

/** The fields the workspace dereferences without checking first. */
function isRenderable(value: Analysis | null): value is Analysis {
  return Boolean(
    value &&
      typeof value.document_id === "string" &&
      typeof value.text === "string" &&
      value.document_type?.rationale !== undefined &&
      Array.isArray(value.actions) &&
      Array.isArray(value.conditions) &&
      Array.isArray(value.completed),
  );
}

export default function Page() {
  const { isLoaded, isSignedIn } = useAuth();
  const [status, setStatus] = useState<Status>("idle");
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [history, setHistory] = useState<Seen[]>([]);
  const [comparison, setComparison] = useState<
    { result: Comparison; previousName: string } | null
  >(null);
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [patching, setPatching] = useState(false);
  const inFlight = useRef<AbortController | null>(null);

  const remember = useCallback((result: Analysis) => {
    setAnalysis(result);
    setHistory((current) => {
      const without = current.filter((item) => item.id !== result.document_id);
      return [...without, { id: result.document_id, name: result.filename }];
    });
    try {
      window.localStorage.setItem(LAST_DOCUMENT, result.document_id);
    } catch {
      // Private browsing refuses storage. Losing the handle across a reload is
      // a small loss; failing to render the analysis would not be.
    }
  }, []);

  // Bring back the last document read on this machine, so a reload does not
  // throw away a plan with ticks on it.
  useEffect(() => {
    let cancelled = false;
    let stored: string | null = null;
    try {
      stored = window.localStorage.getItem(LAST_DOCUMENT);
    } catch {
      return;
    }
    if (!stored) return;

    fetchAnalysis(stored)
      .then((result) => {
        if (cancelled) return;
        // Everything else in the app receives an analysis it just requested.
        // This one arrives from a previous run against a server that may since
        // have been rebuilt, so it is the one payload worth checking before
        // rendering — an older shape here took the whole page down.
        if (!isRenderable(result)) return;
        remember(result);
        setStatus("ready");
      })
      .catch(() => {
        // The server restarted, or the document was evicted from its cache.
        // Either way there is nothing to restore and nothing to report.
        try {
          window.localStorage.removeItem(LAST_DOCUMENT);
        } catch {
          /* nothing further to do */
        }
      });

    return () => {
      cancelled = true;
    };
  }, [remember]);

  const run = useCallback(
    async (work: (signal: AbortSignal) => Promise<Analysis>) => {
      inFlight.current?.abort();
      const controller = new AbortController();
      inFlight.current = controller;

      setStatus("working");
      setFailure(null);
      try {
        const result = await work(controller.signal);
        if (controller.signal.aborted) return;
        remember(result);
        setSelection(null);
        setComparison(null);
        setPortfolio(null);
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
    },
    [remember],
  );

  /**
   * Every change to the reader's own state goes back to the server and comes
   * back as a recomputed plan. Ticking a step off is not a strikethrough: it
   * unblocks what was waiting on it and takes it out of the feasibility sum.
   */
  const patch = useCallback(
    async (body: Parameters<typeof updatePlan>[1]) => {
      if (!analysis) return;
      setPatching(true);
      try {
        setAnalysis(await updatePlan(analysis.document_id, body));
      } catch (cause) {
        const error = cause instanceof ApiError ? cause : null;
        setFailure({
          message: error?.message ?? "The plan could not be updated.",
          remedy: error?.remedy,
        });
      } finally {
        setPatching(false);
      }
    },
    [analysis],
  );

  const toggleComplete = useCallback(
    (id: string) => {
      if (!analysis) return;
      const next = new Set(analysis.completed);
      if (!next.delete(id)) next.add(id);
      void patch({ completed: [...next] });
    },
    [analysis, patch],
  );

  const compareWith = useCallback(
    async (previousId: string) => {
      if (!analysis) return;
      const previous = history.find((item) => item.id === previousId);
      try {
        const result = await fetchChanges(analysis.document_id, previousId);
        setComparison({ result, previousName: previous?.name ?? "the earlier version" });
        setPortfolio(null);
      } catch (cause) {
        const error = cause instanceof ApiError ? cause : null;
        setFailure({
          message: error?.message ?? "The comparison failed.",
          remedy: undefined,
        });
      }
    },
    [analysis, history],
  );

  const readTogether = useCallback(async () => {
    try {
      setPortfolio(await fetchPortfolio(history.map((item) => item.id)));
      setComparison(null);
    } catch (cause) {
      const error = cause instanceof ApiError ? cause : null;
      setFailure({
        message: error?.message ?? "The documents could not be read together.",
        remedy: undefined,
      });
    }
  }, [history]);

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
      case "condition":
        return (
          analysis.conditions.find((c) => c.requirement === selection.id)?.evidence ??
          null
        );
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
    if (selection.kind === "condition") return selection.id;
    if (selection.kind === "deadline") return "the deadline";
    if (selection.kind === "type") return "the document type";
    return "the title";
  }, [analysis, selection]);

  const others = history.filter((item) => item.id !== analysis?.document_id);

  return (
    <div className={styles.shell}>
      <Header
        signedIn={Boolean(isSignedIn)}
        authLoaded={isLoaded}
        analysis={analysis}
        busy={status === "working"}
        onReset={() => {
          inFlight.current?.abort();
          setAnalysis(null);
          setSelection(null);
          setComparison(null);
          setPortfolio(null);
          setStatus("idle");
          setFailure(null);
          try {
            window.localStorage.removeItem(LAST_DOCUMENT);
          } catch {
            /* nothing further to do */
          }
        }}
      />

      <main id="main" className={styles.main}>
        {status === "ready" && analysis ? (
          <div className={styles.workspace}>
            <div className={styles.left}>
              {others.length > 0 && (
                <SessionBar
                  others={others}
                  onCompare={compareWith}
                  onReadTogether={readTogether}
                  canReadTogether={history.length >= 2}
                />
              )}

              {comparison && (
                <ChangesBanner
                  comparison={comparison.result}
                  previousName={comparison.previousName}
                  onDismiss={() => setComparison(null)}
                />
              )}

              {portfolio && (
                <PortfolioPanel
                  portfolio={portfolio}
                  onClose={() => setPortfolio(null)}
                />
              )}

              <PlanPane
                analysis={analysis}
                selection={selection}
                onSelect={setSelection}
                onToggleComplete={toggleComplete}
                onProfile={(profile: Profile) => void patch({ profile })}
                onClearProfile={() => void patch({ clear_profile: true })}
                busy={patching}
              />
            </div>

            <SourcePane
              text={analysis.text}
              active={activeEvidence}
              label={activeLabel}
            />
          </div>
        ) : !isLoaded ? (
          // Neither view until the session is known. Showing the signed-out
          // page first makes a returning user watch it disappear.
          <div className={styles.composer} aria-busy="true" />
        ) : isSignedIn ? (
          <Composer
            status={status}
            failure={failure}
            onFile={(file) => run((signal) => analyseFile(file, signal))}
            onText={(text, name) => run((signal) => analyseText(text, name, signal))}
          />
        ) : (
          <SignedOutIntro />
        )}
      </main>
    </div>
  );
}

/**
 * Reading a second document is what makes the third and fourth behaviours
 * possible, so the affordance appears the moment there is a second one — and
 * not a moment before, when it would be a dead control.
 */
function SessionBar({
  others,
  onCompare,
  onReadTogether,
  canReadTogether,
}: {
  others: Seen[];
  onCompare: (id: string) => void;
  onReadTogether: () => void;
  canReadTogether: boolean;
}) {
  const [choice, setChoice] = useState(others[others.length - 1]?.id ?? "");

  return (
    <div className={styles.sessionBar}>
      <span className={styles.sessionLabel}>
        {others.length + 1} documents this session
      </span>

      <div className={styles.sessionControls}>
        <label htmlFor="compare-with" className="visually-hidden">
          Earlier version to compare against
        </label>
        <select
          id="compare-with"
          className={styles.sessionSelect}
          value={choice}
          onChange={(event) => setChoice(event.target.value)}
        >
          {others.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
        <button
          type="button"
          className={styles.sessionButton}
          onClick={() => onCompare(choice)}
          disabled={!choice}
        >
          What changed
        </button>
        <button
          type="button"
          className={styles.sessionButton}
          onClick={onReadTogether}
          disabled={!canReadTogether}
        >
          Read them together
        </button>
      </div>
    </div>
  );
}

function Header({
  analysis,
  busy,
  onReset,
  signedIn,
  authLoaded,
}: {
  analysis: Analysis | null;
  busy: boolean;
  onReset: () => void;
  signedIn: boolean;
  authLoaded: boolean;
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

      <div className={styles.account}>
        {!authLoaded ? null : signedIn ? (
          <UserButton />
        ) : (
          <SignInButton mode="modal">
            <button type="button" className={styles.reset}>
              Sign in
            </button>
          </SignInButton>
        )}
      </div>
    </header>
  );
}

/**
 * What a signed-out visitor sees.
 *
 * It explains what the tool does and why signing in is the price, rather than
 * showing a bare wall. People are being asked to upload marksheets and identity
 * documents; the reason their work is kept to their own account is the most
 * relevant thing on the page.
 */
function SignedOutIntro() {
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

      <div className={styles.drop}>
        <p className={styles.dropText}>Sign in to analyse a document</p>
        <SignInButton mode="modal">
          <button type="button" className={styles.primary}>
            Continue with Google
          </button>
        </SignInButton>
        <p className={styles.limit}>
          Your documents are visible only to your account, and you can delete
          all of them at any time. Nothing is shared with anyone else.
        </p>
      </div>
    </div>
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
