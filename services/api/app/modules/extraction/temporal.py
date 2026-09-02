"""Deterministic date and deadline extraction.

Date arithmetic is the one part of this pipeline with a single correct answer,
so a language model has no business performing it. A model that reads
"18 September" and writes `2026-09-18` is guessing at the year; a model that
reads `03/04/2026` and picks a month is guessing at the locale. Both guesses
are invisible in the output and wrong roughly half the time.

This module finds temporal expressions, resolves the ones that can be resolved,
and reports honestly on the ones that cannot -- an ambiguous numeric date comes
back flagged rather than silently disambiguated.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum

_MONTHS: dict[str, int] = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_NUMBER_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fourteen": 14, "fifteen": 15, "twenty": 20, "thirty": 30,
}

_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
_YEAR = r"(?P<year>\d{4})"

#: "18 September 2026", "18th Sept, 2026", "18 September"
_DAY_MONTH = re.compile(
    rf"\b{_DAY}\s+(?:of\s+)?(?P<month>{_MONTH_ALT})\b(?:[,\s]+{_YEAR})?",
    re.IGNORECASE,
)

#: "September 18, 2026", "Sept 18"
_MONTH_DAY = re.compile(
    rf"\b(?P<month>{_MONTH_ALT})\s+{_DAY}\b(?:[,\s]+{_YEAR})?",
    re.IGNORECASE,
)

#: "18/09/2026", "18-09-26". Field order is genuinely ambiguous.
_NUMERIC = re.compile(
    r"\b(?P<first>\d{1,2})[/.\-](?P<second>\d{1,2})[/.\-](?P<year>\d{2,4})\b"
)

#: "within 7 days", "within seven working days", "in 15 days"
_RELATIVE = re.compile(
    r"\b(?:with)?in\s+(?P<count>\d{1,3}|"
    + "|".join(_NUMBER_WORDS)
    + r")\s+(?P<unit>working\s+day|day|week|month)s?\b",
    re.IGNORECASE,
)

#: "from 1 September to 20 September", "between 1 Sep and 20 Sep"
_WINDOW = re.compile(
    r"\b(?:from|between)\s+(?P<start>.{3,32}?)\s+(?:to|and|until|till)\s+(?P<end>.{3,32}?)"
    r"(?=[.,;)]|\s+(?:for|and|the|to)\b|$)",
    re.IGNORECASE | re.DOTALL,
)

#: Phrases that mark a date as the thing the reader must act before.
_DEADLINE_CUES = (
    "before", "by", "no later than", "not later than", "deadline",
    "last date", "final date", "due", "close", "closes", "closing", "closed",
    "on or before", "latest by", "latest", "ends", "ending", "expires",
    "expiry", "cut-off", "cutoff", "till", "until", "up to", "upto",
    "positively",
)

#: Matched on word boundaries. Substring matching made "by" fire inside
#: "nearby" and, worse, made "close" fail because only "closes" was listed.
_CUE_PATTERN = re.compile(
    r"\b(?:" + "|".join(cue.replace(" ", r"\s+").replace("-", r"[- ]") for cue in _DEADLINE_CUES) + r")\b",
    re.I,
)

#: A date is governed by the cues in its own clause. Commas matter here: a
#: series gives each date a separate verb, and ignoring the boundary makes one
#: "closes" mark every date in the sentence as a deadline.
#:
#: A *single* newline is not a boundary. Notices are hard-wrapped, so "before"
#: and the date it governs routinely land on different lines; treating that as
#: a clause break severed the cue from its date and silently lost the deadline
#: in the most ordinary document there is. Only a blank line separates clauses.
_CLAUSE_BREAK = re.compile(r"[,;:.]|\n[ \t]*\n")


class TemporalKind(str, Enum):
    ABSOLUTE = "absolute"
    RELATIVE = "relative"
    WINDOW = "window"


@dataclass(frozen=True, slots=True)
class TemporalExpression:
    """A date-bearing phrase, resolved where resolution is defensible."""

    text: str
    char_start: int
    char_end: int
    kind: TemporalKind
    #: Resolved calendar date, or None when the phrase cannot be pinned down
    #: without information the document does not supply.
    resolved: date | None
    #: End of an application window; set only for `WINDOW`.
    window_end: date | None
    confidence: float
    rationale: str
    #: True when the phrase is preceded by language marking it as a cutoff.
    is_deadline: bool
    #: Set when the same characters admit more than one reading.
    alternate: date | None = None

    @property
    def is_ambiguous(self) -> bool:
        return self.alternate is not None


def extract_temporal(
    text: str,
    *,
    reference: date,
) -> list[TemporalExpression]:
    """Find every temporal expression in `text`.

    `reference` is the date the document was issued or uploaded. It anchors
    relative expressions and supplies the year for dates written without one;
    both uses are reflected in a reduced confidence score.
    """
    found: list[TemporalExpression] = []
    for expression in _scan(text, reference):
        found.append(expression)
    found.sort(key=lambda item: item.char_start)
    return _drop_overlaps(found)


#: How a document states its own date. Only the opening is scanned, because a
#: date this far in is the document's own; one further down is content.
_ISSUE_DATE = re.compile(
    r"\b(?:dated|date(?:d)?\s*:|issued\s+on|circular\s+dated|notice\s+dated)\s*"
    r"(?P<date>\d{1,2}[\s./-][A-Za-z]{3,9}[\s./-]\d{2,4}|"
    r"[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|"
    r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})",
    re.I,
)

#: Characters from the top of the document searched for its own date.
_ISSUE_WINDOW = 400


def issue_date(text: str, *, fallback: date) -> date:
    """The date the document says it was written, if it says so.

    "Registrations must be completed within 10 days of the date of this
    circular" resolves against the circular's date, not against the day someone
    happens to upload it. Anchoring to the reading date made every relative
    period silently wrong for any document not read the day it was issued --
    which is almost all of them, and wrong in the dangerous direction, because
    the computed deadline is later than the real one.
    """
    match = _ISSUE_DATE.search(text[:_ISSUE_WINDOW])
    if not match:
        return fallback
    for candidate in extract_temporal(match.group("date"), reference=fallback):
        if candidate.resolved and candidate.kind is TemporalKind.ABSOLUTE:
            return candidate.resolved
    return fallback


def _scan(text: str, reference: date):
    yield from _scan_windows(text, reference)
    yield from _scan_named(text, reference, _DAY_MONTH, day_first=True)
    yield from _scan_named(text, reference, _MONTH_DAY, day_first=False)
    yield from _scan_numeric(text, reference)
    yield from _scan_relative(text, reference)


def _scan_named(text: str, reference: date, pattern: re.Pattern, *, day_first: bool):
    for match in pattern.finditer(text):
        month = _MONTHS[match.group("month").lower()]
        day = int(match.group("day"))
        raw_year = match.group("year")
        if not _valid_day(day, month):
            continue

        if raw_year:
            resolved = date(int(raw_year), month, day)
            confidence = 0.97
            rationale = "Day, month and year are all stated explicitly."
        else:
            resolved = _next_occurrence(month, day, reference)
            confidence = 0.72
            rationale = (
                f"No year is stated; resolved to the next {calendar.month_name[month]} "
                f"{day} on or after {reference.isoformat()}."
            )

        yield TemporalExpression(
            text=match.group(0),
            char_start=match.start(),
            char_end=match.end(),
            kind=TemporalKind.ABSOLUTE,
            resolved=resolved,
            window_end=None,
            confidence=confidence,
            rationale=rationale,
            is_deadline=_has_deadline_cue(text, match.start()),
        )
        _ = day_first


def _scan_numeric(text: str, reference: date):
    for match in _NUMERIC.finditer(text):
        first, second = int(match.group("first")), int(match.group("second"))
        year = _expand_year(int(match.group("year")))

        day_first = _build(year, second, first)
        month_first = _build(year, first, second)
        if day_first is None and month_first is None:
            continue

        if day_first and month_first and day_first != month_first:
            # 03/04/2026 is 3 April or 4 March depending on convention, and the
            # document does not say which. Reporting one silently is the exact
            # failure this system exists to avoid.
            yield TemporalExpression(
                text=match.group(0),
                char_start=match.start(),
                char_end=match.end(),
                kind=TemporalKind.ABSOLUTE,
                resolved=day_first,
                window_end=None,
                confidence=0.45,
                rationale=(
                    "Numeric date is ambiguous: reads as "
                    f"{day_first.isoformat()} (day/month) or "
                    f"{month_first.isoformat()} (month/day). Confirm before relying on it."
                ),
                is_deadline=_has_deadline_cue(text, match.start()),
                alternate=month_first,
            )
            continue

        resolved = day_first or month_first
        assert resolved is not None
        yield TemporalExpression(
            text=match.group(0),
            char_start=match.start(),
            char_end=match.end(),
            kind=TemporalKind.ABSOLUTE,
            resolved=resolved,
            window_end=None,
            confidence=0.88,
            rationale=(
                "Numeric date; only one field ordering yields a valid calendar date."
            ),
            is_deadline=_has_deadline_cue(text, match.start()),
        )
        _ = reference


def _scan_relative(text: str, reference: date):
    for match in _RELATIVE.finditer(text):
        raw = match.group("count").lower()
        count = int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]
        unit = match.group("unit").lower().replace(" ", "")

        if unit == "workingday":
            resolved = _add_working_days(reference, count)
            note = "working days, excluding weekends"
        elif unit == "day":
            resolved = reference + timedelta(days=count)
            note = "calendar days"
        elif unit == "week":
            resolved = reference + timedelta(weeks=count)
            note = "weeks"
        else:
            resolved = _add_months(reference, count)
            note = "months"

        yield TemporalExpression(
            text=match.group(0),
            char_start=match.start(),
            char_end=match.end(),
            kind=TemporalKind.RELATIVE,
            resolved=resolved,
            window_end=None,
            confidence=0.55,
            rationale=(
                f"Relative period of {count} {note}, counted from "
                f"{reference.isoformat()}. The document does not state the date "
                "the period starts from, so this depends on when it was received."
            ),
            is_deadline=True,
        )


def _scan_windows(text: str, reference: date):
    for match in _WINDOW.finditer(text):
        start_dates = _first_absolute(match.group("start"), reference)
        end_dates = _first_absolute(match.group("end"), reference)
        if start_dates is None or end_dates is None:
            continue
        if end_dates < start_dates:
            continue

        yield TemporalExpression(
            text=match.group(0),
            char_start=match.start(),
            char_end=match.end(),
            kind=TemporalKind.WINDOW,
            resolved=start_dates,
            window_end=end_dates,
            confidence=0.9,
            rationale="Both ends of the period are stated.",
            is_deadline=False,
        )


def _first_absolute(fragment: str, reference: date) -> date | None:
    for pattern in (_DAY_MONTH, _MONTH_DAY):
        match = pattern.search(fragment)
        if not match:
            continue
        month = _MONTHS[match.group("month").lower()]
        day = int(match.group("day"))
        if not _valid_day(day, month):
            continue
        raw_year = match.group("year")
        if raw_year:
            return date(int(raw_year), month, day)
        return _next_occurrence(month, day, reference)
    numeric = _NUMERIC.search(fragment)
    if numeric:
        year = _expand_year(int(numeric.group("year")))
        return _build(year, int(numeric.group("second")), int(numeric.group("first")))
    return None


def _drop_overlaps(items: list[TemporalExpression]) -> list[TemporalExpression]:
    """Keep the widest expression when spans overlap.

    A window match encloses its own two endpoint dates; reporting all three
    would triple-count one deadline.
    """
    kept: list[TemporalExpression] = []
    for item in items:
        covered = any(
            existing.char_start <= item.char_start
            and item.char_end <= existing.char_end
            for existing in kept
        )
        if covered:
            continue
        kept = [
            existing
            for existing in kept
            if not (
                item.char_start <= existing.char_start
                and existing.char_end <= item.char_end
            )
        ]
        kept.append(item)
    kept.sort(key=lambda entry: entry.char_start)
    return kept


def _has_deadline_cue(text: str, position: int) -> bool:
    """Whether a date is framed as a cutoff rather than merely mentioned.

    The search is scoped to the date's own clause, which matters more than it
    sounds. A fixed lookback window is wrong in both directions: too short and
    it misses "The last date for submission of the bursary application was
    4 January 2020" (the cue is 56 characters away); too long and it spills
    across a comma series, so that "opens 1 August, closes 18 September,
    results 30 October" marks all three as deadlines because one of them said
    "closes".

    Clause scope gets every case in that sentence right, because a list like
    that gives each date its own verb.
    """
    start, end = _clause_bounds(text, position)
    before = text[start:position].lower()
    if _CUE_PATTERN.search(before):
        return True
    # "18 September 2026 is the last date for submission" states the cue after
    # the date. Still the same clause, so still governs it.
    return bool(_CUE_PATTERN.search(text[position:end].lower()))


def _clause_bounds(text: str, position: int) -> tuple[int, int]:
    """The clause containing `position`, delimited by punctuation."""
    start = 0
    for match in _CLAUSE_BREAK.finditer(text, 0, position):
        start = match.end()
    following = _CLAUSE_BREAK.search(text, position)
    return start, following.start() if following else len(text)


def _valid_day(day: int, month: int) -> bool:
    return 1 <= month <= 12 and 1 <= day <= calendar.monthrange(2024, month)[1]


def _build(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _expand_year(year: int) -> int:
    """Two-digit years in notices refer to the current century."""
    return year if year >= 100 else 2000 + year


def _next_occurrence(month: int, day: int, reference: date) -> date:
    candidate = _build(reference.year, month, day)
    if candidate is None:  # 29 February in a non-leap year
        return date(reference.year + 1, month, min(day, 28))
    if candidate >= reference:
        return candidate
    following = _build(reference.year + 1, month, day)
    return following or date(reference.year + 1, month, 28)


def _add_working_days(start: date, count: int) -> date:
    current = start
    remaining = count
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def _add_months(start: date, count: int) -> date:
    month_index = start.month - 1 + count
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))
