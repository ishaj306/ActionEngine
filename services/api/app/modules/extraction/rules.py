"""Rule-based extraction of actions, requirements and information gaps.

This is not a placeholder for the model. It is the baseline arm of the
evaluation: the product's central claim is that a hybrid of rules and a model
beats either alone, and that claim is unmeasurable without a rule-only system
good enough to be a fair opponent.

It is also the fallback path. When the model is unavailable, over budget, or
returns unparseable output, the engine degrades to these rules rather than to
an error page -- and everything it produces is still evidence-backed, because
rule matches carry their own spans by construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.modules.action_engine.planner import ActionVerb

#: Verbs grouped by what the reader has to do, not by dictionary sense.
_VERB_LEXICON: dict[ActionVerb, frozenset[str]] = {
    ActionVerb.OBTAIN: frozenset(
        "obtain collect get download procure secure acquire request draw".split()
    ),
    ActionVerb.PREPARE: frozenset(
        "complete fill prepare scan update review sign attach enclose compile"
        " affix write".split()
    ),
    # Registering is the act of applying, not the act of showing up, so it
    # belongs to the stage that hands something over.
    ActionVerb.SUBMIT: frozenset(
        "submit upload send deposit pay forward return apply furnish register"
        " enrol enroll remit".split()
    ),
    ActionVerb.ATTEND: frozenset(
        "attend appear report participate join present".split()
    ),
    ActionVerb.CONFIRM: frozenset(
        "confirm verify check contact clarify ensure intimate".split()
    ),
}

#: Forms that regular inflection gets wrong.
_IRREGULAR: dict[str, tuple[str, ...]] = {
    "pay": ("paid",),
    "get": ("got",),
    "draw": ("drew", "drawn"),
    "send": ("sent",),
    "write": ("wrote", "written"),
}

_VOWELS = frozenset("aeiou")


def _inflect(verb: str) -> set[str]:
    """Generate the inflected forms of one verb.

    Notices are written in the passive far more often than the imperative --
    "registrations must be completed", not "complete your registration" -- so
    matching only base forms misses most of the instructions in a document.
    English morphology for a closed set of verbs is small enough to enumerate
    and far more predictable than stemming the input.
    """
    forms = {verb, f"{verb}s"}
    forms.update(_IRREGULAR.get(verb, ()))

    if verb.endswith("e"):
        stem = verb[:-1]
        forms.update({f"{stem}ed", f"{stem}ing"})
    elif len(verb) > 2 and verb.endswith("y") and verb[-2] not in _VOWELS:
        stem = verb[:-1]
        forms.update({f"{stem}ied", f"{stem}ies", f"{verb}ing"})
    elif (
        len(verb) >= 3
        and verb[-1] not in _VOWELS
        and verb[-1] not in "wxy"
        and verb[-2] in _VOWELS
        and verb[-3] not in _VOWELS
    ):
        # Consonant-vowel-consonant doubles the final letter: submit, submitted.
        doubled = verb + verb[-1]
        forms.update({f"{doubled}ed", f"{doubled}ing"})
    else:
        forms.update({f"{verb}ed", f"{verb}ing"})
    return forms


#: Maps every inflected form to its bucket *and* its base form. The base is
#: needed to rewrite a passive sentence back into an instruction.
_VERB_OF: dict[str, tuple[ActionVerb, str]] = {
    form: (verb, word)
    for verb, words in _VERB_LEXICON.items()
    for word in words
    for form in _inflect(word)
}

_VERB_ALT = "|".join(sorted(_VERB_OF, key=len, reverse=True))

#: Obligation language, strongest first. The strength maps onto confidence:
#: "must submit" is an instruction, "may wish to submit" is not.
_OBLIGATION = (
    (0.94, re.compile(r"\b(?:must|shall|are\s+required\s+to|is\s+required\s+to|has\s+to|have\s+to)\b", re.I)),
    (0.86, re.compile(r"\b(?:should|are\s+to|is\s+to|needs?\s+to|are\s+advised\s+to)\b", re.I)),
    (0.70, re.compile(r"\b(?:kindly|please|are\s+requested\s+to)\b", re.I)),
    (0.55, re.compile(r"\b(?:may|can|are\s+encouraged\s+to)\b", re.I)),
)

#: Imperative openings — a notice often just says "Submit the form by …".
_IMPERATIVE = re.compile(rf"^\s*(?:{_VERB_ALT})\b", re.I)

#: Phrases introducing a list of things the reader must bring or enclose.
_REQUIREMENT_CUES = re.compile(
    r"\b(?:along\s+with|accompanied\s+by|together\s+with|enclosing|enclose|attach(?:ed|ing)?|"
    r"supported\s+by|documents?\s+required|required\s+documents?|bring|carry|"
    r"self[- ]attested\s+cop(?:y|ies)\s+of)\b",
    re.I,
)

#: Vague references that name a thing without identifying it. Each one is a
#: question the reader cannot answer from the document alone.
_VAGUE_REFERENTS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"\b(?:the\s+)?(?:designated|concerned|respective|appropriate|relevant)\s+"
                   r"(?:office|department|authority|official|desk|counter)\b", re.I),
        "Which office is this?",
        "The document refers to an office without naming it or giving its location.",
    ),
    (
        re.compile(r"\b(?:the\s+)?(?:prescribed|official|standard)\s+(?:form|format|proforma)\b", re.I),
        "Where is the prescribed form obtained?",
        "A specific form is required but the document does not say where to get it.",
    ),
    (
        re.compile(r"\b(?:online\s+portal|the\s+portal|the\s+website|the\s+link|our\s+website)\b", re.I),
        "What is the portal address?",
        "Submission is online but no URL is given.",
    ),
    (
        re.compile(r"\b(?:as\s+per\s+(?:the\s+)?(?:rules|norms|guidelines|circular)|"
                   r"as\s+applicable|subject\s+to\s+(?:the\s+)?(?:rules|norms|approval))\b", re.I),
        "Which rules apply?",
        "Eligibility or process defers to rules the document does not reproduce.",
    ),
    (
        re.compile(r"\b(?:nominal|applicable|prescribed)\s+(?:fee|charges?|amount)\b", re.I),
        "How much is the fee?",
        "A payment is required but the amount is not stated.",
    ),
    (
        re.compile(r"\bin\s+due\s+course\b|\bshortly\b|\blater\s+date\s+to\s+be\s+(?:announced|notified)\b", re.I),
        "When will this be announced?",
        "A date is promised but not given.",
    ),
)

#: Sentence boundary that does not split on common abbreviations or initials.
_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+(?=[A-Z(])|\n(?=\s*(?:\d+[.)]|[-•*]))|\n{2,}")

_ABBREVIATIONS = frozenset(
    "no dr mr mrs ms prof sr jr etc viz vs approx dept govt univ inst ltd pvt".split()
)

#: Sentences shorter than this are headings or fragments, not instructions.
_MIN_SENTENCE_CHARS = 18

#: Most requirements pulled from a single sentence.
_MAX_REQUIREMENTS = 8

#: Longest rewritten instruction shown to the reader.
_MAX_DESCRIPTION_CHARS = 160

#: A subject longer than this is a clause, not a noun phrase, and folding it
#: back into an imperative produces something worse than the original.
_MAX_SUBJECT_CHARS = 48

#: Matches everything up to and including the obligation marker, capturing the
#: subject that precedes it.
_OBLIGATION_PREFIX = re.compile(
    r"^(?P<subject>.*?)\b(?:must|shall|should|needs?\s+to|are\s+required\s+to|"
    r"is\s+required\s+to|have\s+to|has\s+to|are\s+advised\s+to|"
    r"are\s+requested\s+to|kindly|please)\s+",
    re.I,
)

#: "be completed", "been submitted" -- the passive that follows the modal.
_PASSIVE_HEAD = re.compile(r"^(?:be|been|being)\s+\w+\s*", re.I)

#: Third-person possessives, rewritten to address the reader.
_THIRD_PERSON = re.compile(r"\b(?:their|his/her|his or her|his|her)\b", re.I)


@dataclass(frozen=True, slots=True)
class Sentence:
    text: str
    char_start: int
    char_end: int


@dataclass(frozen=True, slots=True)
class ActionCandidate:
    """An action found by rule, with the span that justifies it."""

    description: str
    verb: ActionVerb
    char_start: int
    char_end: int
    confidence: float
    rationale: str
    #: Requirement phrases found in the same sentence.
    requires: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GapCandidate:
    """A vague referent: the document mentions something it never identifies."""

    question: str
    why_it_matters: str
    char_start: int
    char_end: int
    matched_text: str


def split_sentences(text: str) -> list[Sentence]:
    """Split into sentences, keeping each one's offsets in the source."""
    sentences: list[Sentence] = []
    cursor = 0
    for match in _SENTENCE_END.finditer(text):
        end = match.start()
        if _ends_on_abbreviation(text, end):
            continue
        chunk = text[cursor:end]
        if chunk.strip():
            sentences.append(_trimmed(chunk, cursor))
        cursor = match.end()
    tail = text[cursor:]
    if tail.strip():
        sentences.append(_trimmed(tail, cursor))
    return sentences


