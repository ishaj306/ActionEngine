"""HTTP surface.

Storage is in-process for now, which is a deliberate staging decision rather
than an oversight: the extraction and reasoning modules are the risky part of
this system, and they are worth getting right before a schema is frozen around
their output. The persistence boundary is `Store`, so swapping it for Postgres
touches this file only.
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from threading import Lock

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from app.api.export import calendar_for, enquiry_for
from app.api.schemas import (
    AnalysisOut,
    ComparisonOut,
    ErrorOut,
    PlanPatch,
    PortfolioOut,
    PortfolioRequest,
)
from app.comparison.changes import compare
from app.comparison.crossdoc import review
from app.modules.ingestion.document import ParsedDocument, UnsupportedDocument, parse
from app.modules.reasoning.relevance import Profile
from app.pipeline import Analysis, analyse

logger = logging.getLogger("action_engine")

#: Refuse anything larger before reading it into memory.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

#: Documents retained in process. Oldest are evicted; this is a demo cache, not
#: a database, and the eviction is what keeps memory bounded.
MAX_RETAINED_DOCUMENTS = 64

_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]


@dataclass(slots=True)
class Record:
    """A document, its analysis, and the working state the reader owns.

    The parsed document is kept rather than only its text. Re-analysing from
    extracted text alone would silently flatten a multi-page PDF onto page one
    and take every evidence citation with it, so the state that produced the
    page map has to survive as long as the analysis does.
    """

    analysis: AnalysisOut
    #: The same reading in domain form. Cross-document reasoning works on
    #: findings rather than on the wire model, and both are produced by the one
    #: call in `_record`, so they cannot drift apart.
    reading: Analysis
    document: ParsedDocument
    filename: str
    completed: frozenset[str] = frozenset()
    profile: Profile | None = None


class Store:
    """Bounded, thread-safe map of records, keyed by document id."""

    def __init__(self, capacity: int = MAX_RETAINED_DOCUMENTS) -> None:
        self._items: OrderedDict[str, Record] = OrderedDict()
        self._capacity = capacity
        self._lock = Lock()

    def put(self, record: Record) -> None:
        with self._lock:
            key = record.analysis.document_id
            self._items[key] = record
            self._items.move_to_end(key)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)

    def get(self, document_id: str) -> Record | None:
        with self._lock:
            return self._items.get(document_id)

    def recent(self) -> list[AnalysisOut]:
        with self._lock:
            return [record.analysis for record in reversed(self._items.values())]

    def delete(self, document_id: str) -> bool:
        with self._lock:
            return self._items.pop(document_id, None) is not None


store = Store()

app = FastAPI(
    title="Document → Action Engine",
    version="0.1.0",
    summary="Turns notices into evidence-backed, dependency-ordered action plans.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)


@app.exception_handler(UnsupportedDocument)
async def _unsupported(_request, exc: UnsupportedDocument) -> JSONResponse:
    return JSONResponse(
        status_code=415,
        content=ErrorOut(
            detail=str(exc),
            remedy="Upload a PDF with a text layer, or a plain text file.",
        ).model_dump(),
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "time": datetime.now(UTC).isoformat()}


@app.post("/v1/documents", response_model=AnalysisOut, status_code=201)
async def upload(file: UploadFile = File(...)) -> AnalysisOut:
    """Parse and analyse an uploaded document.

    Synchronous because rule-based analysis of a notice completes in
    milliseconds. Once a model is in the pipeline this becomes a job returning
    202, and the response model already carries everything the polling client
    would need.
    """
    payload = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
        )
    if not payload:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")

    document = parse(payload, filename=file.filename)
    result = _record(document, filename=file.filename or "untitled").analysis

    logger.info(
        "analysed %s: %d actions, %d gaps, %.1fms",
        result.filename,
        len(result.actions),
        len(result.gaps),
        result.duration_ms,
    )
    return result


@app.post("/v1/documents/text", response_model=AnalysisOut, status_code=201)
def upload_text(body: dict[str, str]) -> AnalysisOut:
    """Analyse pasted text.

    The fastest path to a first result, and the one the sample documents use --
    a new user should see what the engine does before deciding whether to hand
    it a real document.
    """
    from app.modules.ingestion.document import from_text

    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="No text was provided.")
    if len(text) > 200_000:
        raise HTTPException(status_code=413, detail="Text exceeds 200,000 characters.")

    document = from_text(text)
    return _record(document, filename=body.get("filename") or "Pasted text").analysis


def _record(
    document: ParsedDocument,
    *,
    filename: str,
    completed: frozenset[str] = frozenset(),
    profile: Profile | None = None,
) -> Record:
    """Analyse a document under some working state, and store the result."""
    analysis = analyse(
        document,
        today=date.today(),
        completed=completed,
        profile=profile,
    )
    record = Record(
        analysis=AnalysisOut.of(analysis, filename=filename, text=document.text),
        reading=analysis,
        document=document,
        filename=filename,
        completed=completed,
        profile=profile,
    )
    store.put(record)
    return record


@app.get("/v1/documents", response_model=list[AnalysisOut])
def list_documents() -> list[AnalysisOut]:
    return store.recent()


@app.get("/v1/documents/{document_id}", response_model=AnalysisOut)
def get_document(document_id: str) -> AnalysisOut:
    return _require(document_id).analysis


@app.patch("/v1/documents/{document_id}", response_model=AnalysisOut)
def update_plan(document_id: str, patch: PlanPatch) -> AnalysisOut:
    """Update the reader's working state and re-derive the plan from it.

    Ticking a step off is not a display concern. A completed prerequisite stops
    blocking what depends on it and stops counting against feasibility, so the
    honest response to a tick is a recomputed plan rather than a struck-through
    line. Re-analysis runs against the stored parsed document, so page maps and
    OCR confidence are exactly what they were on upload.
    """
    record = _require(document_id)

    completed = record.completed
    if patch.completed is not None:
        known = {action.id for action in record.analysis.actions}
        unknown = sorted(set(patch.completed) - known)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"No such action(s) in this plan: {', '.join(unknown)}.",
            )
        completed = frozenset(patch.completed)

    profile = record.profile
    if patch.clear_profile:
        profile = None
    elif patch.profile is not None:
        profile = patch.profile.to_domain()

    return _record(
        record.document,
        filename=record.filename,
        completed=completed,
        profile=profile,
    ).analysis


@app.get("/v1/documents/{document_id}/calendar.ics", response_class=PlainTextResponse)
def calendar(document_id: str) -> PlainTextResponse:
    """The dated steps as an iCalendar file.

    Served as a download rather than pushed to a calendar account. A live
    integration would need OAuth scopes, a token store and a consent screen to
    deliver what a file delivers, and would put writes into somebody's calendar
    on the strength of an extraction.
    """
    record = _require(document_id)
    return PlainTextResponse(
        content=calendar_for(record.analysis),
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{_ascii_slug(record.filename)}.ics"'
            )
        },
    )


class EnquiryOut(BaseModel):
    subject: str
    body: str
    question_count: int


@app.get("/v1/documents/{document_id}/enquiry", response_model=EnquiryOut)
def enquiry(document_id: str) -> EnquiryOut:
    """A draft email asking whatever the document failed to state."""
    draft = enquiry_for(_require(document_id).analysis)
    return EnquiryOut(
        subject=draft.subject,
        body=draft.body,
        question_count=draft.question_count,
    )


@app.get("/v1/documents/{document_id}/changes", response_model=ComparisonOut)
def changes(document_id: str, since: str) -> ComparisonOut:
    """What changed between an earlier reading of a document and this one.

    Compares findings, not text. A reissued notice is reflowed and renumbered,
    so a textual diff of two versions is almost all noise and the one line that
    matters -- the deadline moving forward four days -- is lost in it.
    """
    if since == document_id:
        raise HTTPException(
            status_code=422, detail="A document cannot be compared with itself."
        )
    previous = _require(since)
    current = _require(document_id)

    return ComparisonOut.of(
        compare(previous.reading, current.reading),
        previous_document_id=since,
        current_document_id=document_id,
    )


@app.post("/v1/portfolio", response_model=PortfolioOut)
def portfolio(request: PortfolioRequest) -> PortfolioOut:
    """Read several analysed documents as one body of instructions.

    Contradictions between them are the output worth having: a corrigendum that
    moves a date is only useful next to the notice it corrects.
    """
    seen: dict[str, tuple[str, Analysis]] = {}
    for document_id in request.document_ids:
        if document_id in seen:
            continue
        record = _require(document_id)
        seen[document_id] = (record.filename, record.reading)

    if len(seen) < 2:
        raise HTTPException(
            status_code=422,
            detail="At least two distinct documents are needed to compare them.",
        )
    return PortfolioOut.of(review(seen))


def _require(document_id: str) -> Record:
    found = store.get(document_id)
    if found is None:
        raise HTTPException(status_code=404, detail="No such document.")
    return found


def _ascii_slug(filename: str) -> str:
    """A filename safe to put in a Content-Disposition header.

    Header values are latin-1; a document named in Devanagari would otherwise
    raise on the way out rather than simply download under a duller name.
    """
    stem = filename.rsplit(".", 1)[0][:60]
    cleaned = "".join(
        character if character.isalnum() or character in "-_ " else "-"
        for character in stem
        if character.isascii()
    ).strip()
    return cleaned or "plan"


@app.delete("/v1/documents/{document_id}", status_code=204)
def delete_document(document_id: str) -> None:
    if not store.delete(document_id):
        raise HTTPException(status_code=404, detail="No such document.")
