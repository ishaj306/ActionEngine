"""Taking the plan out of the app.

Two exports, both deliberately offline. Live calendar and mail integrations
were considered and cut: they cost OAuth scopes, a token store, refresh
handling and a consent screen, and buy a feature every competitor already has.
A file and a draft do the same job for the reader and leave the interesting
work where the interesting work is.

Both exports carry the epistemic layer with them, which is the part worth
noticing. A calendar entry from this system says which sentence it came from
and how far it can be trusted; an entry from anything else says a date.

Lives in the API package on purpose. These are renderings of the wire contract
rather than domain logic, so building them from the same models the client
receives means the calendar can never disagree with the screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app.api.schemas import ActionOut, AnalysisOut

__all__ = ["Enquiry", "calendar_for", "enquiry_for"]

_PRODID = "-//Document to Action Engine//Plan Export//EN"

#: RFC 5545 caps a content line at 75 octets before folding.
_LINE_OCTETS = 75


def calendar_for(analysis: AnalysisOut, *, now: datetime | None = None) -> str:
    """Render the dated parts of a plan as an iCalendar file.

    Every event lands on the day work has to *start*, not the day it is due.
    That is the number the scheduler exists to compute, and putting the
    deadline in the calendar instead is how people end up starting a ten-day
    errand two days out. The deadline still gets its own entry so the date the
    document actually states is never lost.

    An action with no governing deadline gets no event. It could be given one
    by guessing, and guessing a date into somebody's calendar is worse than
    leaving them to schedule it themselves.
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(analysis.filename)}",
    ]

    for action in analysis.actions:
        if action.latest_start is None:
            continue
        lines.extend(_start_event(analysis, action, stamp))

    if analysis.primary_deadline and analysis.primary_deadline.value:
        lines.extend(_deadline_event(analysis, stamp))

    lines.append("END:VCALENDAR")
    return "".join(_fold(line) for line in lines)


def _start_event(analysis: AnalysisOut, action: ActionOut, stamp: str) -> list[str]:
    assert action.latest_start is not None
    inherited = (
        action.effective_deadline is not None
        and action.effective_deadline != action.deadline
    )

    detail = [action.rationale]
    if inherited:
        detail.append(
            f"This date is not stated in the document. It is inherited from a "
            f"later step that depends on this one, due "
            f"{_human(action.effective_deadline)}."
        )
    if action.requires:
        detail.append("Bring: " + ", ".join(action.requires) + ".")
    detail.append(
        f"Confidence: {action.claim.classification.value} "
        f"{action.claim.confidence:.0%} — {action.claim.rationale}"
    )
    if action.claim.evidence:
        detail.append(f'Source: "{action.claim.evidence.text.strip()}"')
    detail.append(f"Allow about {_days(action.effort_days)} for this step.")

    lines = [
        "BEGIN:VEVENT",
        f"UID:{analysis.document_id}-{action.id}@document-action-engine",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{_ics_date(action.latest_start)}",
        f"DTEND;VALUE=DATE:{_ics_date(action.latest_start + timedelta(days=1))}",
        f"SUMMARY:{_escape(f'Start: {action.description}')}",
        f"DESCRIPTION:{_escape(chr(10).join(detail))}",
        f"CATEGORIES:{_escape(action.verb.upper())}",
        # Below the actionable threshold the entry is a prompt to check, not an
        # instruction to follow, and the calendar should say so.
        f"STATUS:{'CONFIRMED' if action.claim.confidence >= 0.75 else 'TENTATIVE'}",
    ]
    lines.extend(_alarm("Starts today. Anything later and the deadline slips."))
    lines.append("END:VEVENT")
    return lines


def _deadline_event(analysis: AnalysisOut, stamp: str) -> list[str]:
    claim = analysis.primary_deadline
    assert claim is not None
    due = date.fromisoformat(claim.value)

    detail = [claim.rationale]
    if claim.evidence:
        detail.append(f'Source: "{claim.evidence.text.strip()}"')
    if not analysis.is_feasible:
        detail.append(
            "This plan does not fit: at least one step needs longer than the "
            "time remaining. Check the steps marked behind schedule."
        )

    lines = [
        "BEGIN:VEVENT",
        f"UID:{analysis.document_id}-deadline@document-action-engine",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{_ics_date(due)}",
        f"DTEND;VALUE=DATE:{_ics_date(due + timedelta(days=1))}",
        f"SUMMARY:{_escape(f'Deadline: {analysis.filename}')}",
        f"DESCRIPTION:{_escape(chr(10).join(detail))}",
        f"STATUS:{'CONFIRMED' if claim.confidence >= 0.75 else 'TENTATIVE'}",
    ]
    lines.extend(_alarm("Due today."))
    lines.append("END:VEVENT")
    return lines


