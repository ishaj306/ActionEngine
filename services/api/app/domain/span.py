"""Source locations.

Offsets are always into the *raw* extracted text of a document, never into a
normalized or cleaned variant. The UI highlights against the raw text, so any
normalization used during matching has to be mapped back before it gets here.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BoundingBox(BaseModel):
    """Page-space rectangle, PDF coordinate order (origin top-left, points)."""

    model_config = ConfigDict(frozen=True)

    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode="after")
    def _ordered(self) -> BoundingBox:
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("bounding box corners are transposed")
        return self


class EvidenceSpan(BaseModel):
    """A anchored region of source text backing a single claim."""

    model_config = ConfigDict(frozen=True)

    page: int = Field(ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    #: Verbatim slice of the raw document text. Stored rather than re-sliced so
    #: the UI can render evidence without re-fetching the source file.
    text: str
    #: 1.0 for an exact match; lower when the span was recovered fuzzily.
    match_score: float = Field(default=1.0, ge=0.0, le=1.0)
    boxes: tuple[BoundingBox, ...] = ()

    @model_validator(mode="after")
    def _non_empty(self) -> EvidenceSpan:
        if self.char_end <= self.char_start:
            raise ValueError("span must cover at least one character")
        return self

    @property
    def length(self) -> int:
        return self.char_end - self.char_start

    def excerpt(self, limit: int = 160) -> str:
        """Single-line preview for compact UI surfaces."""
        flat = " ".join(self.text.split())
        if len(flat) <= limit:
            return flat
        return flat[: limit - 1].rstrip() + "…"