def extract_actions(text: str) -> list[ActionCandidate]:
    """Find sentences that instruct the reader to do something."""
    found: list[ActionCandidate] = []
    for sentence in split_sentences(text):
        if len(sentence.text) < _MIN_SENTENCE_CHARS:
            continue
        candidate = _action_from(sentence)
        if candidate:
            found.append(candidate)
    return found


def extract_gaps(text: str) -> list[GapCandidate]:
    """Find references the document makes but never resolves.

    Deliberately conservative. A false gap trains the reader to ignore the gap
    panel, which costs more than a missed one.
    """
    found: list[GapCandidate] = []
    claimed: list[tuple[int, int]] = []

    for pattern, question, why in _VAGUE_REFERENTS:
        for match in pattern.finditer(text):
            if any(start <= match.start() < end for start, end in claimed):
                continue
            if _is_resolved_nearby(text, match, pattern):
                continue
            claimed.append((match.start(), match.end()))
            found.append(
                GapCandidate(
                    question=question,
                    why_it_matters=why,
                    char_start=match.start(),
                    char_end=match.end(),
                    matched_text=match.group(0),
                )
            )
    found.sort(key=lambda gap: gap.char_start)
    return found


def _action_from(sentence: Sentence) -> ActionCandidate | None:
    lowered = sentence.text.lower()

    obligation_score = 0.0
    for score, pattern in _OBLIGATION:
        if pattern.search(sentence.text):
            obligation_score = score
            break

    imperative = bool(_IMPERATIVE.match(sentence.text))
    if not obligation_score and not imperative:
        return None

    # Look for the verb the obligation governs, not merely the leftmost verb in
    # the sentence. In "students enrolled in the programme must be verified",
    # "enrolled" describes the subject and "verified" is the instruction.
    scope = lowered
    prefix = _OBLIGATION_PREFIX.match(sentence.text) if obligation_score else None
    if prefix:
        scope = lowered[prefix.end() :]

    verb_match = re.search(rf"\b({_VERB_ALT})\b", scope)
    if not verb_match:
        return None

    verb, base = _VERB_OF[verb_match.group(1)]
    verb_end = (prefix.end() if prefix else 0) + verb_match.end()
    confidence = obligation_score or 0.78
    rationale = (
        "Sentence states an obligation using a modal verb."
        if obligation_score
        else "Sentence is phrased as a direct instruction."
    )

    return ActionCandidate(
        description=_as_instruction(sentence.text, base),
        verb=verb,
        char_start=sentence.char_start,
        char_end=sentence.char_end,
        confidence=confidence,
        rationale=rationale,
        requires=_requirements_in(sentence.text, verb_end),
    )


