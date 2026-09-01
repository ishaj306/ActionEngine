"""Offset-preserving text normalization.

Matching a model's quotation against a PDF fails on cosmetics: the source has a
soft hyphen across a line break, a ligature, a non-breaking space, curly quotes,
or three spaces where the model wrote one. Normalizing both sides fixes that,
but a normalized offset is useless to a UI that highlights the raw text.

So every normalization step records which source character each output
character came from. `NormalizedText.to_source` walks that map backwards.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

#: Characters that behave as whitespace in a PDF but are not `str.isspace()`.
_EXTRA_SPACE = {
    " ",  # no-break space
    " ",  # figure space
    " ",  # narrow no-break space
    "​",  # zero-width space
    "﻿",  # BOM used as ZWNBSP
}

#: Punctuation a typesetter substitutes and a model types plainly.
_FOLD = {
    "‘": "'",
    "’": "'",
    "‚": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "–": "-",  # en dash
    "—": "-",  # em dash
    "−": "-",  # minus sign
    "‐": "-",  # hyphen
    "‑": "-",  # non-breaking hyphen
    "…": "...",
}

#: Hyphens that may indicate a word broken across a line.
_HYPHENS = frozenset("-‐‑­")


@dataclass(frozen=True, slots=True)
class NormalizedText:
    """Normalized text alongside a per-character index back into the source."""

    text: str
    #: `offsets[i]` is the index in the source string that produced `text[i]`.
    offsets: tuple[int, ...]
    source_length: int

    def __post_init__(self) -> None:
        if len(self.text) != len(self.offsets):
            raise ValueError("normalized text and offset map are out of sync")

    def to_source(self, start: int, end: int) -> tuple[int, int]:
        """Map a half-open span in normalized space back to source space.

        The returned span is inclusive of every source character that
        contributed to the normalized range, so it is never narrower than the
        text the caller matched.
        """
        if not 0 <= start < end <= len(self.text):
            raise IndexError(f"span [{start}, {end}) outside normalized text")
        source_start = self.offsets[start]
        source_end = self.offsets[end - 1] + 1
        return source_start, min(source_end, self.source_length)


def normalize(source: str) -> NormalizedText:
    """Casefold, fold punctuation, rejoin hyphenated line breaks, collapse space.

    Whitespace runs become a single space. Leading and trailing whitespace is
    dropped. The result is lowercase.
    """
    chars: list[str] = []
    offsets: list[int] = []
    index = 0
    length = len(source)
    pending_space = False

    while index < length:
        char = source[index]

        if char.isspace() or char in _EXTRA_SPACE:
            # Defer emitting: a run of whitespace collapses to at most one
            # space, and trailing whitespace should not be emitted at all.
            pending_space = bool(chars)
            index += 1
            continue

        if char in _HYPHENS and _is_line_break_hyphen(source, index):
            # "submit-\nted" is one word. Drop the hyphen and the break, and do
            # not let the newline register as a space.
            index = _skip_break_after_hyphen(source, index)
            pending_space = False
            continue

        if pending_space:
            chars.append(" ")
            offsets.append(index)
            pending_space = False

        replacement = _FOLD.get(char)
        if replacement is None:
            decomposed = unicodedata.normalize("NFKC", char).casefold()
            replacement = decomposed or char

        for out in replacement:
            chars.append(out)
            offsets.append(index)
        index += 1

    return NormalizedText(
        text="".join(chars),
        offsets=tuple(offsets),
        source_length=length,
    )


def _is_line_break_hyphen(source: str, index: int) -> bool:
    """True when the hyphen at `index` is a soft break rather than a real one.

    A soft break is a hyphen followed by a newline and then a letter. A real
    hyphen ("part-time") has no newline, and a dash used as a bullet has no
    letter before it.
    """
    if source[index] == "­":  # soft hyphen is unambiguous
        return True
    if index == 0 or not source[index - 1].isalpha():
        return False
    cursor = index + 1
    saw_newline = False
    while cursor < len(source) and source[cursor].isspace():
        if source[cursor] in "\r\n":
            saw_newline = True
        cursor += 1
    return saw_newline and cursor < len(source) and source[cursor].isalpha()


def _skip_break_after_hyphen(source: str, index: int) -> int:
    """Return the index of the first character after a soft line break."""
    cursor = index + 1
    while cursor < len(source) and source[cursor].isspace():
        cursor += 1
    return cursor
