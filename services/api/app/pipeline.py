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
from app.modules.extraction import rules
from app.modules.extraction.temporal import (
    TemporalExpression,
    TemporalKind,
    extract_temporal,
)
from app.modules.ingestion.document import ParsedDocument, SourceKind

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
class Analysis:
    """Everything the engine concluded about one document."""

    document_id: str
    title: Claim[str] | None
    deadlines: tuple[Claim[date], ...]
    plan: Plan
    gaps: tuple[InformationGap, ...]
    source_kind: SourceKind
    page_count: int
    needs_ocr: bool
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

    return Analysis(
        document_id=document_id or _fingerprint(text),
        title=_title(document),
        deadlines=deadlines,
        plan=plan,
        gaps=gaps,
        source_kind=document.source_kind,
        page_count=document.page_count,
        needs_ocr=document.needs_ocr,
        duration_ms=round((perf_counter() - started) * 1000, 2),
    )


def _build_actions(
    candidates: list[rules.ActionCandidate],
    temporal: list[TemporalExpression],
    document: ParsedDocument,
) -> list[Action]:
    actions: list[Action] = []
    for index, candidate in enumerate(candidates):
        identifier = f"action-{index + 1}"
        actions.append(
            Action(
                id=identifier,
                description=candidate.description,
                verb=candidate.verb,
                claim=Claim[str](
                    value=candidate.description,
                    classification=_classify(candidate.confidence),
                    confidence=Confidence(
                        score=candidate.confidence,
                        rationale=candidate.rationale,
                    ),
                    evidence=_span(candidate.char_start, candidate.char_end, document),
                ),
                deadline=_deadline_for(candidate, temporal),
                effort_days=_DEFAULT_EFFORT[candidate.verb],
                requires=candidate.requires,
            )
        )
    return _link_dependencies(actions)


def _link_dependencies(actions: list[Action]) -> list[Action]:
    """Infer ordering between actions.

    Two signals, strongest first. An action that names something another action
    produces depends on it directly -- "submit along with the income
    certificate" needs whichever action obtains that certificate. Failing that,
    actions fall back to the coarse stage order of their verbs.

    Only the immediately preceding stage is linked rather than every earlier
    one, so the graph stays a chain instead of becoming dense with redundant
    edges that make the plan unreadable.
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
            previous = [
                other
                for other_stage, ids in by_stage.items()
                if other_stage == stage - 1
                for other in ids
            ]
            dependencies.update(previous)

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

    classification = _classify(expression.confidence)
    if expression.kind is TemporalKind.RELATIVE and classification is ClaimClass.FACT:
        # A relative period is arithmetic over an assumed start date, which is
        # an inference no matter how clearly the period itself is stated.
        classification = ClaimClass.INFERENCE

    return Claim[date](
        value=expression.resolved,
        classification=classification,
        confidence=Confidence(score=expression.confidence, rationale=rationale),
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


def _classify(confidence: float) -> ClaimClass:
    if confidence >= _FACT_THRESHOLD:
        return ClaimClass.FACT
    if confidence >= _UNCERTAIN_THRESHOLD:
        return ClaimClass.INFERENCE
    return ClaimClass.UNCERTAIN


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
