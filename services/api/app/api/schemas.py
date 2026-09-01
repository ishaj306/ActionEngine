"""Wire format.

Kept separate from the domain types so the API contract can stay stable while
internals move, and so the serializer is the single place that guarantees the
product's central invariant: nothing crosses this boundary without its
classification, its confidence, and -- where one exists -- its source span.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from app.domain.claims import Claim, ClaimClass, InformationGap
from app.domain.span import EvidenceSpan
from app.modules.action_engine.planner import ScheduledAction
from app.pipeline import Analysis


class EvidenceOut(BaseModel):
    page: int
    char_start: int
    char_end: int
    text: str
    excerpt: str
    match_score: float

    @classmethod
    def of(cls, span: EvidenceSpan) -> EvidenceOut:
        return cls(
            page=span.page,
            char_start=span.char_start,
            char_end=span.char_end,
            text=span.text,
            excerpt=span.excerpt(),
            match_score=span.match_score,
        )


class ClaimOut(BaseModel):
    """A value the engine believes, with why it believes it."""

    value: str
    classification: ClaimClass
    confidence: float
    rationale: str
    evidence: EvidenceOut | None

    @classmethod
    def of(cls, claim: Claim) -> ClaimOut:
        return cls(
            value=str(claim.value),
            classification=claim.classification,
            confidence=claim.confidence.score,
            rationale=claim.confidence.rationale,
            evidence=EvidenceOut.of(claim.evidence) if claim.evidence else None,
        )


class ActionOut(BaseModel):
    id: str
    order: int
    description: str
    verb: str
    priority: str
    depth: int
    requires: list[str]
    blocked_by: list[str]
    deadline: date | None = Field(description="Deadline stated for this action itself")
    effective_deadline: date | None = Field(
        description="Deadline after inheriting constraints from dependent actions"
    )
    latest_start: date | None
    slack_days: int | None
    effort_days: int
    rationale: str
    claim: ClaimOut

    @classmethod
    def of(cls, item: ScheduledAction) -> ActionOut:
        return cls(
            id=item.action.id,
            order=item.order,
            description=item.action.description,
            verb=item.action.verb.value,
            priority=item.priority.value,
            depth=item.depth,
            requires=list(item.action.requires),
            blocked_by=list(item.blocked_by),
            deadline=item.action.deadline,
            effective_deadline=item.effective_deadline,
            latest_start=item.latest_start,
            slack_days=item.slack_days,
            effort_days=item.action.effort_days,
            rationale=item.rationale,
            claim=ClaimOut.of(item.action.claim),
        )


class GapOut(BaseModel):
    question: str
    why_it_matters: str
    suggested_resolution: str | None
    evidence: EvidenceOut | None

    @classmethod
    def of(cls, gap: InformationGap) -> GapOut:
        return cls(
            question=gap.question,
            why_it_matters=gap.why_it_matters,
            suggested_resolution=gap.suggested_resolution,
            evidence=EvidenceOut.of(gap.prompted_by) if gap.prompted_by else None,
        )


class AnalysisOut(BaseModel):
    document_id: str
    filename: str
    title: ClaimOut | None
    primary_deadline: ClaimOut | None
    deadlines: list[ClaimOut]
    actions: list[ActionOut]
    gaps: list[GapOut]
    #: False when a prerequisite cannot finish in time for what depends on it.
    is_feasible: bool
    unresolved_count: int
    page_count: int
    source_kind: str
    needs_ocr: bool
    #: Ordering contradictions the extractor produced, surfaced not hidden.
    broken_cycles: list[list[str]]
    duration_ms: float
    #: Full document text, so the client can render evidence highlights without
    #: a second round trip. Documents here are notices, not books.
    text: str

    @classmethod
    def of(cls, analysis: Analysis, *, filename: str, text: str) -> AnalysisOut:
        return cls(
            document_id=analysis.document_id,
            filename=filename,
            title=ClaimOut.of(analysis.title) if analysis.title else None,
            primary_deadline=(
                ClaimOut.of(analysis.primary_deadline)
                if analysis.primary_deadline
                else None
            ),
            deadlines=[ClaimOut.of(claim) for claim in analysis.deadlines],
            actions=[ActionOut.of(item) for item in analysis.plan.scheduled],
            gaps=[GapOut.of(gap) for gap in analysis.gaps],
            is_feasible=analysis.plan.is_feasible,
            unresolved_count=analysis.unresolved_count,
            page_count=analysis.page_count,
            source_kind=analysis.source_kind.value,
            needs_ocr=analysis.needs_ocr,
            broken_cycles=[list(pair) for pair in analysis.plan.broken_cycles],
            duration_ms=analysis.duration_ms,
            text=text,
        )


class ErrorOut(BaseModel):
    detail: str
    #: What the user can do about it. An error without a next step is a dead end.
    remedy: str | None = None
