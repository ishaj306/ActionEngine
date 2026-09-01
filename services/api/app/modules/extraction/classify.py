"""Identify what kind of document this is.

The type steers everything downstream: a job description's "requirements" are
qualifications the reader either has or lacks, while a notice's "requirements"
are papers to go and fetch. Getting the type wrong mislabels the entire plan.

Scoring is lexical and weighted, and the winning margin becomes the confidence.
A document that scores 9 against 8 is genuinely ambiguous and comes back as an
`UNCERTAIN` claim; nothing here is allowed to assert a type it barely won.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class DocumentType(str, Enum):
    NOTICE = "notice"
    FORM = "form"
    JOB_DESCRIPTION = "job_description"
    POLICY = "policy"
    OTHER = "other"


#: Human-readable names for the interface.
TYPE_LABEL: dict[DocumentType, str] = {
    DocumentType.NOTICE: "Notice",
    DocumentType.FORM: "Form",
    DocumentType.JOB_DESCRIPTION: "Job description",
    DocumentType.POLICY: "Policy",
    DocumentType.OTHER: "Document",
}

#: Signals per type, as (pattern, weight). Weights are deliberately coarse --
#: 3 for a phrase that alone nearly settles it, 1 for weak corroboration.
_SIGNALS: dict[DocumentType, tuple[tuple[re.Pattern[str], int], ...]] = {
    DocumentType.NOTICE: (
        (re.compile(r"\b(?:notice|circular|notification|announcement)\b", re.I), 3),
        (re.compile(r"\bit\s+is\s+(?:hereby\s+)?(?:notified|informed|announced)\b", re.I), 3),
        (re.compile(r"\b(?:all\s+(?:students|candidates|employees)|concerned)\s+are\s+"
                    r"(?:hereby\s+)?(?:informed|advised|directed)\b", re.I), 3),
        # A notice addresses a group and tells it to do something by a date.
        (re.compile(r"\b(?:eligible\s+)?(?:students|candidates|applicants|employees)\s+"
                    r"(?:of\s+.{0,30}?\s+)?(?:must|should|shall|are|may)\b", re.I), 2),
        (re.compile(r"\b(?:last\s+date|deadline|on\s+or\s+before|apply\s+before|"
                    r"submit\s+before|before\s+\d{1,2}\s+\w+|closes?\s+on)\b", re.I), 2),
        (re.compile(r"\b(?:scholarship|admission|examination|internship|placement)\b", re.I), 1),
    ),
    DocumentType.FORM: (
        # Weak on its own: notices mention the application form far more often
        # than forms name themselves. The structural signals below carry this.
        (re.compile(r"\bapplication\s+form\b", re.I), 1),
        (re.compile(r"\b(?:fill\s+in|fill\s+up|tick|strike\s+out)\b", re.I), 2),
        # A run of blank-fill rules is the strongest signal a form gives.
        (re.compile(r"_{4,}"), 3),
        (re.compile(r"\b(?:signature|date\s*:)\s*_{0,}", re.I), 1),
        (re.compile(r"\bfor\s+office\s+use\s+only\b", re.I), 3),
    ),
    DocumentType.JOB_DESCRIPTION: (
        (re.compile(r"\b(?:job\s+(?:description|title)|position|vacancy|role)\b", re.I), 2),
        (re.compile(r"\b(?:responsibilities|what\s+you.?ll\s+do|key\s+duties)\b", re.I), 3),
        (re.compile(r"\b(?:qualifications|requirements|skills\s+required|"
                    r"we.?re\s+looking\s+for)\b", re.I), 2),
        (re.compile(r"\b(?:salary|ctc|compensation|benefits|package)\b", re.I), 2),
        (re.compile(r"\b(?:years?\s+of\s+experience|experience\s+in)\b", re.I), 2),
    ),
    DocumentType.POLICY: (
        (re.compile(r"\b(?:policy|guidelines|code\s+of\s+conduct|terms\s+and\s+conditions)\b", re.I), 3),
        (re.compile(r"\b(?:clause|sub-?clause|section\s+\d+|annexure)\b", re.I), 2),
        (re.compile(r"\b(?:shall\s+be\s+(?:deemed|liable)|is\s+governed\s+by|"
                    r"comes?\s+into\s+(?:force|effect))\b", re.I), 3),
        (re.compile(r"\b(?:applicable\s+to\s+all|scope\s+and\s+applicability)\b", re.I), 2),
    ),
}

#: Only the opening of a document is scanned for its type. A notice quoting a
#: policy at length should stay a notice.
_HEAD_CHARS = 2500

#: Total score below which no type is asserted at all.
_MIN_SCORE = 3

#: Characters of the first line treated as the document's heading.
_HEADING_CHARS = 90

#: Added once when a type's language appears in the heading itself. A document
#: titled "NOTICE" is a notice even when its body discusses a policy at length,
#: and without this the body outvotes the title.
_HEADING_BOOST = 2

#: Share of the winning score that must separate it from the runner-up before
#: the type is treated as settled. Proportional rather than absolute: a gap of
#: three points means far less at a total of twelve than at a total of four.
_DECISIVE_RATIO = 0.5
_MODERATE_RATIO = 0.25


@dataclass(frozen=True, slots=True)
class Classification:
    document_type: DocumentType
    confidence: float
    rationale: str
    #: The phrase that contributed most, for the evidence link.
    strongest_signal: tuple[int, int] | None


def classify(text: str) -> Classification:
    """Determine the document's type from its opening."""
    head = text[:_HEAD_CHARS]
    heading = head.split("\n", 1)[0][:_HEADING_CHARS]
    scores: dict[DocumentType, int] = {}
    best_span: dict[DocumentType, tuple[int, int]] = {}
    best_weight: dict[DocumentType, int] = {}

    for document_type, signals in _SIGNALS.items():
        total = 0
        boosted = False
        for pattern, weight in signals:
            match = pattern.search(head)
            if not match:
                continue
            total += weight
            if not boosted and pattern.search(heading):
                total += _HEADING_BOOST
                boosted = True
            if weight > best_weight.get(document_type, 0):
                best_weight[document_type] = weight
                best_span[document_type] = (match.start(), match.end())
        scores[document_type] = total

    ranked = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0].value))
    winner, top = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0

    if top < _MIN_SCORE:
        return Classification(
            document_type=DocumentType.OTHER,
            confidence=0.3,
            rationale="No strong signal for any known document type.",
            strongest_signal=None,
        )

    margin = (top - runner_up) / top
    if margin >= _DECISIVE_RATIO:
        confidence = 0.9
        rationale = f"Clear {TYPE_LABEL[winner].lower()} language, with no close alternative."
    elif margin >= _MODERATE_RATIO:
        confidence = 0.72
        rationale = (
            f"Reads as a {TYPE_LABEL[winner].lower()}, though it shares language "
            f"with a {TYPE_LABEL[ranked[1][0]].lower()}."
        )
    else:
        confidence = 0.5
        rationale = (
            f"Ambiguous: scores almost equally as a {TYPE_LABEL[winner].lower()} "
            f"and a {TYPE_LABEL[ranked[1][0]].lower()}. Treat the type as unconfirmed."
        )

    return Classification(
        document_type=winner,
        confidence=confidence,
        rationale=rationale,
        strongest_signal=best_span.get(winner),
    )
