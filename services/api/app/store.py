"""Where documents live.

Two implementations behind one protocol: an in-process map for tests and local
runs, and a SQL-backed one for anything with users in it. `DATABASE_URL`
chooses. Both are exercised by the same suite, because a persistence layer that
is only tested in the configuration nobody deploys is not tested.

**What is stored is the document, not the analysis.** A row holds the text, the
page map and the reader's own state; the plan is re-derived on read, in about
five milliseconds. Caching the analysis instead would be faster and would be
the wrong trade: the analysis shape changes with every extraction improvement,
and a column full of last month's output is worse than no column, because it
looks current. The document and what the reader ticked off are the only things
here that are genuinely durable facts.

Migrations are deliberately absent. `create_all` bootstraps the schema, which
is honest for a system with no deployment and no data in it; the moment either
exists, the first schema change needs Alembic and this comment is the marker
for it.
"""

from __future__ import annotations

import json
import os
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from threading import Lock
from typing import Protocol, cast

from sqlalchemy import (
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    delete,
    select,
)
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.modules.ingestion.document import Page, ParsedDocument, SourceKind
from app.modules.reasoning.relevance import Profile

__all__ = [
    "DocumentStore",
    "MemoryStore",
    "SqlStore",
    "StoredDocument",
    "build_store",
]

#: Documents returned by a listing. The plan is re-derived per document, so an
#: unbounded list would turn one request into seconds of work.
LIST_LIMIT = 20

#: Kept only by `MemoryStore`, where nothing is evicted to disk and memory is
#: the only bound there is.
MAX_RETAINED_DOCUMENTS = 64


@dataclass(frozen=True, slots=True)
class StoredDocument:
    """One document and the working state its owner has attached to it."""

    id: str
    owner_id: str
    filename: str
    #: Hash of the text. Not the id -- everyone who uploads the same circular
    #: would share it -- but useful for spotting a re-upload of the same file.
    content_hash: str
    document: ParsedDocument
    completed: frozenset[str] = frozenset()
    profile: Profile | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def with_state(
        self,
        *,
        completed: frozenset[str],
        profile: Profile | None,
    ) -> StoredDocument:
        return replace(self, completed=completed, profile=profile)


class DocumentStore(Protocol):
    """Every read is scoped to an owner. There is no unscoped read."""

    def put(self, stored: StoredDocument) -> None: ...

    def get(self, document_id: str, *, owner_id: str) -> StoredDocument | None: ...

    def recent(self, *, owner_id: str, limit: int = LIST_LIMIT) -> list[StoredDocument]: ...

    def delete(self, document_id: str, *, owner_id: str) -> bool: ...

    def delete_all(self, *, owner_id: str) -> int: ...


# --------------------------------------------------------------------------
# In process
# --------------------------------------------------------------------------


class MemoryStore:
    """Bounded, thread-safe, and gone when the process is.

    Ownership is enforced here rather than at the call sites. A filter that
    lives at the call site has to be remembered at every call site, and
    forgetting it once in one route exposes everything.
    """

    def __init__(self, capacity: int = MAX_RETAINED_DOCUMENTS) -> None:
        self._items: OrderedDict[str, StoredDocument] = OrderedDict()
        self._capacity = capacity
        self._lock = Lock()

    def put(self, stored: StoredDocument) -> None:
        with self._lock:
            self._items[stored.id] = stored
            self._items.move_to_end(stored.id)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)

    def get(self, document_id: str, *, owner_id: str) -> StoredDocument | None:
        with self._lock:
            found = self._items.get(document_id)
            return found if found and found.owner_id == owner_id else None

    def recent(self, *, owner_id: str, limit: int = LIST_LIMIT) -> list[StoredDocument]:
        with self._lock:
            mine = [item for item in reversed(self._items.values()) if item.owner_id == owner_id]
        return mine[:limit]

    def delete(self, document_id: str, *, owner_id: str) -> bool:
        with self._lock:
            found = self._items.get(document_id)
            if found is None or found.owner_id != owner_id:
                return False
            del self._items[document_id]
            return True

    def delete_all(self, *, owner_id: str) -> int:
        with self._lock:
            doomed = [key for key, item in self._items.items() if item.owner_id == owner_id]
            for key in doomed:
                del self._items[key]
            return len(doomed)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