def _requirements_in(sentence: str, verb_end: int = 0) -> tuple[str, ...]:
    """Pull the things the sentence demands.

    Two shapes. Most notices flag them with a cue -- "along with", "enclosing".
    The rest simply list them as objects of the instruction: "upload your
    resume, a copy of your ID card, and the offer letter". Reading only the
    cued form misses every requirement in the second kind of sentence.
    """
    items: list[str] = []
    for cue in _REQUIREMENT_CUES.finditer(sentence):
        tail = _before_trailing_clause(sentence[cue.end() :])
        for part in re.split(r",|\band\b|\bor\b|/|;", tail):
            cleaned = _clean_requirement(part)
            if cleaned and cleaned.lower() not in {item.lower() for item in items}:
                items.append(cleaned)

    if items:
        return tuple(items[:_MAX_REQUIREMENTS])
    return _series_after_verb(sentence, verb_end)


def _series_after_verb(sentence: str, verb_end: int) -> tuple[str, ...]:
    """Read a comma-separated list of objects following the instruction's verb.

    Requires both a comma and a coordinating conjunction, so ordinary prose
    with one incidental comma is not mistaken for a list of documents.
    """
    tail = _before_trailing_clause(sentence[verb_end:])
    if "," not in tail or not re.search(r"\b(?:and|or)\b", tail):
        return ()

    found: list[str] = []
    for part in re.split(r",|\band\b|\bor\b|;", tail):
        cleaned = _clean_requirement(part)
        if cleaned and cleaned.lower() not in {item.lower() for item in found}:
            found.append(cleaned)
    return tuple(found[:_MAX_REQUIREMENTS]) if len(found) >= 2 else ()


def _before_trailing_clause(tail: str) -> str:
    """Cut at the preposition that ends the list and begins the destination."""
    return re.split(
        r"\b(?:before|by|to\s+the|on\s+or\s+before|through|via|at\s+the)\b",
        tail,
        maxsplit=1,
    )[0]


