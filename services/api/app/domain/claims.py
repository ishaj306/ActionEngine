"""Epistemic primitives.

Every assertion the engine makes about a document carries a classification and a
confidence. Nothing reaches the user as bare text; the UI contract depends on
these two fields existing on every claim.
"""

from __future__ import annotations

from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.domain.span import EvidenceSpan


class ClaimClass(str, Enum):
    """How the engine came to believe a claim.

    Ordered from most to least defensible. `MISSING` is not an absence of a
    claim -- it is a positive assertion that the document fails to state
    something the reader needs, which is distinct from the engine failing to
    find it (that case is `UNCERTAIN`).
    """

    FACT = "FACT"
    INFERENCE = "INFERENCE"
    UNCERTAIN = "UNCERTAIN"
    MISSING = "MISSING"


#: A claim at or above this confidence may drive an irreversible side effect
#: (calendar export, notification). Below it, the UI must require confirmation.
ACTIONABLE_CONFIDENCE = 0.75

#: Claims below this are not shown as conclusions at all, only as open questions.
MIN_REPORTABLE_CONFIDENCE = 0.35


class Confidence(BaseModel):
    """A score plus the reason it landed where it did.

    The rationale is not decorative. When a user asks "why 0.62?", the answer
    has to come from stored state rather than a second model call.
    """

    model_config = ConfigDict(frozen=True)

    score: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=400)

    @property
    def is_actionable(self) -> bool:
        return self.score >= ACTIONABLE_CONFIDENCE

    @property
    def is_reportable(self) -> bool:
        return self.score >= MIN_REPORTABLE_CONFIDENCE


T = TypeVar("T")


class Claim(BaseModel, Generic[T]):
    """A typed value the engine believes, with its provenance attached.

    `evidence` is required for FACT and INFERENCE and forbidden for MISSING.
    That invariant is enforced at construction so an unsourced fact cannot
    physically exist in the system.
    """

    model_config = ConfigDict(frozen=True)

    value: T
    classification: ClaimClass
    confidence: Confidence
    evidence: EvidenceSpan | None = None

    def model_post_init(self, _context: object) -> None:
        requires_evidence = self.classification in (
            ClaimClass.FACT,
            ClaimClass.INFERENCE,
        )
        if requires_evidence and self.evidence is None:
            raise ValueError(
                f"{self.classification.value} claims must cite evidence; "
                "demote to UNCERTAIN if the source span could not be anchored"
            )
        if self.classification is ClaimClass.MISSING and self.evidence is not None:
            raise ValueError(
                "MISSING asserts the document does not state something; "
                "it cannot cite a span that states it"
            )

    def demote(self, reason: str) -> Claim[T]:
        """Return this claim downgraded to UNCERTAIN.

        Used by the verification pass when a claim survives extraction but its
        cited span does not actually entail it.
        """
        return Claim[T](
            value=self.value,
            classification=ClaimClass.UNCERTAIN,
            confidence=Confidence(
                score=min(self.confidence.score, MIN_REPORTABLE_CONFIDENCE),
                rationale=reason,
            ),
            evidence=self.evidence,
        )


class InformationGap(BaseModel):
    """Something the reader needs that the document does not supply.

    Distinct from a failed extraction: a gap means the engine read the document
    correctly and the information genuinely is not there.
    """

    model_config = ConfigDict(frozen=True)

    question: str = Field(min_length=1, max_length=300)
    why_it_matters: str = Field(min_length=1, max_length=400)
    suggested_resolution: str | None = Field(default=None, max_length=300)
    #: The span that *raised* the question (e.g. the vague phrase itself).
    prompted_by: EvidenceSpan | None = None
