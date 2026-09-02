"""Reading several documents about the same thing at once.

A single process arrives as four documents: the notice, the corrigendum, the
department circular and the form's own instructions. They disagree. One says
the 18th and one says the 22nd; one wants a self-attested copy and one wants
the original. Every reader of a bureaucracy has been caught by this, and no
document AI product answers it, because answering it means holding two
documents' claims side by side rather than summarising each in turn.

Two outputs, and the boundary between them is a deliberate scope decision.

**Contradictions** are asserted only between documents that are demonstrably
about the same subject, and only on values the engine can compare exactly.
Two deadlines that differ is a contradiction. Two documents listing different
requirements is not -- that is simply two documents, and reporting it as a
conflict would bury the real ones.

**A merged timeline** orders every step from every document by the date it has
to start. What it deliberately does *not* do is infer dependencies across
documents. Whether the circular's step blocks the notice's step is a question
about the world, not about the text, and a fabricated cross-document edge
would produce a confident schedule built on nothing. Steps stay attributed to
the document they came from, and the reader draws the links the engine cannot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum

from app.modules.action_engine.planner import ScheduledAction
from app.modules.reasoning.relevance import Attribute, Comparator, Criterion
from app.pipeline import Analysis

__all__ = [
    "Conflict",
    "ConflictKind",
    "Portfolio",
    "ScheduledItem",
    "review",
]


class ConflictKind(str, Enum):
    DEADLINE = "deadline"
    ELIGIBILITY = "eligibility"


@dataclass(frozen=True, slots=True)
class Conflict:
    kind: ConflictKind
    summary: str
    #: (document id, document name, the value that document states).
    positions: tuple[tuple[str, str, str], ...]
    #: How confident we are the documents are about the same thing, 0 to 1.
    relatedness: float
    #: What the reader should do, since the engine cannot decide which is right.
    resolution: str


@dataclass(frozen=True, slots=True)
class ScheduledItem:
    """One step from one document, placed on the shared timeline."""

    document_id: str
    document_name: str
    action: ScheduledAction


@dataclass(frozen=True, slots=True)
class Portfolio:
    timeline: tuple[ScheduledItem, ...]
    conflicts: tuple[Conflict, ...]
    #: Steps with no governing date, which therefore have no place in an
    #: ordering by date. Listed rather than dropped.
    undated: tuple[ScheduledItem, ...]

    @property
    def is_consistent(self) -> bool:
        return not self.conflicts

    @property
    def next_due(self) -> date | None:
        """The soonest date anything across these documents has to be finished.

        Named for what it is. This is often *earlier* than any date any of the
        documents states, because a prerequisite inherits the deadline of what
        depends on it -- which is the point, but calling it "the deadline"
        would put a date on screen that appears in none of the sources.
        """
        dated = [
            item.action.effective_deadline
            for item in self.timeline
            if item.action.effective_deadline
        ]
        return min(dated) if dated else None

    @property
    def next_stated_deadline(self) -> date | None:
        """The soonest deadline a document actually states."""
        dated = [
            item.action.action.deadline
            for item in self.timeline
            if item.action.action.deadline
        ]
        return min(dated) if dated else None


#: Documents must share at least this much distinctive vocabulary before their
#: disagreements are called contradictions rather than differences.
_SAME_SUBJECT = 0.25

_WORD = re.compile(r"[a-z]{4,}")

_FILLER = frozenset(
    """
    student students candidate candidates applicant applicants must should
    shall will your with from this that before after office submit form
    document documents date last notice
    """.split()
)


def review(analyses: dict[str, tuple[str, Analysis]]) -> Portfolio:
    """Read several analysed documents as one body of instructions.

    `analyses` maps document id to (display name, analysis).
    """
    items: list[ScheduledItem] = []
    undated: list[ScheduledItem] = []

    for document_id, (name, analysis) in analyses.items():
        for action in analysis.plan.scheduled:
            item = ScheduledItem(
                document_id=document_id, document_name=name, action=action
            )
            (items if action.latest_start else undated).append(item)

    items.sort(
        key=lambda item: (
            item.action.latest_start or date.max,
            item.document_name,
            item.action.order,
        )
    )
    undated.sort(key=lambda item: (item.document_name, item.action.order))

    return Portfolio(
        timeline=tuple(items),
        conflicts=tuple(_conflicts(analyses)),
        undated=tuple(undated),
    )


def _conflicts(analyses: dict[str, tuple[str, Analysis]]):
    entries = list(analyses.items())
    for index, (left_id, (left_name, left)) in enumerate(entries):
        for right_id, (right_name, right) in entries[index + 1 :]:
            overlap = _subject_overlap(left, right)
            if overlap < _SAME_SUBJECT:
                continue
            yield from _deadline_conflict(
                (left_id, left_name, left), (right_id, right_name, right), overlap
            )
            yield from _eligibility_conflicts(
                (left_id, left_name, left), (right_id, right_name, right), overlap
            )


def _deadline_conflict(left, right, overlap: float):
    left_id, left_name, left_analysis = left
    right_id, right_name, right_analysis = right

    first = left_analysis.primary_deadline
    second = right_analysis.primary_deadline
    if first is None or second is None or first.value == second.value:
        return

    earlier, later = sorted(
        ((first.value, left_name), (second.value, right_name)),
        key=lambda pair: pair[0],
    )
    yield Conflict(
        kind=ConflictKind.DEADLINE,
        summary=(
            f"Two documents about the same process state different deadlines: "
            f"{_human(earlier[0])} in {earlier[1]}, {_human(later[0])} in {later[1]}."
        ),
        positions=(
            (left_id, left_name, first.value.isoformat()),
            (right_id, right_name, second.value.isoformat()),
        ),
        relatedness=round(overlap, 3),
        resolution=(
            f"Work to {_human(earlier[0])} until the issuing office confirms "
            "which is current. The earlier date is the only one that cannot "
            "cost you the opportunity if it turns out to be the right one."
        ),
    )


def _eligibility_conflicts(left, right, overlap: float):
    """Two documents setting a different bar on the same attribute.

    Only comparable criteria are checked. Two documents naming departments
    differently is a wording difference, and the same reason it cannot decide
    eligibility on its own applies with more force across documents.
    """
    left_id, left_name, left_analysis = left
    right_id, right_name, right_analysis = right

    by_attribute_left = _comparable(left_analysis)
    by_attribute_right = _comparable(right_analysis)

    for attribute in sorted(
        set(by_attribute_left) & set(by_attribute_right), key=lambda item: item.value
    ):
        first = by_attribute_left[attribute]
        second = by_attribute_right[attribute]
        if first.value == second.value and first.comparator is second.comparator:
            continue

        yield Conflict(
            kind=ConflictKind.ELIGIBILITY,
            summary=(
                f"The two documents set different eligibility on {attribute.value}: "
                f"{first.requirement} in {left_name}, "
                f"{second.requirement} in {right_name}."
            ),
            positions=(
                (left_id, left_name, first.requirement),
                (right_id, right_name, second.requirement),
            ),
            relatedness=round(overlap, 3),
            resolution=(
                "Assume the stricter of the two until the issuing office says "
                "otherwise, and ask which document supersedes the other."
            ),
        )


def _comparable(analysis: Analysis) -> dict[Attribute, Criterion]:
    """The first comparable criterion per attribute, in document order."""
    found: dict[Attribute, Criterion] = {}
    for condition in analysis.conditions:
        criterion = condition.criterion
        if criterion.comparator is Comparator.NOT_COMPARABLE:
            continue
        if criterion.attribute not in _COMPARABLE_ATTRIBUTES:
            continue
        found.setdefault(criterion.attribute, criterion)
    return found


#: Attributes whose values are numbers or a closed vocabulary. Free-text ones
#: are excluded for the same reason they can never produce a conflict within a
#: single document: a difference in wording is not a difference in rule.
_COMPARABLE_ATTRIBUTES = frozenset(
    {Attribute.YEAR, Attribute.SCORE, Attribute.CGPA, Attribute.AGE, Attribute.CATEGORY}
)


def _subject_overlap(left: Analysis, right: Analysis) -> float:
    first = _subject(left)
    second = _subject(right)
    if not first or not second:
        return 0.0
    return len(first & second) / min(len(first), len(second))


def _subject(analysis: Analysis) -> frozenset[str]:
    """The distinctive terms naming what a document is about.

    Titles and requirements carry the subject; the boilerplate of instruction
    ("students must submit before") is shared by every notice ever written and
    would make any two of them look related.
    """
    parts: list[str] = []
    if analysis.title:
        parts.append(analysis.title.value)
    parts.extend(item.text for item in analysis.requirements)
    parts.extend(item.action.description for item in analysis.plan.scheduled)
    return frozenset(_WORD.findall(" ".join(parts).lower())) - _FILLER


def _human(value: date) -> str:
    return value.strftime("%d %B %Y")