def _clean_requirement(fragment: str) -> str | None:
    text = re.sub(r"\b(?:their|his|her|the|a|an|its|your)\b", " ", fragment, flags=re.I)
    text = re.sub(r"[^\w\s./-]", " ", text)
    text = " ".join(text.split())
    text = _trim_dangling_participle(text)
    if not 3 <= len(text) <= 60:
        return None
    if not re.search(r"[A-Za-z]{3}", text):
        return None
    return text[:1].upper() + text[1:]


def _trim_dangling_participle(text: str) -> str:
    """Drop a trailing participle left behind when its clause was cut.

    "the offer letter issued by the host organisation" is truncated at "by",
    leaving "offer letter issued". The participle modified the clause that is
    now gone, so it only reads as noise on a checklist.
    """
    words = text.split()
    if len(words) >= 3 and re.fullmatch(r"\w+(?:ed|ing)", words[-1], re.I):
        return " ".join(words[:-1])
    return text


def _as_instruction(sentence: str, base_verb: str) -> str:
    """Rewrite a notice sentence as an imperative the reader can act on.

    The passive case is the one that matters. Notices overwhelmingly say
    "registrations must be completed", and simply deleting the modal leaves
    "completed within 10 days", which is not an instruction and reads as a
    fragment. Restoring the subject as the object -- "complete registrations
    within 10 days" -- keeps the meaning the passive construction carried.
    """
    text = " ".join(sentence.split())
    match = _OBLIGATION_PREFIX.match(text)
    if not match:
        return _tidy(text, fallback=text)

    subject = match.group("subject").strip(" ,;:")
    remainder = text[match.end() :]

    passive = _PASSIVE_HEAD.match(remainder)
    if passive:
        tail = remainder[passive.end() :]
        if subject and len(subject) <= _MAX_SUBJECT_CHARS:
            rebuilt = f"{base_verb} {subject[:1].lower()}{subject[1:]} {tail}"
        else:
            rebuilt = f"{base_verb} {tail}"
        return _tidy(rebuilt, fallback=text)

    return _tidy(remainder, fallback=text)


def _tidy(text: str, *, fallback: str) -> str:
    cleaned = " ".join(text.split()).rstrip(".;:, ")
    if not cleaned:
        return fallback[:_MAX_DESCRIPTION_CHARS]
    # The notice addresses students in the third person; the plan addresses the
    # reader directly, so "submit their form" becomes "submit your form".
    cleaned = _THIRD_PERSON.sub("your", cleaned)
    cleaned = _truncate(cleaned)
    return cleaned[:1].upper() + cleaned[1:]


def _truncate(text: str) -> str:
    """Cut at a word boundary. A mid-phrase cut ("…before 18") reads as data loss."""
    if len(text) <= _MAX_DESCRIPTION_CHARS:
        return text
    clipped = text[:_MAX_DESCRIPTION_CHARS]
    boundary = clipped.rfind(" ")
    if boundary > _MAX_DESCRIPTION_CHARS // 2:
        clipped = clipped[:boundary]
    return clipped.rstrip(".,;: ") + "…"


def _is_resolved_nearby(text: str, match: re.Match[str], pattern: re.Pattern[str]) -> bool:
    """Suppress a gap when the document answers it in the immediate vicinity.

    "the online portal (admissions.example.edu)" is not a gap. Checking a
    window rather than the whole document keeps this cheap and avoids
    suppressing a real gap because an unrelated URL appears on another page.
    """
    window = text[match.end() : match.end() + 90]
    if re.search(r"https?://|www\.|\b[\w.-]+\.(?:edu|gov|org|com|in)\b", window):
        return True
    if re.search(r"\(\s*[^)]{4,}\)", window[:40]):
        return True
    return bool(
        pattern.pattern.startswith(r"\b(?:nominal")
        and re.search(r"(?:rs\.?|inr|₹)\s*\d", window, re.I)
    )


def _ends_on_abbreviation(text: str, end: int) -> bool:
    preceding = text[max(0, end - 12) : end].rstrip(".")
    word = re.split(r"[\s.]", preceding)[-1].lower() if preceding else ""
    if word in _ABBREVIATIONS:
        return True
    # A single capital before the period is an initial, as in "R. K. Sharma".
    return len(word) == 1 and word.isalpha()


def _trimmed(chunk: str, offset: int) -> Sentence:
    leading = len(chunk) - len(chunk.lstrip())
    stripped = chunk.strip()
    return Sentence(
        text=stripped,
        char_start=offset + leading,
        char_end=offset + leading + len(stripped),
    )