class DocumentRow(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: The extracted text, and the page boundaries needed to turn an offset
    #: back into a page number. The uploaded file itself is deliberately not
    #: kept: once the text is out, holding somebody's scanned Aadhaar on disk
    #: buys nothing and adds an entire category of breach.
    text: Mapped[str] = mapped_column(Text, nullable=False)
    page_bounds: Mapped[str] = mapped_column(Text, nullable=False)

    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    text_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    ocr_engine: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ocr_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    pages_needing_ocr: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    #: The reader's own state. Stored beside the document rather than in its
    #: own table: it is one row per document, always read with it, never
    #: without it.
    completed: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    profile: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    #: Monotonic per-owner insertion order. `created_at` alone sorts wrongly
    #: when two uploads land inside the same clock tick, which SQLite manages
    #: comfortably and which made list order flap in the tests.
    sequence: Mapped[int] = mapped_column(Integer, autoincrement=True, nullable=False)


Index("ix_documents_owner_recent", DocumentRow.owner_id, DocumentRow.sequence.desc())


class SqlStore:
    """Documents in a database, still scoped to their owner on every read."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self._engine = create_engine(url, echo=echo, future=True, connect_args=connect_args)
        Base.metadata.create_all(self._engine)
        self._session = sessionmaker(self._engine, expire_on_commit=False)
        self._lock = Lock()
        self._next = self._highest_sequence()

    def _highest_sequence(self) -> int:
        with self._session() as session:
            top = session.execute(
                select(DocumentRow.sequence).order_by(DocumentRow.sequence.desc()).limit(1)
            ).scalar()
        return (top or 0) + 1

    def put(self, stored: StoredDocument) -> None:
        with self._lock:
            sequence = self._next
            self._next += 1
        with self._session() as session, session.begin():
            existing = session.get(DocumentRow, stored.id)
            if existing is not None:
                # An update to the reader's state, not a new document. The text
                # and page map are immutable once written.
                existing.completed = json.dumps(sorted(stored.completed))
                existing.profile = _dump_profile(stored.profile)
                existing.filename = stored.filename
                return
            session.add(_to_row(stored, sequence))

    def get(self, document_id: str, *, owner_id: str) -> StoredDocument | None:
        with self._session() as session:
            row = session.execute(
                select(DocumentRow).where(
                    DocumentRow.id == document_id,
                    DocumentRow.owner_id == owner_id,
                )
            ).scalar_one_or_none()
        return _from_row(row) if row else None

    def recent(self, *, owner_id: str, limit: int = LIST_LIMIT) -> list[StoredDocument]:
        with self._session() as session:
            rows = (
                session.execute(
                    select(DocumentRow)
                    .where(DocumentRow.owner_id == owner_id)
                    .order_by(DocumentRow.sequence.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
        return [_from_row(row) for row in rows]

    def delete(self, document_id: str, *, owner_id: str) -> bool:
        with self._session() as session, session.begin():
            # A DELETE always yields a CursorResult; the generic Result the
            # stubs declare has no rowcount, so the cast is about the stub
            # rather than about the runtime.
            result = cast(
                CursorResult,
                session.execute(
                    delete(DocumentRow).where(
                        DocumentRow.id == document_id,
                        DocumentRow.owner_id == owner_id,
                    )
                ),
            )
        return bool(result.rowcount)

    def delete_all(self, *, owner_id: str) -> int:
        with self._session() as session, session.begin():
            result = cast(
                CursorResult,
                session.execute(delete(DocumentRow).where(DocumentRow.owner_id == owner_id)),
            )
        return int(result.rowcount or 0)

    def clear(self) -> None:
        with self._session() as session, session.begin():
            session.execute(delete(DocumentRow))


def _to_row(stored: StoredDocument, sequence: int) -> DocumentRow:
    document = stored.document
    return DocumentRow(
        id=stored.id,
        owner_id=stored.owner_id,
        filename=stored.filename,
        content_hash=stored.content_hash,
        text=document.text,
        page_bounds=json.dumps([[page.char_start, page.char_end] for page in document.pages]),
        source_kind=document.source_kind.value,
        text_confidence=document.text_confidence,
        ocr_engine=document.ocr_engine,
        ocr_error=document.ocr_error,
        pages_needing_ocr=json.dumps(list(document.pages_needing_ocr)),
        completed=json.dumps(sorted(stored.completed)),
        profile=_dump_profile(stored.profile),
        created_at=stored.created_at,
        sequence=sequence,
    )


def _from_row(row: DocumentRow) -> StoredDocument:
    bounds = json.loads(row.page_bounds)
    pages = tuple(
        Page(
            number=index + 1,
            text=row.text[start:end],
            char_start=start,
            char_end=end,
        )
        for index, (start, end) in enumerate(bounds)
    )
    document = ParsedDocument(
        text=row.text,
        pages=pages,
        source_kind=SourceKind(row.source_kind),
        pages_needing_ocr=tuple(json.loads(row.pages_needing_ocr)),
        text_confidence=row.text_confidence,
        ocr_engine=row.ocr_engine,
        ocr_error=row.ocr_error,
    )
    return StoredDocument(
        id=row.id,
        owner_id=row.owner_id,
        filename=row.filename,
        content_hash=row.content_hash,
        document=document,
        completed=frozenset(json.loads(row.completed)),
        profile=_load_profile(row.profile),
        created_at=row.created_at,
    )


def _dump_profile(profile: Profile | None) -> str | None:
    return None if profile is None else json.dumps(asdict(profile))


def _load_profile(raw: str | None) -> Profile | None:
    if not raw:
        return None
    return Profile(**json.loads(raw))


def build_store() -> DocumentStore:
    """Pick an implementation from the environment.

    Falling back to memory rather than refusing is the right default here, and
    the opposite of the choice made for authentication. An unconfigured
    database costs the user their history on restart; unconfigured auth costs
    everyone their privacy. Only one of those is safe to guess at.
    """
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return MemoryStore()
    # SQLAlchemy wants an explicit driver; Heroku-style URLs still say postgres://.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    return SqlStore(url)
