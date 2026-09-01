"""Anchor a quoted claim back to exact coordinates in the source document.

A language model asked to cite its source returns a *paraphrase-adjacent*
quotation: usually verbatim, often re-spaced, sometimes with an ellipsis or a
dropped article. Trusting it verbatim means most citations fail to resolve;
trusting it loosely means a claim can be "sourced" to text that does not
support it. Neither is acceptable, so matching is fuzzy but scored, and the
score travels with the span so downstream code can demote weak anchors.

Strategy:
  1. Exact substring match on normalized text -- the common case, and cheap.
  2. Otherwise, locate candidate windows by the quote's *rarest* tokens rather
     than its first token, since quotes routinely begin with a stopword that
     occurs thousands of times.
  3. Score each candidate by character-level similarity, then tighten the span
     to the actual aligned region so highlights do not bleed into neighbours.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.modules.evidence.normalize import NormalizedText, normalize

#: Below this similarity a candidate is not considered a citation at all.
DEFAULT_THRESHOLD = 0.72

#: A span at or above this score is treated as verbatim for reporting purposes.
EXACT_ENOUGH = 0.985

#: How many of the quote's rarest tokens to use as search anchors.
_MAX_ANCHOR_TOKENS = 4

#: Cap occurrences examined per anchor token, so a quote made entirely of
#: common words cannot turn matching into a full scan of the document.
_MAX_OCCURRENCES_PER_ANCHOR = 64

#: Candidate windows are widened past the quote length to tolerate text the
#: model dropped from the middle of its citation.
_WINDOW_SLACK = 1.35

_TOKEN_RE = re.compile(r"\w+")


@dataclass(frozen=True, slots=True)
class Token:
    text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class AnchorMatch:
    """Where a quote was found, in source coordinates."""

    char_start: int
    char_end: int
    #: The source text actually spanned, which may differ cosmetically from the
    #: quote that was searched for.
    text: str
    score: float

    @property
    def is_verbatim(self) -> bool:
        return self.score >= EXACT_ENOUGH


def anchor(
    quote: str,
    source: str,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    source_normalized: NormalizedText | None = None,
) -> AnchorMatch | None:
    """Locate `quote` within `source`, or return None if it is not there.

    `source_normalized` may be supplied to avoid re-normalizing a document that
    is being searched repeatedly, which is the usual case -- one document, many
    claims.
    """
    if not quote.strip() or not source.strip():
        return None

    haystack = source_normalized or normalize(source)
    needle = normalize(quote)
    if not needle.text or not haystack.text:
        return None

    exact = haystack.text.find(needle.text)
    if exact != -1:
        start, end = haystack.to_source(exact, exact + len(needle.text))
        return AnchorMatch(start, end, source[start:end], 1.0)

    return _fuzzy(needle, haystack, source, threshold)


def _fuzzy(
    needle: NormalizedText,
    haystack: NormalizedText,
    source: str,
    threshold: float,
) -> AnchorMatch | None:
    source_tokens = _tokenize(haystack.text)
    quote_tokens = _tokenize(needle.text)
    if not source_tokens or not quote_tokens:
        return None

    positions = _index_by_text(source_tokens)
    best: tuple[float, int, int] | None = None
    seen_windows: set[int] = set()
    window_size = max(
        len(needle.text),
        int(len(needle.text) * _WINDOW_SLACK),
    )

    for quote_index, token in _anchor_tokens(quote_tokens, positions):
        for source_index in positions[token.text][:_MAX_OCCURRENCES_PER_ANCHOR]:
            # Align the candidate so the anchor token sits at the same relative
            # position it occupies in the quote.
            offset = source_tokens[source_index].start - (
                token.start - quote_tokens[0].start
            )
            window_start = max(0, offset)
            if window_start in seen_windows:
                continue
            seen_windows.add(window_start)

            window_end = min(len(haystack.text), window_start + window_size)
            window = haystack.text[window_start:window_end]
            if not window:
                continue

            matcher = SequenceMatcher(None, needle.text, window, autojunk=False)
            if matcher.quick_ratio() < threshold:
                continue
            score = matcher.ratio()
            if score < threshold:
                continue

            tight = _tighten(matcher, window_start)
            if tight is None:
                continue
            if best is None or score > best[0]:
                best = (score, tight[0], tight[1])

            _ = quote_index  # anchor ordering is advisory only

    if best is None:
        return None

    score, norm_start, norm_end = best
    start, end = haystack.to_source(norm_start, norm_end)
    return AnchorMatch(start, end, source[start:end], round(score, 4))


def _tighten(matcher: SequenceMatcher, window_start: int) -> tuple[int, int] | None:
    """Trim a candidate window to the region that actually aligned.

    Without this a highlight starts at an arbitrary window boundary and can
    include a sentence the claim has nothing to do with.
    """
    blocks = [block for block in matcher.get_matching_blocks() if block.size > 0]
    if not blocks:
        return None
    first, last = blocks[0], blocks[-1]
    return window_start + first.b, window_start + last.b + last.size


def _anchor_tokens(
    quote_tokens: list[Token],
    positions: dict[str, list[int]],
) -> list[tuple[int, Token]]:
    """Pick the quote tokens most likely to pin down a unique location.

    Rarity in the source beats position in the quote: a quote opening with
    "the applications" is anchored far better by "applications".
    """
    candidates = [
        (index, token)
        for index, token in enumerate(quote_tokens)
        if token.text in positions
    ]
    if not candidates:
        return []
    candidates.sort(key=lambda pair: (len(positions[pair[1].text]), -len(pair[1].text)))
    return candidates[:_MAX_ANCHOR_TOKENS]


def _tokenize(text: str) -> list[Token]:
    return [Token(m.group(), m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]


def _index_by_text(tokens: list[Token]) -> dict[str, list[int]]:
    index: dict[str, list[int]] = defaultdict(list)
    for position, token in enumerate(tokens):
        index[token.text].append(position)
    return index
