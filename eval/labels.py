"""The label format, and the loader that turns it into something scorable.

Labels cite **quotes**, not character offsets. The plan originally called for
raw offsets and that was a mistake: hand-counting `char_start` for fifty
documents guarantees wrong numbers, and a benchmark whose ground truth is
quietly wrong is worse than no benchmark, because it produces a plausible score
nobody can audit.

So a label says *what the document says*, and this module resolves it to a span
by searching the text. A quote that is absent, or that appears more than once,
raises rather than resolving -- the same discipline the engine applies to model
output, applied to the ground truth itself. Ambiguity is fixed by extending the
quote until it is unique.

Two label fields have no counterpart in the engine yet, deliberately:
`conditional` and `optional`. v1 cannot represent either, and both produce its
most dangerous failures -- a conditional requirement rendered as mandatory sends
someone to fetch a document they do not need; an optional one does the same.
Labelling them now is what forces the fix and what measures it afterwards.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

__all__ = [
    "GoldAction",
    "GoldCondition",
    "GoldDeadline",
    "GoldDocument",
    "GoldRequirement",
    "LabelError",
    "load_corpus",
    "load_document",
]


class LabelError(Exception):
    """A label that cannot be trusted. Always fatal -- never skipped.

    A benchmark that silently drops the entries it cannot parse reports a score
    for a corpus that is not the corpus.
    """


@dataclass(frozen=True, slots=True)
class Span:
    start: int
    end: int

    def overlaps(self, other: Span, *, ratio: float = 0.5) -> bool:
        """True when the two spans share enough characters to be the same find.

        Exact boundary agreement is the wrong bar. An extractor that returns
        "before 18 September 2026" where the label says "18 September 2026" has
        found the right thing, and scoring it wrong would make the benchmark
        measure punctuation instead of extraction.
        """
        shared = min(self.end, other.end) - max(self.start, other.start)
        if shared <= 0:
            return False
        shortest = min(self.end - self.start, other.end - other.start)
        return shortest > 0 and shared / shortest >= ratio


@dataclass(frozen=True, slots=True)
class GoldDeadline:
    value: date
    span: Span
    #: False for a date that is stated but is not a cutoff -- an event date, an
    #: opening date, a date the document mentions in passing.
    is_deadline: bool


@dataclass(frozen=True, slots=True)
class GoldAction:
    verb: str
    span: Span
    #: The gist, for human review of scoring output. Never matched on.
    gist: str
    #: True when the action applies only to some readers.
    conditional: bool
    #: True when the document says the reader may, not must.
    optional: bool
    deadline: date | None


@dataclass(frozen=True, slots=True)
class GoldRequirement:
    text: str
    kind: str
    conditional: bool
    optional: bool


@dataclass(frozen=True, slots=True)
class GoldCondition:
    attribute: str
    comparator: str
    value: str | None


@dataclass(frozen=True, slots=True)
class GoldDocument:
    name: str
    text: str
    document_type: str
    deadlines: tuple[GoldDeadline, ...]
    actions: tuple[GoldAction, ...]
    requirements: tuple[GoldRequirement, ...]
    conditions: tuple[GoldCondition, ...]
    #: The phrase in the document that raises each gap, as a span. Labelled by
    #: quote rather than by keyword because the wording of the *question* is the
    #: engine's business: scoring "Which office is this?" against a label that
    #: said "department" measures phrasing, not detection.
    gaps: tuple[Span, ...]
    notes: str = ""
    #: Phenomena this document exists to test, for per-category reporting.
    tags: tuple[str, ...] = field(default_factory=tuple)


def _resolve(text: str, quote: str, *, where: str) -> Span:
    """Find `quote` in `text`, insisting it appear exactly once."""
    if not quote:
        raise LabelError(f"{where}: empty quote")
    first = text.find(quote)
    if first == -1:
        raise LabelError(
            f"{where}: quote not found in the document: {quote!r}. "
            "Copy it verbatim, including punctuation and line breaks."
        )
    if text.find(quote, first + 1) != -1:
        raise LabelError(
            f"{where}: quote appears more than once: {quote!r}. "
            "Extend it until it is unique."
        )
    return Span(first, first + len(quote))


_REQUIRED_KINDS = {"document", "information", "condition", "unclassified"}
_REQUIRED_VERBS = {"obtain", "prepare", "submit", "attend", "confirm"}
_REQUIRED_TYPES = {"notice", "form", "job_description", "policy", "other"}


def load_document(text_path: Path) -> GoldDocument:
    """Read one document and its labels, validating as it goes."""
    label_path = text_path.with_suffix(".labels.json")
    if not label_path.exists():
        raise LabelError(f"{text_path.name}: no {label_path.name} beside it")

    text = text_path.read_text(encoding="utf-8")
    try:
        raw = json.loads(label_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LabelError(f"{label_path.name}: {exc}") from exc

    name = text_path.stem

    document_type = raw.get("document_type", "other")
    if document_type not in _REQUIRED_TYPES:
        raise LabelError(f"{name}: unknown document_type {document_type!r}")

    deadlines = []
    for index, item in enumerate(raw.get("deadlines", [])):
        where = f"{name}.deadlines[{index}]"
        deadlines.append(
            GoldDeadline(
                value=_as_date(item["date"], where),
                span=_resolve(text, item["quote"], where=where),
                is_deadline=bool(item.get("is_deadline", True)),
            )
        )

    actions = []
    for index, item in enumerate(raw.get("actions", [])):
        where = f"{name}.actions[{index}]"
        verb = item["verb"]
        if verb not in _REQUIRED_VERBS:
            raise LabelError(f"{where}: unknown verb {verb!r}")
        actions.append(
            GoldAction(
                verb=verb,
                span=_resolve(text, item["quote"], where=where),
                gist=item.get("gist", ""),
                conditional=bool(item.get("conditional", False)),
                optional=bool(item.get("optional", False)),
                deadline=_as_date(item["deadline"], where) if item.get("deadline") else None,
            )
        )

    requirements = []
    for index, item in enumerate(raw.get("requirements", [])):
        where = f"{name}.requirements[{index}]"
        kind = item.get("kind", "unclassified")
        if kind not in _REQUIRED_KINDS:
            raise LabelError(f"{where}: unknown kind {kind!r}")
        requirements.append(
            GoldRequirement(
                text=item["text"],
                kind=kind,
                conditional=bool(item.get("conditional", False)),
                optional=bool(item.get("optional", False)),
            )
        )

    conditions = tuple(
        GoldCondition(
            attribute=item["attribute"],
            comparator=item.get("comparator", "equals"),
            value=None if item.get("value") is None else str(item["value"]),
        )
        for item in raw.get("conditions", [])
    )

    return GoldDocument(
        name=name,
        text=text,
        document_type=document_type,
        deadlines=tuple(deadlines),
        actions=tuple(actions),
        requirements=tuple(requirements),
        conditions=conditions,
        gaps=tuple(
            _resolve(text, quote, where=f"{name}.gaps[{index}]")
            for index, quote in enumerate(raw.get("gaps", []))
        ),
        notes=raw.get("notes", ""),
        tags=tuple(raw.get("tags", [])),
    )


def _as_date(value: str, where: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise LabelError(f"{where}: {value!r} is not an ISO date") from exc


def load_corpus(root: Path) -> list[GoldDocument]:
    """Load every labelled document under `root`, in a stable order."""
    documents = [
        load_document(path)
        for path in sorted(root.glob("*.txt"))
    ]
    if not documents:
        raise LabelError(f"no documents found in {root}")
    return documents
