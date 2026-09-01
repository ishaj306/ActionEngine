"""Assemble the stages into one analysis of one document.

Order matters here. Evidence is attached at the point each finding is created,
never bolted on afterwards, because a finding that reaches this module without
a span has already lost the information needed to source it. Dependencies are
inferred after actions exist but before scheduling, since the schedule is a
function of the graph.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from time import perf_counter

from app.domain.claims import (
    Claim,
    ClaimClass,
    Confidence,
    InformationGap,
)
from app.domain.span import EvidenceSpan
from app.modules.action_engine.planner import Action, ActionVerb, Plan, build_plan
from app.modules.extraction import requirements as requirement_rules
from app.modules.extraction import rules
from app.modules.extraction.classify import TYPE_LABEL, classify
from app.modules.extraction.requirements import Requirement
from app.modules.extraction.temporal import (
    TemporalExpression,
    TemporalKind,
    extract_temporal,
)
from app.modules.ingestion.document import ParsedDocument, SourceKind
from app.modules.reasoning.relevance import (
    Assessment,
    Criterion,
    Finding,
    Match,
    Profile,
    Relevance,
    assess,
    extract_criteria,
)

#: Confidence at or above which a temporal expression is stated outright.
_FACT_THRESHOLD = 0.85

#: Confidence below which a finding is an open question, not a conclusion.
_UNCERTAIN_THRESHOLD = 0.6

#: Verb precedence used to infer ordering when no explicit link exists.
#: Confirming eligibility precedes gathering inputs, which precedes preparing
#: them, which precedes handing them over.
_VERB_STAGE: dict[ActionVerb, int] = {
    ActionVerb.CONFIRM: 0,
    ActionVerb.OBTAIN: 1,
    ActionVerb.PREPARE: 2,
    ActionVerb.SUBMIT: 3,
    ActionVerb.ATTEND: 4,
}

#: Default calendar days per verb, used when nothing better is known. Obtaining
#: a document from a third party is the long pole in almost every notice.
_DEFAULT_EFFORT: dict[ActionVerb, int] = {
    ActionVerb.CONFIRM: 1,
    ActionVerb.OBTAIN: 7,
    ActionVerb.PREPARE: 2,
    ActionVerb.SUBMIT: 1,
    ActionVerb.ATTEND: 1,
}

_TITLE_LINE = re.compile(r"^[^\n]{6,120}$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Condition:
    """An eligibility criterion, anchored to the sentence that states it.

    The reasoning module works in offsets and knows nothing about pages or
    documents, the same way every other extraction module here does. Anchoring
    happens once, at this seam, so there is one place where a finding acquires
    its provenance.
    """

    criterion: Criterion
    evidence: EvidenceSpan
    #: The check against the reader's profile. None when none was supplied --
    #: the condition is still worth showing, because it tells the reader what
    #: they would have to know to answer it.
    finding: Finding | None


@dataclass(frozen=True, slots=True)
class Analysis:
    """Everything the engine concluded about one document."""

    document_id: str
    title: Claim[str] | None
    document_type: Claim[str]
    deadlines: tuple[Claim[date], ...]
    plan: Plan
    gaps: tuple[InformationGap, ...]
    requirements: tuple[Requirement, ...]
    #: Eligibility conditions the document states, whether or not a profile was
    #: supplied to check them against.
    conditions: tuple[Condition, ...]
    #: Present only when the caller supplied a profile.
    assessment: Assessment | None
    relevance: Claim[str] | None
    source_kind: SourceKind
    page_count: int
    needs_ocr: bool
    #: Confidence in the characters themselves; below 1.0 when read by OCR.
    text_confidence: float
    ocr_engine: str | None
    duration_ms: float

    @property
    def primary_deadline(self) -> Claim[date] | None:
        """The soonest deadline the engine is willing to stand behind."""
        usable = [
            claim
            for claim in self.deadlines
            if claim.classification is not ClaimClass.UNCERTAIN
        ]
        pool = usable or list(self.deadlines)
        return min(pool, key=lambda claim: claim.value) if pool else None

    @property
    def unresolved_count(self) -> int:
        return len(self.gaps) + sum(
            1
            for item in self.plan.scheduled
            if item.action.claim.classification is ClaimClass.UNCERTAIN
        )


def analyse(
    document: ParsedDocument,
    *,
    today: date,
    document_id: str | None = None,
    completed: frozenset[str] = frozenset(),
    profile: Profile | None = None,
) -> Analysis:
    """Run the full pipeline over an already-parsed document."""
    started = perf_counter()
    text = document.text

    temporal = extract_temporal(text, reference=today)
    deadlines = tuple(
        _deadline_claim(expression, document)
        for expression in temporal
        if expression.is_deadline and expression.resolved
    )

    candidates = rules.extract_actions(text)
    actions = _build_actions(candidates, temporal, document)
    plan = build_plan(actions, today=today, completed=completed)

    gaps = tuple(_gap(candidate, document) for candidate in rules.extract_gaps(text))
    requirements = requirement_rules.group(
        tuple(phrase for action in actions for phrase in action.requires)
    )

    criteria = extract_criteria(text)
    assessment = assess(criteria, profile) if profile is not None else None
    # `assess` returns exactly one finding per criterion, in order, so the two
    # sequences pair positionally.
    findings = assessment.findings if assessment else (None,) * len(criteria)
    conditions = tuple(
        Condition(
            criterion=criterion,
            evidence=_span(criterion.char_start, criterion.char_end, document),
            finding=finding,
        )
        for criterion, finding in zip(criteria, findings, strict=True)
    )

    return Analysis(
        document_id=document_id or _fingerprint(text),
        title=_title(document),
        document_type=_document_type(document),
        deadlines=deadlines,
        plan=plan,
        gaps=gaps,
        requirements=requirements,
        conditions=conditions,
        assessment=assessment,
        relevance=_relevance_claim(assessment, document),
        source_kind=document.source_kind,
        page_count=document.page_count,
        needs_ocr=document.needs_ocr,
        text_confidence=document.text_confidence,
        ocr_engine=document.ocr_engine,
        duration_ms=round((perf_counter() - started) * 1000, 2),
    )


def _document_type(document: ParsedDocument) -> Claim[str]:
    """Classify the document, as a claim like any other."""
    result = classify(document.text)
    confidence = _capped(result.confidence, document)
    span = (
        _span(*result.strongest_signal, document)
        if result.strongest_signal
        else None
    )
    classification = (
        _classify_confidence(confidence) if span else ClaimClass.UNCERTAIN
    )
    # A type is always read off language rather than declared outright, so it
    # is at best an inference even when the signal is unmistakable.
    if classification is ClaimClass.FACT:
        classification = ClaimClass.INFERENCE

    return Claim[str](
        value=TYPE_LABEL[result.document_type],
        classification=classification,
        confidence=Confidence(score=confidence, rationale=result.rationale),
        evidence=span if classification is not ClaimClass.UNCERTAIN else None,
    )


#: What each verdict says to the reader, in the reader's terms.
_RELEVANCE_LABEL: dict[Relevance, str] = {
    Relevance.APPLIES: "This applies to you",
    Relevance.DOES_NOT_APPLY: "This does not appear to apply to you",
    Relevance.UNDETERMINED: "Not enough about you to tell",
    Relevance.NOT_RESTRICTED: "The document never says who it is for",
}


def _relevance_claim(
    assessment: Assessment | None,
    document: ParsedDocument,
) -> Claim[str] | None:
    """Express an eligibility verdict as a claim like any other.

    Two things are load-bearing here. A document that states no conditions
    produces a `MISSING` claim -- the absence of a stated audience is a real
    finding about the document, not a failure to extract one, and it is the
    single most common reason a reader wastes a week on a notice meant for
    somebody else.

    And relevance is never a `FACT`. No document says "you are ineligible"; it
    states a condition, and the engine compares. That comparison is an
    inference however arithmetic it looks, so the classification is capped.
    """
    if assessment is None:
        return None

    confidence = _capped(assessment.confidence, document)
    label = _RELEVANCE_LABEL[assessment.verdict]
    rationale = assessment.rationale + _ocr_note(document)

    if assessment.verdict is Relevance.NOT_RESTRICTED:
        return Claim[str](
            value=label,
            classification=ClaimClass.MISSING,
            confidence=Confidence(score=confidence, rationale=rationale),
        )

    decisive = assessment.conflicts or tuple(
        item for item in assessment.findings if item.match is Match.MATCHES
    )
    anchor = (decisive or assessment.findings)[0].criterion
    span = _span(anchor.char_start, anchor.char_end, document)

    classification = _classify_confidence(confidence)
    if classification is ClaimClass.FACT:
        classification = ClaimClass.INFERENCE

    return Claim[str](
        value=label,
        classification=classification,
        confidence=Confidence(score=confidence, rationale=rationale),
        evidence=span,
    )


def _build_actions(
    candidates: list[rules.ActionCandidate],
    temporal: list[TemporalExpression],
    document: ParsedDocument,
) -> list[Action]:
    actions: list[Action] = []
    for index, candidate in enumerate(candidates):
        identifier = f"action-{index + 1}"
        confidence = _capped(candidate.confidence, document)
        actions.append(
            Action(
                id=identifier,
                description=candidate.description,
                verb=candidate.verb,
                claim=Claim[str](
                    value=candidate.description,
                    classification=_classify_confidence(confidence),
                    confidence=Confidence(
                        score=confidence,
                        rationale=candidate.rationale + _ocr_note(document),
                    ),
                    evidence=_span(candidate.char_start, candidate.char_end, document),
                ),
                deadline=_deadline_for(candidate, temporal),
                effort_days=_DEFAULT_EFFORT[candidate.verb],
                # Collapsed here as well as in the document-wide list, or the
                # step reads "Income certificate · Self-attested copy of
                # previous marksheet · Previous marksheet" while the summary
                # below it correctly says two things. The reader trusts
                # neither once they disagree.
                requires=tuple(
                    item.text for item in requirement_rules.group(candidate.requires)
                ),
            )
        )
    return _link_dependencies(actions)


def _link_dependencies(actions: list[Action]) -> list[Action]:
    """Infer ordering between actions.

    Two signals, strongest first. An action that names something another action
    produces depends on it directly -- "submit along with the income
    certificate" needs whichever action obtains that certificate. Failing that,
    actions fall back to the coarse stage order of their verbs.

    Only the nearest occupied earlier stage is linked, not every earlier one,
    so the graph stays a chain instead of becoming dense with redundant edges
    that make the plan unreadable. Nearest *occupied* matters: obtaining a
    certificate has to block submitting the form even when the document names
    no preparation step in between, and keying off the literal previous stage
    number left exactly that pairing unlinked.
    """
    producers: dict[str, str] = {}
    for action in actions:
        if action.verb is not ActionVerb.OBTAIN:
            continue
        for token in _keywords(action.description):
            producers.setdefault(token, action.id)

    by_stage: dict[int, list[str]] = {}
    for action in actions:
        by_stage.setdefault(_VERB_STAGE[action.verb], []).append(action.id)

    linked: list[Action] = []
    for action in actions:
        dependencies: set[str] = set()

        for requirement in action.requires:
            for token in _keywords(requirement):
                producer = producers.get(token)
                if producer and producer != action.id:
                    dependencies.add(producer)

        if not dependencies:
            stage = _VERB_STAGE[action.verb]
            earlier = [other for other in by_stage if other < stage]
            if earlier:
                dependencies.update(by_stage[max(earlier)])

        linked.append(
            Action(
                id=action.id,
                description=action.description,
                verb=action.verb,
                claim=action.claim,
                deadline=action.deadline,
                effort_days=action.effort_days,
                depends_on=frozenset(dependencies - {action.id}),
                requires=action.requires,
            )
        )
    return linked


def _deadline_for(
    candidate: rules.ActionCandidate,
    temporal: list[TemporalExpression],
) -> date | None:
    """Attach the deadline stated inside the action's own sentence.

    A date elsewhere in the document may well govern this action, but claiming
    so without a syntactic link is the kind of confident guess this engine is
    built to avoid. Cross-sentence attachment is the model's job.
    """
    inside = [
        expression
        for expression in temporal
        if expression.resolved
        and candidate.char_start <= expression.char_start
        and expression.char_end <= candidate.char_end
    ]
    deadlines = [item for item in inside if item.is_deadline]
    pool = deadlines or [item for item in inside if item.kind is TemporalKind.WINDOW]
    if not pool:
        return None
    chosen = min(pool, key=lambda item: item.resolved or date.max)
    if chosen.kind is TemporalKind.WINDOW and chosen.window_end:
        return chosen.window_end
    return chosen.resolved


def _deadline_claim(
    expression: TemporalExpression,
    document: ParsedDocument,
) -> Claim[date]:
    assert expression.resolved is not None
    rationale = expression.rationale
    if expression.is_ambiguous and expression.alternate:
        rationale = f"{rationale} Alternate reading: {expression.alternate.isoformat()}."
    rationale += _ocr_note(document)

    confidence = _capped(expression.confidence, document)
    classification = _classify_confidence(confidence)
    if expression.kind is TemporalKind.RELATIVE and classification is ClaimClass.FACT:
        # A relative period is arithmetic over an assumed start date, which is
        # an inference no matter how clearly the period itself is stated.
        classification = ClaimClass.INFERENCE

    return Claim[date](
        value=expression.resolved,
        classification=classification,
        confidence=Confidence(score=confidence, rationale=rationale),
        evidence=_span(expression.char_start, expression.char_end, document),
    )


def _gap(candidate: rules.GapCandidate, document: ParsedDocument) -> InformationGap:
    return InformationGap(
        question=candidate.question,
        why_it_matters=candidate.why_it_matters,
        suggested_resolution=_suggest(candidate),
        prompted_by=_span(candidate.char_start, candidate.char_end, document),
    )


def _suggest(candidate: rules.GapCandidate) -> str:
    lowered = candidate.question.lower()
    if "office" in lowered:
        return "Ask the issuing department which office accepts the submission."
    if "portal" in lowered or "form" in lowered:
        return "Request the exact link or form from the issuing department."
    if "fee" in lowered:
        return "Confirm the amount and accepted payment methods before you go."
    if "rules" in lowered:
        return "Ask for a copy of the referenced rules or circular."
    return "Contact the issuing department to confirm this detail."


def _classify_confidence(confidence: float) -> ClaimClass:
    if confidence >= _FACT_THRESHOLD:
        return ClaimClass.FACT
    if confidence >= _UNCERTAIN_THRESHOLD:
        return ClaimClass.INFERENCE
    return ClaimClass.UNCERTAIN


def _capped(confidence: float, document: ParsedDocument) -> float:
    """Limit a claim's confidence by how well the text itself was read.

    Certainty about a sentence cannot exceed certainty about its characters.
    When a deadline is extracted from OCR output at 61% mean confidence, the
    extraction rule's own 97% is not the honest number to report.
    """
    return round(min(confidence, document.text_confidence), 4)


def _ocr_note(document: ParsedDocument) -> str:
    if not document.is_ocr_derived:
        return ""
    percent = round(document.text_confidence * 100)
    return (
        f" Read by OCR at {percent}% character confidence, which caps how far "
        "this can be trusted."
    )


def _span(start: int, end: int, document: ParsedDocument) -> EvidenceSpan:
    return EvidenceSpan(
        page=document.page_for_offset(start),
        char_start=start,
        char_end=end,
        text=document.slice(start, end),
        match_score=1.0,
    )


def _title(document: ParsedDocument) -> Claim[str] | None:
    """Take the first substantial line as the document's title.

    Weak by design: it is an inference from layout, and is labelled as one.
    """
    for match in _TITLE_LINE.finditer(document.text[:600]):
        line = match.group(0).strip()
        if len(line) < 6 or not re.search(r"[A-Za-z]{3}", line):
            continue
        shouty = line.isupper()
        return Claim[str](
            value=line,
            classification=ClaimClass.INFERENCE,
            confidence=Confidence(
                score=0.8 if shouty else 0.62,
                rationale=(
                    "First heading-style line of the document."
                    if shouty
                    else "First substantial line; the document has no clear heading."
                ),
            ),
            evidence=_span(match.start(), match.start() + len(line), document),
        )
    return None


def _keywords(text: str) -> set[str]:
    """Content words used to match a requirement against what produces it."""
    stop = {
        "the", "a", "an", "of", "to", "and", "or", "with", "your", "their",
        "along", "copy", "copies", "self", "attested", "previous", "completed",
        "from", "for", "in", "on", "at", "by", "it", "its", "this", "that",
    }
    return {
        word
        for word in re.findall(r"[a-z]{3,}", text.lower())
        if word not in stop
    }


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]
