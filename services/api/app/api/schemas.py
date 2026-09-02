"""Wire format.

Kept separate from the domain types so the API contract can stay stable while
internals move, and so the serializer is the single place that guarantees the
product's central invariant: nothing crosses this boundary without its
classification, its confidence, and -- where one exists -- its source span.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from app.comparison.changes import Change, Comparison
from app.comparison.crossdoc import Conflict, Portfolio, ScheduledItem
from app.domain.claims import Claim, ClaimClass, InformationGap
from app.domain.span import EvidenceSpan
from app.modules.action_engine.planner import ScheduledAction
from app.modules.extraction.requirements import Requirement
from app.modules.reasoning.relevance import Profile
from app.pipeline import Analysis, Condition


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


class RequirementOut(BaseModel):
    text: str
    kind: str

    @classmethod
    def of(cls, requirement: Requirement) -> RequirementOut:
        return cls(text=requirement.text, kind=requirement.kind.value)


class ConditionOut(BaseModel):
    """An eligibility condition, and the verdict on it if one was reached."""

    attribute: str
    requirement: str
    #: "matches", "conflicts", "unknown", or null when no profile was supplied.
    match: str | None
    #: What the reader said about themselves, rendered for display.
    profile_value: str | None
    #: Why the check landed where it did. Empty when nothing was checked.
    explanation: str
    evidence: EvidenceOut

    @classmethod
    def of(cls, condition: Condition) -> ConditionOut:
        finding = condition.finding
        return cls(
            attribute=condition.criterion.attribute.value,
            requirement=condition.criterion.requirement,
            match=finding.match.value if finding else None,
            profile_value=finding.profile_value if finding else None,
            explanation=finding.explanation if finding else "",
            evidence=EvidenceOut.of(condition.evidence),
        )


class ProfileIn(BaseModel):
    """Self-declared attributes, all optional.

    Nothing here is stored beyond the process, and nothing is inferred from a
    document. A reader who fills in one field gets one condition checked and is
    told the rest are open, which is a better trade than an all-or-nothing form.
    """

    year: int | None = Field(default=None, ge=1, le=10)
    programme: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=40)
    domicile: str | None = Field(default=None, max_length=80)
    score: float | None = Field(default=None, ge=0, le=100)
    cgpa: float | None = Field(default=None, ge=0, le=10)
    age: int | None = Field(default=None, ge=10, le=120)

    def to_domain(self) -> Profile:
        return Profile(
            year=self.year,
            programme=self.programme,
            category=self.category,
            domicile=self.domicile,
            score=self.score,
            cgpa=self.cgpa,
            age=self.age,
        )


class PlanPatch(BaseModel):
    """Working state the reader owns, rather than the document.

    Both fields are tri-state on purpose: absent means "leave as it is",
    present means "replace with this", and an explicit null on `profile` means
    "forget what I told you", which a reader must always be able to do.
    """

    completed: list[str] | None = None
    profile: ProfileIn | None = None
    clear_profile: bool = False


class AnalysisOut(BaseModel):
    document_id: str
    filename: str
    title: ClaimOut | None
    document_type: ClaimOut
    primary_deadline: ClaimOut | None
    deadlines: list[ClaimOut]
    actions: list[ActionOut]
    gaps: list[GapOut]
    requirements: list[RequirementOut]
    #: Eligibility conditions the document states, checked where possible.
    conditions: list[ConditionOut]
    #: The verdict on whether the document applies to this reader. Null when no
    #: profile was supplied; MISSING when the document never says who it is for.
    relevance: ClaimOut | None
    #: "applies", "does_not_apply", "undetermined", "not_restricted", or null.
    relevance_verdict: str | None
    #: Action ids the reader has ticked off.
    completed: list[str]
    #: False when a prerequisite cannot finish in time for what depends on it.
    is_feasible: bool
    unresolved_count: int
    page_count: int
    source_kind: str
    needs_ocr: bool
    #: Confidence in the characters themselves; below 1.0 when read by OCR.
    text_confidence: float
    ocr_engine: str | None
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
            document_type=ClaimOut.of(analysis.document_type),
            primary_deadline=(
                ClaimOut.of(analysis.primary_deadline)
                if analysis.primary_deadline
                else None
            ),
            deadlines=[ClaimOut.of(claim) for claim in analysis.deadlines],
            actions=[ActionOut.of(item) for item in analysis.plan.scheduled],
            gaps=[GapOut.of(gap) for gap in analysis.gaps],
            requirements=[RequirementOut.of(item) for item in analysis.requirements],
            conditions=[ConditionOut.of(item) for item in analysis.conditions],
            relevance=ClaimOut.of(analysis.relevance) if analysis.relevance else None,
            relevance_verdict=(
                analysis.assessment.verdict.value if analysis.assessment else None
            ),
            completed=sorted(analysis.plan.completed),
            is_feasible=analysis.plan.is_feasible,
            unresolved_count=analysis.unresolved_count,
            page_count=analysis.page_count,
            source_kind=analysis.source_kind.value,
            needs_ocr=analysis.needs_ocr,
            text_confidence=analysis.text_confidence,
            ocr_engine=analysis.ocr_engine,
            broken_cycles=[list(pair) for pair in analysis.plan.broken_cycles],
            duration_ms=analysis.duration_ms,
            text=text,
        )


class ChangeOut(BaseModel):
    kind: str
    severity: str
    summary: str
    before: str | None
    after: str | None

    @classmethod
    def of(cls, change: Change) -> ChangeOut:
        return cls(
            kind=change.kind.value,
            severity=change.severity.value,
            summary=change.summary,
            before=change.before,
            after=change.after,
        )


class ComparisonOut(BaseModel):
    """What changed between two versions of a document."""

    previous_document_id: str
    current_document_id: str
    changes: list[ChangeOut]
    #: One line fit for a banner.
    headline: str
    #: Shared vocabulary, 0 to 1. Low means these are probably not two versions
    #: of the same document, and `warning` will say so.
    relatedness: float
    warning: str | None

    @classmethod
    def of(
        cls,
        comparison: Comparison,
        *,
        previous_document_id: str,
        current_document_id: str,
    ) -> ComparisonOut:
        return cls(
            previous_document_id=previous_document_id,
            current_document_id=current_document_id,
            changes=[ChangeOut.of(item) for item in comparison.changes],
            headline=comparison.headline,
            relatedness=comparison.relatedness,
            warning=comparison.warning,
        )


class ConflictOut(BaseModel):
    kind: str
    summary: str
    #: One entry per document: its id, its name, and the value it states.
    positions: list[list[str]]
    relatedness: float
    resolution: str

    @classmethod
    def of(cls, conflict: Conflict) -> ConflictOut:
        return cls(
            kind=conflict.kind.value,
            summary=conflict.summary,
            positions=[list(item) for item in conflict.positions],
            relatedness=conflict.relatedness,
            resolution=conflict.resolution,
        )


class TimelineItemOut(BaseModel):
    document_id: str
    document_name: str
    action: ActionOut

    @classmethod
    def of(cls, item: ScheduledItem) -> TimelineItemOut:
        return cls(
            document_id=item.document_id,
            document_name=item.document_name,
            action=ActionOut.of(item.action),
        )


class PortfolioRequest(BaseModel):
    document_ids: list[str] = Field(min_length=2, max_length=12)


class PortfolioOut(BaseModel):
    """Several documents read as one body of instructions."""

    timeline: list[TimelineItemOut]
    #: Steps with no governing date, listed rather than dropped.
    undated: list[TimelineItemOut]
    conflicts: list[ConflictOut]
    is_consistent: bool
    #: The soonest date anything must be finished. Often earlier than any date
    #: stated anywhere, because prerequisites inherit their dependents'.
    next_due: date | None
    #: The soonest deadline a document actually states.
    next_stated_deadline: date | None

    @classmethod
    def of(cls, portfolio: Portfolio) -> PortfolioOut:
        return cls(
            timeline=[TimelineItemOut.of(item) for item in portfolio.timeline],
            undated=[TimelineItemOut.of(item) for item in portfolio.undated],
            conflicts=[ConflictOut.of(item) for item in portfolio.conflicts],
            is_consistent=portfolio.is_consistent,
            next_due=portfolio.next_due,
            next_stated_deadline=portfolio.next_stated_deadline,
        )


class ErrorOut(BaseModel):
    detail: str
    #: What the user can do about it. An error without a next step is a dead end.
    remedy: str | None = None
