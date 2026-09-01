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

export const REQUIREMENT_KINDS = [
  "document",
  "information",
  "condition",
  "unclassified",
] as const;
export type RequirementKind = (typeof REQUIREMENT_KINDS)[number];

export interface Requirement {
  text: string;
  kind: RequirementKind;
}

export const ATTRIBUTES = [
  "year",
  "programme",
  "category",
  "domicile",
  "score",
  "cgpa",
  "age",
] as const;
export type Attribute = (typeof ATTRIBUTES)[number];

export type MatchResult = "matches" | "conflicts" | "unknown";

export type RelevanceVerdict =
  | "applies"
  | "does_not_apply"
  | "undetermined"
  | "not_restricted";

/** One eligibility condition the document states, checked where possible. */
export interface Condition {
  attribute: Attribute;
  requirement: string;
  /** Null until a profile is supplied. */
  match: MatchResult | null;
  profile_value: string | null;
  explanation: string;
  evidence: Evidence;
}

/** Self-declared, all optional. Nothing here is inferred from a document. */
export interface Profile {
  year?: number | null;
  programme?: string | null;
  category?: string | null;
  domicile?: string | null;
  score?: number | null;
  cgpa?: number | null;
  age?: number | null;
}

export interface Analysis {
  document_id: string;
  filename: string;
  title: Claim | null;
  document_type: Claim;
  primary_deadline: Claim | null;
  deadlines: Claim[];
  actions: Action[];
  gaps: Gap[];
  requirements: Requirement[];
  conditions: Condition[];
  /** Null until a profile is supplied; MISSING when the document never says. */
  relevance: Claim | null;
  relevance_verdict: RelevanceVerdict | null;
  completed: string[];
  is_feasible: boolean;
  unresolved_count: number;
  page_count: number;
  source_kind: string;
  needs_ocr: boolean;
  /** Confidence in the characters themselves; below 1 when read by OCR. */
  text_confidence: number;
  ocr_engine: string | null;
  broken_cycles: string[][];
  duration_ms: number;
  text: string;
}

export type Severity = "critical" | "notable" | "minor";

export interface Change {
  kind: string;
  severity: Severity;
  summary: string;
  before: string | null;
  after: string | null;
}

export interface Comparison {
  previous_document_id: string;
  current_document_id: string;
  changes: Change[];
  headline: string;
  /** Shared vocabulary, 0–1. Low means these may not be the same document. */
  relatedness: number;
  warning: string | null;
}

export interface Conflict {
  kind: "deadline" | "eligibility";
  summary: string;
  /** [document id, document name, the value that document states] */
  positions: string[][];
  relatedness: number;
  resolution: string;
}

export interface TimelineItem {
  document_id: string;
  document_name: string;
  action: Action;
}

export interface Portfolio {
  timeline: TimelineItem[];
  undated: TimelineItem[];
  conflicts: Conflict[];
  is_consistent: boolean;
  /** Often earlier than anything stated: prerequisites inherit deadlines. */
  next_due: string | null;
  next_stated_deadline: string | null;
}

export interface EnquiryDraft {
  subject: string;
  body: string;
  question_count: number;
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

async function unwrapAs<T>(response: Response): Promise<T> {
  if (response.ok) return (await response.json()) as T;

  let detail = `Request failed (${response.status}).`;
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") detail = body.detail;
  } catch {
    // A non-JSON error body is still an error.
  }
  throw new ApiError(detail);
}

/**
 * Replace the reader's working state and get the re-derived plan.
 *
 * Completion is not a display concern: a finished prerequisite stops blocking
 * what depends on it and stops counting against feasibility, so the server
 * returns a recomputed plan rather than the client striking a line through.
 */
export async function updatePlan(
  documentId: string,
  patch: { completed?: string[]; profile?: Profile; clear_profile?: boolean },
  signal?: AbortSignal,
): Promise<Analysis> {
  try {
    return await unwrap(
      await fetch(`${BASE}/v1/documents/${documentId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
        signal,
      }),
    );
  } catch (cause) {
    asNetworkError(cause);
  }
}

export async function fetchAnalysis(documentId: string): Promise<Analysis> {
  return unwrap(await fetch(`${BASE}/v1/documents/${documentId}`));
}

export async function fetchChanges(
  documentId: string,
  since: string,
): Promise<Comparison> {
  return unwrapAs<Comparison>(
    await fetch(`${BASE}/v1/documents/${documentId}/changes?since=${since}`),
  );
}

export async function fetchPortfolio(documentIds: string[]): Promise<Portfolio> {
  return unwrapAs<Portfolio>(
    await fetch(`${BASE}/v1/portfolio`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_ids: documentIds }),
    }),
  );
}

export async function fetchEnquiry(documentId: string): Promise<EnquiryDraft> {
  return unwrapAs<EnquiryDraft>(
    await fetch(`${BASE}/v1/documents/${documentId}/enquiry`),
  );
}

export function calendarUrl(documentId: string): string {
  return `${BASE}/v1/documents/${documentId}/calendar.ics`;
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
