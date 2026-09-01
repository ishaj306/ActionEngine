"""What changed when the notice was reissued.

Institutions revise documents constantly and announce almost none of it. The
reader who acted on version one finds out at the counter that version two moved
the deadline forward by four days.

The obvious implementation -- diff the two texts -- is the wrong one. A
reissued notice is reflowed, retyped, given a new letterhead and a new
circular number, so a text diff of two versions of the same notice is almost
entirely noise, and the four-day deadline move is one changed line among two
hundred. What the reader needs is a diff of the *findings*: the deadline, the
steps, the requirements, the eligibility conditions. Those are stable objects
with identities, and comparing them produces three or four lines of pure signal.

Severity follows one rule: a change is critical when it costs the reader time
or eligibility they were counting on. A deadline pulled forward, a new
requirement, a new condition. A deadline pushed back is good news and is not
allowed to shout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum

from app.domain.claims import ClaimClass
from app.modules.action_engine.planner import ScheduledAction

# This module takes whole analyses rather than raw text, so unlike every other
# module here it depends on the composer above it. That is what a cross-version
# reasoner is: its input is two finished readings, not a document.
from app.pipeline import Analysis

__all__ = [
    "Change",
    "ChangeKind",
    "Comparison",
    "Severity",
    "compare",
]


class ChangeKind(str, Enum):
    DEADLINE_MOVED = "deadline_moved"
    DEADLINE_ADDED = "deadline_added"
    DEADLINE_REMOVED = "deadline_removed"
    ACTION_ADDED = "action_added"
    ACTION_REMOVED = "action_removed"
    ACTION_RESCHEDULED = "action_rescheduled"
    REQUIREMENT_ADDED = "requirement_added"
    REQUIREMENT_REMOVED = "requirement_removed"
    CONDITION_ADDED = "condition_added"
    CONDITION_REMOVED = "condition_removed"
    GAP_CLOSED = "gap_closed"
    GAP_OPENED = "gap_opened"


class Severity(str, Enum):
    #: Costs the reader time or eligibility they were counting on.
    CRITICAL = "critical"
    #: Changes the plan without taking anything away.
    NOTABLE = "notable"
    #: Worth listing, not worth interrupting anybody for.
    MINOR = "minor"


@dataclass(frozen=True, slots=True)
class Change:
    kind: ChangeKind
    severity: Severity
    #: One line, written to be read on a banner.
    summary: str
    before: str | None
    after: str | None


@dataclass(frozen=True, slots=True)
class Comparison:
    """The difference between two versions of a document."""

    changes: tuple[Change, ...]
    #: How much the two documents have in common, 0 to 1. Low values mean these
    #: are probably not two versions of one document at all.
    relatedness: float
    #: Set when the two look unrelated, so the UI can say so instead of
    #: presenting fifty spurious changes as a revision history.
    warning: str | None

    @property
    def is_unchanged(self) -> bool:
        return not self.changes

    @property
    def critical(self) -> tuple[Change, ...]:
        return tuple(item for item in self.changes if item.severity is Severity.CRITICAL)

    @property
    def headline(self) -> str:
        if not self.changes:
            return "Nothing that affects your plan has changed."
        critical = self.critical
        if critical:
            return critical[0].summary
        return self.changes[0].summary


#: Below this, the two documents are reported as probably unrelated.
_RELATED_THRESHOLD = 0.35

_WORD = re.compile(r"[a-z]{3,}")

_FILLER = frozenset(
    """
    the and for you your with from that this shall must should will may
    are was were been being have has had not all any
    """.split()
)


_SEVERITY_RANK = {Severity.CRITICAL: 0, Severity.NOTABLE: 1, Severity.MINOR: 2}

#: Causes before their consequences. A moved deadline reschedules every step
#: that depends on it, and leading with one of those reschedulings tells the
#: reader what happened to a step instead of what happened to the document.
_KIND_RANK = {
    kind: index
    for index, kind in enumerate(
        (
            ChangeKind.DEADLINE_MOVED,
            ChangeKind.DEADLINE_ADDED,
            ChangeKind.DEADLINE_REMOVED,
            ChangeKind.CONDITION_ADDED,
            ChangeKind.CONDITION_REMOVED,
            ChangeKind.REQUIREMENT_ADDED,
            ChangeKind.ACTION_ADDED,
            ChangeKind.ACTION_RESCHEDULED,
            ChangeKind.GAP_OPENED,
            ChangeKind.REQUIREMENT_REMOVED,
            ChangeKind.ACTION_REMOVED,
            ChangeKind.GAP_CLOSED,
        )
    )
}


def compare(previous: Analysis, current: Analysis) -> Comparison:
    """Diff two analyses of what is claimed to be the same document."""
    relatedness = _relatedness(previous, current)
    warning = None
    if relatedness < _RELATED_THRESHOLD:
        warning = (
            f"These two documents have only {relatedness:.0%} of their wording in "
            "common, which is low for two versions of one notice. The changes "
            "below may simply be the differences between two unrelated documents."
        )

    changes: list[Change] = []
    changes.extend(_deadline_changes(previous, current))
    shift = _deadline_shift(changes)
    changes.extend(_action_changes(previous, current, implied_by_deadline=shift))
    changes.extend(_requirement_changes(previous, current))
    changes.extend(_condition_changes(previous, current))
    changes.extend(_gap_changes(previous, current))

    changes.sort(key=lambda item: (_SEVERITY_RANK[item.severity], _KIND_RANK[item.kind]))

    return Comparison(
        changes=tuple(changes),
        relatedness=round(relatedness, 3),
        warning=warning,
    )


def _deadline_changes(previous: Analysis, current: Analysis):
    was = previous.primary_deadline
    now = current.primary_deadline

    if was is None and now is not None:
        yield Change(
            kind=ChangeKind.DEADLINE_ADDED,
            severity=Severity.CRITICAL,
            summary=(
                f"A deadline of {_human(now.value)} has been added. The earlier "
                "version stated none."
            ),
            before=None,
            after=now.value.isoformat(),
        )
        return

    if was is not None and now is None:
        yield Change(
            kind=ChangeKind.DEADLINE_REMOVED,
            severity=Severity.NOTABLE,
            summary=(
                f"The deadline of {_human(was.value)} is gone from this version. "
                "It has not been extended — it is simply no longer stated, which "
                "is worth confirming before you slow down."
            ),
            before=was.value.isoformat(),
            after=None,
        )
        return

    if was is None or now is None or was.value == now.value:
        return

    days = (now.value - was.value).days
    earlier = days < 0
    yield Change(
        kind=ChangeKind.DEADLINE_MOVED,
        severity=Severity.CRITICAL if earlier else Severity.NOTABLE,
        summary=(
            f"The deadline moved {'forward' if earlier else 'back'} by "
            f"{abs(days)} day{'' if abs(days) == 1 else 's'}, from "
            f"{_human(was.value)} to {_human(now.value)}."
            + (" You have less time than the plan assumed." if earlier else "")
        ),
        before=was.value.isoformat(),
        after=now.value.isoformat(),
    )


def _deadline_shift(changes: list[Change]) -> int | None:
    """How far the deadline moved, in days, if it moved at all."""
    for change in changes:
        if change.kind is not ChangeKind.DEADLINE_MOVED:
            continue
        if change.before and change.after:
            return (date.fromisoformat(change.after) - date.fromisoformat(change.before)).days
    return None


def _action_changes(
    previous: Analysis,
    current: Analysis,
    *,
    implied_by_deadline: int | None = None,
):
    paired, added, removed = _pair(
        list(previous.plan.scheduled),
        list(current.plan.scheduled),
        key=lambda item: item.action.description,
    )

    for item in added:
        yield Change(
            kind=ChangeKind.ACTION_ADDED,
            severity=Severity.CRITICAL,
            summary=f"New step: {item.action.description}.",
            before=None,
            after=item.action.description,
        )

    for item in removed:
        yield Change(
            kind=ChangeKind.ACTION_REMOVED,
            severity=Severity.MINOR,
            summary=f"No longer required: {item.action.description}.",
            before=item.action.description,
            after=None,
        )

    for was, now in paired:
        change = _reschedule(was, now, implied_by_deadline)
        if change:
            yield change


def _reschedule(
    was: ScheduledAction,
    now: ScheduledAction,
    implied_by_deadline: int | None,
) -> Change | None:
    """Report a step whose start date moved, unless the deadline explains it.

    When the deadline shifts, every dated step shifts with it by construction.
    Listing each one as its own critical finding restates the cause five times
    and leaves the reader with five equally loud lines and no idea which is the
    one that actually happened.

    A start date is also always derived rather than stated, so even an
    unexplained shift is never critical: the finding that matters is whatever
    moved it.
    """
    if was.latest_start == now.latest_start:
        return None

    if (
        implied_by_deadline is not None
        and was.latest_start is not None
        and now.latest_start is not None
        and (now.latest_start - was.latest_start).days == implied_by_deadline
    ):
        return None

    if was.latest_start is None or now.latest_start is None:
        return Change(
            kind=ChangeKind.ACTION_RESCHEDULED,
            severity=Severity.NOTABLE,
            summary=(
                f"“{now.action.description}” now has a start date of "
                f"{_human(now.latest_start)}."
                if now.latest_start
                else f"“{now.action.description}” no longer has a start date."
            ),
            before=was.latest_start.isoformat() if was.latest_start else None,
            after=now.latest_start.isoformat() if now.latest_start else None,
        )

    days = (now.latest_start - was.latest_start).days
    earlier = days < 0
    return Change(
        kind=ChangeKind.ACTION_RESCHEDULED,
        severity=Severity.NOTABLE if earlier else Severity.MINOR,
        summary=(
            f"“{now.action.description}” now has to start "
            f"{abs(days)} day{'' if abs(days) == 1 else 's'} "
            f"{'earlier' if earlier else 'later'}, on {_human(now.latest_start)}."
        ),
        before=was.latest_start.isoformat(),
        after=now.latest_start.isoformat(),
    )


def _requirement_changes(previous: Analysis, current: Analysis):
    _, added, removed = _pair(
        list(previous.requirements),
        list(current.requirements),
        key=lambda item: item.text,
    )

    for item in added:
        yield Change(
            kind=ChangeKind.REQUIREMENT_ADDED,
            severity=Severity.CRITICAL,
            summary=f"You now also need: {item.text}.",
            before=None,
            after=item.text,
        )

    for item in removed:
        yield Change(
            kind=ChangeKind.REQUIREMENT_REMOVED,
            severity=Severity.MINOR,
            summary=f"No longer asked for: {item.text}.",
            before=item.text,
            after=None,
        )


def _condition_changes(previous: Analysis, current: Analysis):
    """Eligibility is the change most worth interrupting somebody for.

    A reader who qualified under version one and does not qualify under version
    two will otherwise find out only when they are turned away.
    """
    was = {
        (item.criterion.attribute, item.criterion.requirement) for item in previous.conditions
    }
    now = {
        (item.criterion.attribute, item.criterion.requirement) for item in current.conditions
    }

    for attribute, requirement in sorted(now - was, key=lambda pair: pair[1]):
        yield Change(
            kind=ChangeKind.CONDITION_ADDED,
            severity=Severity.CRITICAL,
            summary=f"New eligibility condition: {requirement}. Check that you still qualify.",
            before=None,
            after=requirement,
        )

    for attribute, requirement in sorted(was - now, key=lambda pair: pair[1]):
        yield Change(
            kind=ChangeKind.CONDITION_REMOVED,
            severity=Severity.NOTABLE,
            summary=f"The requirement to be {requirement} has been dropped.",
            before=requirement,
            after=None,
        )


def _gap_changes(previous: Analysis, current: Analysis):
    was = {gap.question for gap in previous.gaps}
    now = {gap.question for gap in current.gaps}

    for question in sorted(was - now):
        yield Change(
            kind=ChangeKind.GAP_CLOSED,
            severity=Severity.MINOR,
            summary=f"Answered in this version: {question}",
            before=question,
            after=None,
        )

    for question in sorted(now - was):
        yield Change(
            kind=ChangeKind.GAP_OPENED,
            severity=Severity.NOTABLE,
            summary=f"This version leaves open: {question}",
            before=None,
            after=question,
        )


def _relatedness(previous: Analysis, current: Analysis) -> float:
    """Jaccard overlap of the two documents' distinctive vocabulary.

    Deliberately crude. Its job is not to measure similarity but to catch the
    one case that would make every other number on the screen a lie: two
    unrelated documents compared as if they were revisions of each other.
    """
    left = _vocabulary(previous)
    right = _vocabulary(current)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _vocabulary(analysis: Analysis) -> frozenset[str]:
    parts: list[str] = [analysis.document_type.value]
    if analysis.title:
        parts.append(analysis.title.value)
    parts.extend(item.action.description for item in analysis.plan.scheduled)
    parts.extend(item.text for item in analysis.requirements)
    parts.extend(gap.question for gap in analysis.gaps)
    parts.extend(
        claim.confidence.rationale
        for claim in analysis.deadlines
        if claim.classification is not ClaimClass.UNCERTAIN
    )
    return frozenset(_WORD.findall(" ".join(parts).lower())) - _FILLER


#: Two findings overlapping by at least this much are the same finding,
#: reworded. Set from the failure mode it exists to prevent: adding one
#: requirement to a sentence shifts its wording by roughly a fifth, and
#: reporting that as a step deleted and a step added buries the real change.
_SAME_FINDING = 0.5


def _pair(previous: list, current: list, *, key):
    """Match findings across versions by wording overlap, not equality.

    Reissued notices rewrite around the edges. "Submit the completed form"
    becomes "submit the duly completed form and a caste certificate", and
    matching on exact strings reports one step removed and one added, which is
    both wrong and the noise this whole module exists to remove.

    Greedy on the strongest pair first, so the best available match wins even
    when a weaker one was considered earlier.
    """
    scored = sorted(
        (
            (_overlap(_identity(key(was)), _identity(key(now))), index, other)
            for index, was in enumerate(previous)
            for other, now in enumerate(current)
        ),
        key=lambda triple: (-triple[0], triple[1], triple[2]),
    )

    taken_previous: set[int] = set()
    taken_current: set[int] = set()
    paired: list[tuple] = []

    for score, left, right in scored:
        if score < _SAME_FINDING:
            break
        if left in taken_previous or right in taken_current:
            continue
        taken_previous.add(left)
        taken_current.add(right)
        paired.append((previous[left], current[right]))

    added = [item for index, item in enumerate(current) if index not in taken_current]
    removed = [
        item for index, item in enumerate(previous) if index not in taken_previous
    ]
    return paired, added, removed


def _overlap(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _identity(text: str) -> frozenset[str]:
    """The words that distinguish one finding from another."""
    return frozenset(_WORD.findall(text.lower())) - _FILLER


def _human(value: date | None) -> str:
    return value.strftime("%d %B %Y") if value else "an unstated date"