def _alarm(message: str) -> list[str]:
    return [
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "TRIGGER:-P1D",
        f"DESCRIPTION:{_escape(message)}",
        "END:VALARM",
    ]


@dataclass(frozen=True, slots=True)
class Enquiry:
    """A draft message asking for what the document failed to say."""

    subject: str
    body: str
    #: Number of open questions the draft asks. Zero means nothing to send.
    question_count: int


def enquiry_for(analysis: AnalysisOut) -> Enquiry:
    """Compose an email asking the issuing department the open questions.

    Every gap the engine reports is, from the reader's side, an email they have
    to write. Writing it for them is the shortest distance between "the
    document does not say" and the reader knowing.

    Quoting the phrase that raised each question matters more than it looks: it
    lets the person on the other end find the sentence and answer in one reply,
    rather than asking which office the reader means.
    """
    subject_topic = analysis.title.value if analysis.title else analysis.filename
    questions: list[str] = []

    for gap in analysis.gaps:
        quoted = gap.evidence.text.strip() if gap.evidence else None
        line = f"{len(questions) + 1}. {gap.question}"
        if quoted:
            line += f'\n   (the notice says "{quoted}")'
        questions.append(line)

    # An eligibility condition the engine declined to settle is also a question
    # the reader has to ask somebody, and it belongs in the same email.
    for condition in analysis.conditions:
        if condition.match != "unknown" or condition.profile_value is None:
            continue
        questions.append(
            f"{len(questions) + 1}. The notice is limited to "
            f"{condition.requirement}. Does that cover "
            f"{condition.profile_value}?"
        )

    if not questions:
        body = (
            f"Dear Sir/Madam,\n\n"
            f"I am writing with reference to {subject_topic}.\n\n"
            "The notice appears complete — there are no unanswered points to "
            "raise. This draft is here in case you want to confirm anything "
            "before submitting.\n\n"
            "Thank you,\n"
        )
        return Enquiry(
            subject=f"Query regarding {subject_topic}",
            body=body,
            question_count=0,
        )

    deadline_line = ""
    if analysis.primary_deadline:
        deadline_line = (
            f" The stated deadline is "
            f"{_human(date.fromisoformat(analysis.primary_deadline.value))}, "
            "so an early reply would help."
        )

    body = (
        "Dear Sir/Madam,\n\n"
        f"I am writing with reference to {subject_topic}. Before I can act on "
        "it I need to confirm a few points the notice does not state.\n\n"
        + "\n\n".join(questions)
        + f"\n\nI would be grateful for clarification on the above.{deadline_line}\n\n"
        "Thank you,\n"
    )

    return Enquiry(
        subject=f"Clarification needed: {subject_topic}",
        body=body,
        question_count=len(questions),
    )


def _days(count: int) -> str:
    return f"{count} day" if count == 1 else f"{count} days"


def _ics_date(value: date) -> str:
    return value.strftime("%Y%m%d")


def _human(value: date | None) -> str:
    return value.strftime("%d %B %Y") if value else "an unstated date"


def _escape(value: str) -> str:
    """Escape per RFC 5545 §3.3.11. Order matters: backslash first."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Fold a content line to 75 octets, per RFC 5545 §3.1.

    Folding counts octets rather than characters, so a continuation break has
    to land on a UTF-8 boundary or the file is corrupt at the byte level while
    looking fine as a Python string.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= _LINE_OCTETS:
        return line + "\r\n"

    pieces: list[str] = []
    remaining = encoded
    limit = _LINE_OCTETS
    while len(remaining) > limit:
        cut = limit
        # Never split a multi-byte character: continuation bytes are 10xxxxxx.
        while cut > 0 and (remaining[cut] & 0xC0) == 0x80:
            cut -= 1
        pieces.append(remaining[:cut].decode("utf-8"))
        remaining = remaining[cut:]
        limit = _LINE_OCTETS - 1  # a leading space costs one octet
    pieces.append(remaining.decode("utf-8"))

    return pieces[0] + "\r\n" + "".join(f" {piece}\r\n" for piece in pieces[1:])
