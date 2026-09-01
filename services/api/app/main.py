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
from datetime import date, datetime, timezone
from threading import Lock

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.schemas import AnalysisOut, ErrorOut
from app.modules.ingestion.document import UnsupportedDocument, parse
from app.pipeline import analyse

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


class Store:
    """Bounded, thread-safe map of analyses, keyed by document id."""

    def __init__(self, capacity: int = MAX_RETAINED_DOCUMENTS) -> None:
        self._items: OrderedDict[str, AnalysisOut] = OrderedDict()
        self._capacity = capacity
        self._lock = Lock()

    def put(self, analysis: AnalysisOut) -> None:
        with self._lock:
            self._items[analysis.document_id] = analysis
            self._items.move_to_end(analysis.document_id)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)

    def get(self, document_id: str) -> AnalysisOut | None:
        with self._lock:
            return self._items.get(document_id)

    def recent(self) -> list[AnalysisOut]:
        with self._lock:
            return list(reversed(self._items.values()))

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
    allow_methods=["GET", "POST", "DELETE"],
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
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


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
    analysis = analyse(document, today=date.today())
    result = AnalysisOut.of(
        analysis,
        filename=file.filename or "untitled",
        text=document.text,
    )
    store.put(result)

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
    analysis = analyse(document, today=date.today())
    result = AnalysisOut.of(
        analysis,
        filename=body.get("filename") or "Pasted text",
        text=document.text,
    )
    store.put(result)
    return result


@app.get("/v1/documents", response_model=list[AnalysisOut])
def list_documents() -> list[AnalysisOut]:
    return store.recent()


@app.get("/v1/documents/{document_id}", response_model=AnalysisOut)
def get_document(document_id: str) -> AnalysisOut:
    found = store.get(document_id)
    if found is None:
        raise HTTPException(status_code=404, detail="No such document.")
    return found


@app.delete("/v1/documents/{document_id}", status_code=204)
def delete_document(document_id: str) -> None:
    if not store.delete(document_id):
        raise HTTPException(status_code=404, detail="No such document.")
