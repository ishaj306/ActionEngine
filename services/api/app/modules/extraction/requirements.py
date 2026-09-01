"""Group the things a document demands of the reader.

"Income certificate", "bank account details" and "minimum 60% marks" are all
requirements, but the reader does something different with each: fetch a paper,
look up a number, check whether they qualify. Presenting them as one
undifferentiated list makes the reader do that sorting themselves, which is the
work this product exists to remove.

Classification is lexical and conservative. Anything unrecognised stays
`UNCLASSIFIED` rather than being forced into a bucket, because a certificate
filed under "conditions" is worse than one left ungrouped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class RequirementKind(str, Enum):
    """What the reader has to do to satisfy the requirement."""

    #: A physical or digital artefact to obtain and hand over.
    DOCUMENT = "document"
    #: A value to supply on a form.
    INFORMATION = "information"
    #: A criterion the reader either meets or does not.
    CONDITION = "condition"
    #: Recognised as a requirement, but not confidently one of the above.
    UNCLASSIFIED = "unclassified"


#: Nouns that name an artefact. Matched as whole words against the phrase.
_DOCUMENT_TERMS = frozenset(
    """
    certificate marksheet marksheets transcript diploma degree resume cv
    aadhaar aadhar pan passport voter licence license id identity card
    photograph photographs photo photos affidavit undertaking declaration
    form proforma letter offer noc receipt challan proof statement
    testimonial reference bonafide migration transfer character
    """.split()
)

_INFORMATION_TERMS = frozenset(
    """
    number details detail address email phone mobile contact account
    ifsc roll registration enrolment enrollment name dob birth age
    percentage cgpa gpa income salary
    """.split()
)

#: Comparative or eligibility language. A phrase matching one of these is a
#: criterion even when it also mentions a document-ish noun.
_CONDITION_PATTERNS = (
    re.compile(r"\b(?:minimum|maximum|at\s+least|not\s+less\s+than|not\s+more\s+than|"
               r"below|above|under|over|upto|up\s+to|exceeding)\b", re.I),
    re.compile(r"\d+\s*%|\bpercent\b|\bpercentage\s+of\b", re.I),
    re.compile(r"\b(?:eligible|eligibility|qualify|qualifying|criteria|criterion)\b", re.I),
    re.compile(r"\b(?:aged?|years?\s+of\s+age|domicile|resident\s+of|belonging\s+to)\b", re.I),
)

#: "bank account details" is information, not a document, even though "account"
#: alone is ambiguous. Multi-word signals are checked before single terms.
_INFORMATION_PHRASES = (
    re.compile(r"\bbank\s+(?:account|details)", re.I),
    re.compile(r"\baccount\s+(?:number|details)", re.I),
    re.compile(r"\bcontact\s+(?:number|details)", re.I),
)

_TOKEN = re.compile(r"[a-z]+")


@dataclass(frozen=True, slots=True)
class Requirement:
    """One thing the document demands, with what kind of thing it is."""

    text: str
    kind: RequirementKind

    @property
    def is_actionable_artefact(self) -> bool:
        """True when the reader has to go and get something.

        Documents are the requirements that take real calendar time, which is
        why they drive the scheduler's effort estimates.
        """
        return self.kind is RequirementKind.DOCUMENT


def classify_requirement(phrase: str) -> RequirementKind:
    """Bucket a single requirement phrase."""
    text = phrase.strip()
    if not text:
        return RequirementKind.UNCLASSIFIED

    # Conditions are checked first: "minimum 60% in the qualifying marksheet"
    # is a criterion, not a request for the marksheet.
    if any(pattern.search(text) for pattern in _CONDITION_PATTERNS):
        return RequirementKind.CONDITION

    if any(pattern.search(text) for pattern in _INFORMATION_PHRASES):
        return RequirementKind.INFORMATION

    tokens = set(_TOKEN.findall(text.lower()))
    if tokens & _DOCUMENT_TERMS:
        return RequirementKind.DOCUMENT
    if tokens & _INFORMATION_TERMS:
        return RequirementKind.INFORMATION
    return RequirementKind.UNCLASSIFIED


#: Words carrying no identifying weight when comparing two requirements.
_FILLER = frozenset(
    "self attested copy copies of the a an and or two three for with".split()
)


def group(phrases: tuple[str, ...]) -> tuple[Requirement, ...]:
    """Classify each phrase, dropping duplicates and less specific restatements."""
    seen: set[str] = set()
    unique: list[str] = []
    for phrase in phrases:
        key = " ".join(phrase.lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(phrase)

    kept = _drop_subsumed(unique)
    return tuple(
        Requirement(text=phrase, kind=classify_requirement(phrase)) for phrase in kept
    )


def _drop_subsumed(phrases: list[str]) -> list[str]:
    """Collapse phrases that name the same requirement.

    Overlapping cue patterns produce both "previous marksheet" and
    "self-attested copy of previous marksheet" from one sentence. Stripping
    filler words leaves both with the identity {previous, marksheet}, so they
    are the same requirement -- and the fuller wording is the one worth
    keeping, because it says the copy has to be attested.
    """
    representative: dict[frozenset[str], str] = {}
    for phrase in phrases:
        identity = _identity(phrase)
        if not identity:
            continue
        current = representative.get(identity)
        if current is None or len(phrase) > len(current):
            representative[identity] = phrase

    identities = list(representative)
    emitted: set[frozenset[str]] = set()
    kept: list[str] = []

    for phrase in phrases:
        identity = _identity(phrase)
        if not identity:
            kept.append(phrase)
            continue
        if identity in emitted:
            continue
        emitted.add(identity)
        # A proper subset of another identity is a less specific restatement.
        if any(identity < other for other in identities):
            continue
        kept.append(representative[identity])
    return kept


def _identity(phrase: str) -> frozenset[str]:
    """The words that actually distinguish one requirement from another."""
    return frozenset(_TOKEN.findall(phrase.lower())) - _FILLER


def of_kind(
    requirements: tuple[Requirement, ...],
    kind: RequirementKind,
) -> tuple[str, ...]:
    return tuple(item.text for item in requirements if item.kind is kind)
