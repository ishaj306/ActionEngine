"""Optical character recognition, behind a swappable engine.

Two requirements shape this module.

The engine is a protocol because OCR is the part of the pipeline most likely to
be replaced -- local Tesseract for privacy, a cloud service for accuracy on bad
scans -- and because the binary may simply not be installed. An engine that is
unavailable must say so. Returning empty text would present a scan the system
could not read as a document containing nothing, which is the exact failure the
rest of this codebase is built to prevent.

OCR output is also uncertain in a way native text is not. The engine reports a
mean character confidence, and the pipeline uses it to cap the confidence of
every claim derived from that text: a deadline read at 61% cannot be a 97% FACT.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: Mean confidence below which OCR output is too unreliable to extract from.
UNUSABLE_CONFIDENCE = 0.45


class OcrUnavailable(Exception):
    """Raised when OCR is needed but no engine can run."""


@dataclass(frozen=True, slots=True)
class OcrResult:
    text: str
    #: Mean per-word confidence in [0, 1]. Propagated into every downstream claim.
    confidence: float
    engine: str

    @property
    def is_usable(self) -> bool:
        return bool(self.text.strip()) and self.confidence >= UNUSABLE_CONFIDENCE


@runtime_checkable
class OcrEngine(Protocol):
    """Reads text out of a single page image."""

    name: str

    def is_available(self) -> bool:
        """True when this engine can actually run right now."""
        ...

    def read(self, image: bytes) -> OcrResult:
        """Extract text from encoded image bytes (PNG, JPEG, TIFF)."""
        ...


class TesseractEngine:
    """Local Tesseract via pytesseract.

    Availability is checked against the binary on PATH, not merely the Python
    wrapper: pytesseract imports fine without Tesseract installed and only
    fails at call time.
    """

    name = "tesseract"

    def __init__(self, language: str = "eng") -> None:
        self._language = language

    def is_available(self) -> bool:
        if shutil.which("tesseract") is None:
            return False
        try:
            import pytesseract  # noqa: F401
        except ImportError:
            return False
        return True

    def read(self, image: bytes) -> OcrResult:
        if not self.is_available():
            raise OcrUnavailable(
                "The Tesseract binary is not installed or not on PATH."
            )

        import io

        import pytesseract
        from PIL import Image

        try:
            with Image.open(io.BytesIO(image)) as handle:
                page = handle.convert("L")
                data = pytesseract.image_to_data(
                    page,
                    lang=self._language,
                    output_type=pytesseract.Output.DICT,
                )
        except Exception as exc:  # pragma: no cover - depends on local binary
            raise OcrUnavailable(f"Tesseract failed to read the page: {exc}") from exc

        return OcrResult(
            text=_join_words(data),
            confidence=_mean_confidence(data),
            engine=self.name,
        )


class NullEngine:
    """Stands in when no OCR is configured, and refuses rather than guessing."""

    name = "none"

    def is_available(self) -> bool:
        return False

    def read(self, image: bytes) -> OcrResult:
        _ = image
        raise OcrUnavailable("No OCR engine is configured.")


def default_engine() -> OcrEngine:
    """Pick the best engine available in this environment."""
    tesseract = TesseractEngine()
    if tesseract.is_available():
        return tesseract
    logger.info("OCR unavailable: Tesseract is not installed; scans cannot be read.")
    return NullEngine()


def _join_words(data: dict[str, list]) -> str:
    """Rebuild text from Tesseract's word boxes, preserving line breaks.

    `image_to_string` would be simpler, but the per-word confidences only come
    from `image_to_data`, and those confidences are the point.
    """
    words: list[str] = data.get("text", [])
    lines: list[str] = []
    current: list[str] = []
    last_key: tuple[int, int, int] | None = None

    for index, word in enumerate(words):
        if not word.strip():
            continue
        key = (
            data["block_num"][index],
            data["par_num"][index],
            data["line_num"][index],
        )
        if last_key is not None and key != last_key:
            lines.append(" ".join(current))
            current = []
        current.append(word)
        last_key = key

    if current:
        lines.append(" ".join(current))
    return "\n".join(lines)


def _mean_confidence(data: dict[str, list]) -> float:
    """Mean confidence over recognised words.

    Tesseract reports -1 for boxes it did not resolve to a word; including
    those would drag the mean toward zero and misreport a good scan as bad.
    """
    scores = [
        float(value)
        for value, word in zip(data.get("conf", []), data.get("text", []))
        if word.strip() and float(value) >= 0
    ]
    if not scores:
        return 0.0
    return round(sum(scores) / len(scores) / 100.0, 4)
