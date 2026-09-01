/**
 * Client for the analysis API.
 *
 * The types here mirror the server's response models exactly. They are hand-
 * written rather than generated so the shape stays reviewable, and every
 * optional field is optional here for the same reason it is there: the engine
 * genuinely may not know.
 */

export const CLAIM_CLASSES = ["FACT", "INFERENCE", "UNCERTAIN", "MISSING"] as const;
export type ClaimClass = (typeof CLAIM_CLASSES)[number];

export const PRIORITIES = ["overdue", "critical", "high", "medium", "low"] as const;
export type Priority = (typeof PRIORITIES)[number];

export type ActionVerb = "obtain" | "prepare" | "submit" | "attend" | "confirm";

export interface Evidence {
  page: number;
  char_start: number;
  char_end: number;
  text: string;
  excerpt: string;
  match_score: number;
}

export interface Claim {
  value: string;
  classification: ClaimClass;
  confidence: number;
  rationale: string;
  evidence: Evidence | null;
}

export interface Action {
  id: string;
  order: number;
  description: string;
  verb: ActionVerb;
  priority: Priority;
  depth: number;
  requires: string[];
  blocked_by: string[];
  deadline: string | null;
  effective_deadline: string | null;
  latest_start: string | null;
  slack_days: number | null;
  effort_days: number;
  rationale: string;
  claim: Claim;
}

export interface Gap {
  question: string;
  why_it_matters: string;
  suggested_resolution: string | null;
  evidence: Evidence | null;
}

export interface Analysis {
  document_id: string;
  filename: string;
  title: Claim | null;
  primary_deadline: Claim | null;
  deadlines: Claim[];
  actions: Action[];
  gaps: Gap[];
  is_feasible: boolean;
  unresolved_count: number;
  page_count: number;
  source_kind: string;
  needs_ocr: boolean;
  broken_cycles: string[][];
  duration_ms: number;
  text: string;
}

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** An error carrying the server's remedy, so the UI never shows a dead end. */
export class ApiError extends Error {
  readonly remedy: string | undefined;

  constructor(message: string, remedy?: string) {
    super(message);
    this.name = "ApiError";
    this.remedy = remedy;
  }
}

async function unwrap(response: Response): Promise<Analysis> {
  if (response.ok) return (await response.json()) as Analysis;

  let detail = `Request failed (${response.status}).`;
  let remedy: string | undefined;
  try {
    const body = (await response.json()) as { detail?: unknown; remedy?: string };
    if (typeof body.detail === "string") detail = body.detail;
    remedy = body.remedy;
  } catch {
    // A non-JSON error body is still an error; the status text is enough.
  }
  throw new ApiError(detail, remedy);
}

function asNetworkError(cause: unknown): never {
  if (cause instanceof ApiError) throw cause;
  throw new ApiError(
    "Could not reach the analysis service.",
    `Start the API with \`uvicorn app.main:app\` and confirm it is listening on ${BASE}.`,
  );
}

export async function analyseFile(file: File, signal?: AbortSignal): Promise<Analysis> {
  const body = new FormData();
  body.append("file", file);
  try {
    return await unwrap(
      await fetch(`${BASE}/v1/documents`, { method: "POST", body, signal }),
    );
  } catch (cause) {
    asNetworkError(cause);
  }
}

export async function analyseText(
  text: string,
  filename: string,
  signal?: AbortSignal,
): Promise<Analysis> {
  try {
    return await unwrap(
      await fetch(`${BASE}/v1/documents/text`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, filename }),
        signal,
      }),
    );
  } catch (cause) {
    asNetworkError(cause);
  }
}

/** Format an ISO date for display in the viewer's locale. */
export function formatDate(iso: string | null): string {
  if (!iso) return "—";
  const parsed = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return iso;
  return new Intl.DateTimeFormat(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  }).format(parsed);
}

/** Plain-language slack, e.g. "4 days late" or "9 days of buffer". */
export function describeSlack(days: number | null): string | null {
  if (days === null) return null;
  if (days < 0) {
    const late = Math.abs(days);
    return `${late} ${late === 1 ? "day" : "days"} late`;
  }
  if (days === 0) return "Start today";
  return `${days} ${days === 1 ? "day" : "days"} of buffer`;
}
