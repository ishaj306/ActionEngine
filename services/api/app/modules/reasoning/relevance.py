"""Does this document apply to the person reading it?

A notice restricts itself in one or two sentences and then never mentions the
restriction again. The reader who fails that restriction reads the whole thing,
builds the whole plan, and finds out at the counter. So eligibility is worth
answering before anything else in the plan is worth doing.

The answer has to be as careful as every other claim here, which rules out the
obvious approach of asking a model "does this apply to me?". Two rules keep it
honest:

**A conflict is only asserted where the comparison is arithmetic.** Year, marks
and age are numbers, and a number either clears a bar or does not. Programme
and place are open vocabularies: a profile saying "CSE" against a document
saying "Computer Science" has no token in common, and reporting that as *you
are not eligible* would be a confident, wrong, and consequential answer. Those
mismatches come back UNKNOWN with both strings shown, for the reader to settle.

**A criterion that cannot be turned into a comparison says so.** "Final year"
is a real restriction, but which year it names depends on the length of the
programme. It is extracted, displayed, and marked as something the engine
declines to evaluate -- which is more useful than silently dropping it.

Relevance annotates the plan. It never hides it: this extraction is lexical and
can miss, and a plan withheld on a false negative is a worse failure than a
plan shown with a warning on top.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "Assessment",
    "Attribute",
    "Comparator",
    "Criterion",
    "Finding",
    "Match",
    "Profile",
    "Relevance",
    "assess",
    "extract_criteria",
]


class Attribute(str, Enum):
    """A property of the reader that a document can restrict."""

    YEAR = "year"
    PROGRAMME = "programme"
    CATEGORY = "category"
    DOMICILE = "domicile"
    SCORE = "score"
    CGPA = "cgpa"
    AGE = "age"


#: Attributes whose comparison is arithmetic, and may therefore be asserted as
#: a conflict. Everything else can only ever match or be left open.
_ARITHMETIC = frozenset({Attribute.YEAR, Attribute.SCORE, Attribute.CGPA, Attribute.AGE})

#: Closed vocabularies. A value outside the set is a genuine mismatch rather
#: than a wording difference, so these may conflict too.
_CLOSED = frozenset({Attribute.CATEGORY})


class Comparator(str, Enum):
    AT_LEAST = "at_least"
    AT_MOST = "at_most"
    EQUALS = "equals"
    ONE_OF = "one_of"
    #: Recognised as a restriction, but not reducible to a comparison.
    NOT_COMPARABLE = "not_comparable"


class Match(str, Enum):
    MATCHES = "matches"
    CONFLICTS = "conflicts"
    #: The profile is silent, or the comparison cannot be made.
    UNKNOWN = "unknown"


class Relevance(str, Enum):
    APPLIES = "applies"
    DOES_NOT_APPLY = "does_not_apply"
    #: Criteria exist; the profile answers none of them.
    UNDETERMINED = "undetermined"
    #: The document never says who it is for.
    NOT_RESTRICTED = "not_restricted"


@dataclass(frozen=True, slots=True)
class Profile:
    """What the reader has told us about themselves.

    Every field is optional and self-declared. Nothing here is inferred from a
    document, and nothing is required -- a profile that answers one criterion
    is more useful than no profile, and the assessment reports exactly which
    criteria it could and could not evaluate.
    """

    year: int | None = None
    programme: str | None = None
    category: str | None = None
    domicile: str | None = None
    #: Aggregate marks as a percentage.
    score: float | None = None
    cgpa: float | None = None
    age: int | None = None

    @property
    def is_empty(self) -> bool:
        return all(
            getattr(self, field) is None
            for field in ("year", "programme", "category", "domicile", "score", "cgpa", "age")
        )

    def value_for(self, attribute: Attribute) -> object | None:
        return getattr(self, attribute.value, None)


@dataclass(frozen=True, slots=True)
class Criterion:
    """One eligibility condition, as stated by the document."""

    attribute: Attribute
    comparator: Comparator
    #: A number for arithmetic comparators, a tuple of accepted strings for
    #: ONE_OF, and None when the criterion is not comparable.
    value: float | tuple[str, ...] | None
    #: The condition in the document's own words, for display.
    requirement: str
    char_start: int
    char_end: int
    matched_text: str

    @property
    def is_arithmetic(self) -> bool:
        return self.attribute in _ARITHMETIC


@dataclass(frozen=True, slots=True)
class Finding:
    """A criterion checked against the profile."""

    criterion: Criterion
    match: Match
    #: What the profile said, rendered for display. None when it said nothing.
    profile_value: str | None
    explanation: str


@dataclass(frozen=True, slots=True)
class Assessment:
    verdict: Relevance
    confidence: float
    rationale: str
    findings: tuple[Finding, ...]

    @property
    def conflicts(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.match is Match.CONFLICTS)

    @property
    def unresolved(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.match is Match.UNKNOWN)


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

_ORDINAL: dict[str, int] = {
    "first": 1, "1st": 1,
    "second": 2, "2nd": 2,
    "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4,
    "fifth": 5, "5th": 5,
}

#: "second year and above" is a floor, not an equality. Read as equality it
#: excluded every third- and fourth-year student the notice was written for --
#: the most damaging kind of mistake this module can make.
_YEAR = re.compile(
    r"\b(first|second|third|fourth|fifth|final|1st|2nd|3rd|4th|5th)[\s-]+year"
    r"(?P<floor>\s+(?:and|or)\s+(?:above|higher|later)|\s+onwards?)?\b",
    re.I,
)

_SCORE_MIN = re.compile(
    r"\b(?:minimum|at\s+least|not\s+less\s+than|above|over)\s+(?:of\s+)?"
    r"(\d{1,3}(?:\.\d+)?)\s*(?:%|per\s*cent|percent)",
    re.I,
)

_SCORE_FLOOR = re.compile(
    r"\b(\d{1,3}(?:\.\d+)?)\s*(?:%|per\s*cent|percent)\s*(?:or\s+above|and\s+above|or\s+more)\b",
    re.I,
)

_CGPA = re.compile(
    r"\b(?:cgpa|gpa)\s+(?:of\s+)?(?:at\s+least\s+|minimum\s+(?:of\s+)?|not\s+less\s+than\s+)?"
    r"(\d{1,2}(?:\.\d+)?)",
    re.I,
)

# "at least 3 years of experience" is not an age. Requiring the number to look
# like an age, and excluding the words that follow a duration, keeps job
# descriptions from producing a bogus age floor.
_AGE_MIN = re.compile(
    r"\b(?:at\s+least|minimum|not\s+less\s+than|above|over)\s+(?:of\s+)?(\d{2})\s*years?"
    r"(?!\s+of\s+(?:experience|service|work|standing))",
    re.I,
)

_AGE_MAX = re.compile(
    r"\b(?:below|under|not\s+more\s+than|not\s+exceeding|less\s+than|up\s*to)\s+(\d{2})\s*years?"
    r"(?!\s+of\s+(?:experience|service|work|standing))",
    re.I,
)

_CATEGORY_TOKENS = ("SC", "ST", "OBC", "EWS", "PwD", "General", "Unreserved")
_CATEGORY_ALT = "|".join(_CATEGORY_TOKENS)

_CATEGORY_LIST = re.compile(
    rf"\b((?:{_CATEGORY_ALT})(?:\s*(?:/|,|\s+or\s+|\s+and\s+)\s*(?:{_CATEGORY_ALT}))*)"
    r"\s+(?:category\s+)?(?:candidates|students|applicants)\b",
    re.I,
)

_CATEGORY_PHRASE = re.compile(
    rf"\b(?:belonging\s+to|reserved\s+for|open\s+(?:only\s+)?to)\s+(?:the\s+)?"
    rf"((?:{_CATEGORY_ALT})(?:\s*(?:/|,|\s+or\s+|\s+and\s+)\s*(?:{_CATEGORY_ALT}))*)"
    r"(?:\s+category)?",
    re.I,
)

_CATEGORY_TOKEN = re.compile(rf"\b({_CATEGORY_ALT})\b", re.I)

_DOMICILE = re.compile(
    r"\b(?:domiciled?\s+(?:in|of)|(?:permanent\s+)?residents?\s+of)\s+(?:the\s+)?"
    r"([A-Z][\w.]*(?:\s+[A-Z][\w.]*){0,3})",
)

_PROGRAMME_OF = re.compile(
    r"\b(?:students?|candidates?|applicants?)\s+(?:of|from|enrolled\s+in|pursuing)\s+"
    r"(?:the\s+)?([A-Za-z][\w.&-]*(?:\s+[A-Za-z][\w.&-]*){0,3})\s+"
    r"(?:department|programme|program|course|branch|stream|discipline)\b",
    re.I,
)

#: Deliberately case-sensitive. Case-insensitively, `B\.?\s?E\.?` matches the
#: word "be", and "must be submitted" would announce itself as a degree
#: requirement in every second notice.
_PROGRAMME_DEGREE = re.compile(
    r"\b(B\.?\s?Tech|M\.?\s?Tech|B\.?\s?E\.?|B\.?\s?Sc|M\.?\s?Sc|BCA|MCA|B\.?\s?Com|MBA|BBA)\b",
)

#: Language that frames a sentence as a restriction rather than a description.
_ELIGIBILITY_MARKER = re.compile(
    r"\b(?:eligib\w*|entitled|qualify\w*|qualifying|restricted|open\s+(?:only\s+)?to|"
    r"applicable\s+(?:only\s+)?to|only\s+those|criteri\w+|must\s+have|should\s+have)\b",
    re.I,
)

#: Filler dropped when comparing a profile's wording to a document's.
_STOPWORDS = frozenset(
    "the a an of in and or for department programme program course branch "
    "stream discipline engineering studies school college university".split()
)

_WORD = re.compile(r"[a-z0-9]+")


def extract_criteria(text: str) -> tuple[Criterion, ...]:
    """Find every eligibility condition the document states.

    Returned in document order, with overlapping matches collapsed so a phrase
    caught by two patterns is reported once.
    """
    found: list[Criterion] = []
    found.extend(_year_criteria(text))
    found.extend(_score_criteria(text))
    found.extend(_age_criteria(text))
    found.extend(_category_criteria(text))
    found.extend(_domicile_criteria(text))
    found.extend(_programme_criteria(text))

    found.sort(key=lambda item: (item.char_start, -item.char_end))
    return tuple(_drop_overlapping(found))


def _drop_overlapping(criteria: list[Criterion]) -> list[Criterion]:
    """Keep the first criterion of each attribute in any overlapping region."""
    kept: list[Criterion] = []
    for criterion in criteria:
        clash = any(
            other.attribute is criterion.attribute
            and criterion.char_start < other.char_end
            and other.char_start < criterion.char_end
            for other in kept
        )
        if not clash:
            kept.append(criterion)
    return kept


def _year_criteria(text: str):
    for match in _YEAR.finditer(text):
        word = match.group(1).lower()
        number = _ORDINAL.get(word)
        floor = bool(match.group("floor"))
        yield Criterion(
            attribute=Attribute.YEAR,
            comparator=(
                (Comparator.AT_LEAST if floor else Comparator.EQUALS)
                if number
                else Comparator.NOT_COMPARABLE
            ),
            value=float(number) if number else None,
            requirement=(
                (f"year {number} or later" if floor else f"{match.group(0).strip()} students")
                if number
                else "final-year students"
            ),
            char_start=match.start(),
            char_end=match.end(),
            matched_text=match.group(0),
        )


def _score_criteria(text: str):
    for pattern in (_SCORE_MIN, _SCORE_FLOOR):
        for match in pattern.finditer(text):
            value = float(match.group(1))
            if not 0 < value <= 100:
                continue
            yield Criterion(
                attribute=Attribute.SCORE,
                comparator=Comparator.AT_LEAST,
                value=value,
                requirement=f"at least {_trim(value)}% aggregate",
                char_start=match.start(),
                char_end=match.end(),
                matched_text=match.group(0),
            )

    for match in _CGPA.finditer(text):
        value = float(match.group(1))
        if not 0 < value <= 10:
            continue
        yield Criterion(
            attribute=Attribute.CGPA,
            comparator=Comparator.AT_LEAST,
            value=value,
            requirement=f"CGPA of at least {_trim(value)}",
            char_start=match.start(),
            char_end=match.end(),
            matched_text=match.group(0),
        )


def _age_criteria(text: str):
    for pattern, comparator in ((_AGE_MIN, Comparator.AT_LEAST), (_AGE_MAX, Comparator.AT_MOST)):
        for match in pattern.finditer(text):
            value = float(match.group(1))
            if not 14 <= value <= 99:
                continue
            wording = "at least" if comparator is Comparator.AT_LEAST else "under"
            yield Criterion(
                attribute=Attribute.AGE,
                comparator=comparator,
                value=value,
                requirement=f"{wording} {int(value)} years of age",
                char_start=match.start(),
                char_end=match.end(),
                matched_text=match.group(0),
            )


def _category_criteria(text: str):
    for pattern in (_CATEGORY_LIST, _CATEGORY_PHRASE):
        for match in pattern.finditer(text):
            tokens = tuple(
                token.group(1).upper() for token in _CATEGORY_TOKEN.finditer(match.group(1))
            )
            if not tokens:
                continue
            yield Criterion(
                attribute=Attribute.CATEGORY,
                comparator=Comparator.ONE_OF,
                value=tokens,
                requirement=f"{' / '.join(tokens)} category",
                char_start=match.start(),
                char_end=match.end(),
                matched_text=match.group(0),
            )


def _domicile_criteria(text: str):
    for match in _DOMICILE.finditer(text):
        place = match.group(1).strip()
        yield Criterion(
            attribute=Attribute.DOMICILE,
            comparator=Comparator.ONE_OF,
            value=(place,),
            requirement=f"domiciled in {place}",
            char_start=match.start(),
            char_end=match.end(),
            matched_text=match.group(0),
        )


def _programme_criteria(text: str):
    for match in _PROGRAMME_OF.finditer(text):
        name = match.group(1).strip()
        yield Criterion(
            attribute=Attribute.PROGRAMME,
            comparator=Comparator.ONE_OF,
            value=(name,),
            requirement=f"{name} students",
            char_start=match.start(),
            char_end=match.end(),
            matched_text=match.group(0),
        )

    for match in _PROGRAMME_DEGREE.finditer(text):
        # A degree name is only a restriction where the sentence frames it as
        # one; otherwise it is just the notice describing itself.
        if not _in_eligibility_sentence(text, match.start()):
            continue
        name = " ".join(match.group(1).split())
        yield Criterion(
            attribute=Attribute.PROGRAMME,
            comparator=Comparator.ONE_OF,
            value=(name,),
            requirement=f"{name} students",
            char_start=match.start(),
            char_end=match.end(),
            matched_text=match.group(0),
        )


def _in_eligibility_sentence(text: str, offset: int) -> bool:
    start = max(text.rfind(".", 0, offset), text.rfind("\n", 0, offset)) + 1
    end = len(text)
    for terminator in (".", "\n"):
        found = text.find(terminator, offset)
        if found != -1:
            end = min(end, found + 1)
    return bool(_ELIGIBILITY_MARKER.search(text[start:end]))


# --------------------------------------------------------------------------
# Assessment
# --------------------------------------------------------------------------


def assess(criteria: tuple[Criterion, ...], profile: Profile) -> Assessment:
    """Check a profile against extracted criteria."""
    if not criteria:
        return Assessment(
            verdict=Relevance.NOT_RESTRICTED,
            confidence=0.6,
            rationale=(
                "No eligibility condition was recognised anywhere in this document. "
                "It does not say who it is for, so it may well apply to you — but "
                "an unstated restriction is exactly what cannot be checked."
            ),
            findings=(),
        )

    findings = tuple(_check(criterion, profile) for criterion in criteria)
    conflicts = [item for item in findings if item.match is Match.CONFLICTS]
    matches = [item for item in findings if item.match is Match.MATCHES]
    unknown = [item for item in findings if item.match is Match.UNKNOWN]

    if conflicts:
        # The failing conditions are listed individually right below this, so
        # the summary names them rather than restating each explanation.
        failed = _join(item.criterion.requirement for item in conflicts)
        return Assessment(
            verdict=Relevance.DOES_NOT_APPLY,
            confidence=0.85,
            rationale=(
                f"{_plural(len(conflicts), 'condition')} of {len(findings)} did "
                f"not hold: {failed}. The plan is still shown in full, because "
                "conditions are read off the wording and wording can be read wrong."
            ),
            findings=findings,
        )

    if matches and not unknown:
        return Assessment(
            verdict=Relevance.APPLIES,
            confidence=0.88,
            rationale=(
                f"All {_plural(len(matches), 'condition')} stated in the document "
                "were checked against your profile and every one held."
            ),
            findings=findings,
        )

    if matches:
        checked = len(matches) / len(findings)
        return Assessment(
            verdict=Relevance.APPLIES,
            confidence=round(0.5 + 0.35 * checked, 2),
            rationale=(
                f"{_plural(len(matches), 'condition')} held; "
                f"{len(unknown)} could not be checked "
                f"({_join(item.criterion.requirement for item in unknown)}). "
                "Confirm the rest yourself before relying on this."
            ),
            findings=findings,
        )

    return Assessment(
        verdict=Relevance.UNDETERMINED,
        confidence=0.4,
        rationale=(
            f"This document restricts itself to {_join(item.criterion.requirement for item in findings)}, "
            "and your profile does not answer any of those. Fill in the matching "
            "details and this becomes a straight comparison."
        ),
        findings=findings,
    )


def _check(criterion: Criterion, profile: Profile) -> Finding:
    if criterion.comparator is Comparator.NOT_COMPARABLE:
        return Finding(
            criterion=criterion,
            match=Match.UNKNOWN,
            profile_value=None,
            explanation=(
                f"The document restricts this to {criterion.requirement}, which "
                "depends on how long your programme runs. Only you can settle that."
            ),
        )

    stated = profile.value_for(criterion.attribute)
    if stated is None:
        return Finding(
            criterion=criterion,
            match=Match.UNKNOWN,
            profile_value=None,
            explanation=(
                f"The document requires {criterion.requirement}; your profile "
                f"does not state your {_attribute_label(criterion.attribute)}."
            ),
        )

    if criterion.is_arithmetic:
        return _check_number(criterion, float(stated))  # type: ignore[arg-type]
    return _check_text(criterion, str(stated))


#: How to say what the reader told us, per attribute. Written out rather than
#: assembled from a label and a value, because "Your year of study of 3 meets
#: third year students" is what assembly produces and nobody would write it.
_STATEMENT: dict[Attribute, str] = {
    Attribute.YEAR: "You are in year {value}.",
    Attribute.PROGRAMME: "Your programme is “{value}”.",
    Attribute.CATEGORY: "Your category is {value}.",
    Attribute.DOMICILE: "Your domicile is “{value}”.",
    Attribute.SCORE: "Your aggregate is {value}%.",
    Attribute.CGPA: "Your CGPA is {value}.",
    Attribute.AGE: "You are {value} years old.",
}


def _statement(attribute: Attribute, value: str) -> str:
    return _STATEMENT[attribute].format(value=value)


def _check_number(criterion: Criterion, stated: float) -> Finding:
    assert isinstance(criterion.value, float)
    required = criterion.value
    shown = _trim(stated)

    if criterion.comparator is Comparator.AT_LEAST:
        ok = stated >= required
    elif criterion.comparator is Comparator.AT_MOST:
        ok = stated <= required
    else:
        ok = stated == required

    said = _statement(criterion.attribute, shown)
    if ok:
        return Finding(
            criterion=criterion,
            match=Match.MATCHES,
            profile_value=shown,
            explanation=f"{said} That meets the requirement of {criterion.requirement}.",
        )
    return Finding(
        criterion=criterion,
        match=Match.CONFLICTS,
        profile_value=shown,
        explanation=f"{said} This asks for {criterion.requirement}.",
    )


def _check_text(criterion: Criterion, stated: str) -> Finding:
    """Compare open or closed vocabulary values.

    Only closed vocabularies may conflict. An open one that fails to overlap is
    far more likely to be a wording difference than a real exclusion, and the
    cost of getting that wrong -- telling someone a scholarship is not for them
    -- is not worth the tidier output.
    """
    accepted = criterion.value if isinstance(criterion.value, tuple) else ()
    mine = _tokens(stated)
    said = _statement(criterion.attribute, stated)

    for candidate in accepted:
        theirs = _tokens(candidate)
        if theirs and (theirs <= mine or mine <= theirs):
            return Finding(
                criterion=criterion,
                match=Match.MATCHES,
                profile_value=stated,
                explanation=f"{said} That meets the requirement of {criterion.requirement}.",
            )

    if criterion.attribute in _CLOSED:
        return Finding(
            criterion=criterion,
            match=Match.CONFLICTS,
            profile_value=stated,
            explanation=f"{said} This is limited to {criterion.requirement}.",
        )

    return Finding(
        criterion=criterion,
        match=Match.UNKNOWN,
        profile_value=stated,
        explanation=(
            f"{said} The document says {criterion.requirement}. Those do not "
            "obviously match, and abbreviations are not expanded here — worth "
            "settling yourself rather than trusting either reading."
        ),
    )


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_WORD.findall(text.lower())) - _STOPWORDS


_LABELS: dict[Attribute, str] = {
    Attribute.YEAR: "year of study",
    Attribute.PROGRAMME: "programme",
    Attribute.CATEGORY: "category",
    Attribute.DOMICILE: "domicile",
    Attribute.SCORE: "aggregate",
    Attribute.CGPA: "CGPA",
    Attribute.AGE: "age",
}


def _attribute_label(attribute: Attribute) -> str:
    return _LABELS[attribute]


def _trim(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _join(items) -> str:
    values = list(items)
    if len(values) == 1:
        return values[0]
    return f"{', '.join(values[:-1])} and {values[-1]}"
