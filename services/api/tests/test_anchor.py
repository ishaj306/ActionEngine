"""Evidence anchoring.

The cases here are drawn from how PDF text extraction actually degrades:
hyphenated line breaks, ligatures, collapsed columns, curly quotes, and models
that quote approximately. A regression in any of these silently turns sourced
claims into unsourced ones.
"""

from __future__ import annotations

import pytest

from app.modules.evidence.anchor import anchor
from app.modules.evidence.normalize import normalize

NOTICE = """NATIONAL MERIT SCHOLARSHIP 2026

Eligible students of the third year must submit the completed applica-
tion form along with their income certificate to the college office
before 18 September 2026.

Late submissions will not be entertained under any circumstances.
"""


def test_exact_quote_resolves_verbatim():
    match = anchor("income certificate to the college office", NOTICE)

    assert match is not None
    assert match.is_verbatim
    assert NOTICE[match.char_start : match.char_end] == (
        "income certificate to the college office"
    )


def test_span_offsets_index_the_raw_document():
    """Offsets must address the source, not a cleaned copy of it."""
    match = anchor("before 18 September 2026", NOTICE)

    assert match is not None
    assert NOTICE[match.char_start : match.char_end] == "before 18 September 2026"


def test_quote_spanning_a_hyphenated_line_break():
    """The model quotes 'application form'; the PDF broke it as 'applica-\\ntion'."""
    match = anchor("the completed application form", NOTICE)

    assert match is not None
    spanned = NOTICE[match.char_start : match.char_end]
    assert "applica-" in spanned
    assert spanned.endswith("tion form")


def test_quote_spanning_a_newline_inside_a_sentence():
    match = anchor("to the college office before 18 September", NOTICE)

    assert match is not None
    assert "\n" in NOTICE[match.char_start : match.char_end]


def test_whitespace_differences_do_not_defeat_matching():
    source = "Applications   must  be\n\n  submitted   by   Friday."
    match = anchor("Applications must be submitted by Friday.", source)

    assert match is not None
    assert match.score > 0.9


def test_typographic_quotes_and_dashes_fold():
    source = "The “designated office” — see clause 4 — accepts forms."
    match = anchor('The "designated office" -- see clause 4', source)

    assert match is not None
    assert match.char_start == 0


def test_approximate_quote_still_anchors_but_scores_below_verbatim():
    """A dropped article is the most common model paraphrase."""
    match = anchor("students of third year must submit completed form", NOTICE)

    assert match is not None
    assert not match.is_verbatim
    assert "third year must submit" in NOTICE[match.char_start : match.char_end]


def test_absent_quote_returns_none_rather_than_nearest_neighbour():
    """The critical negative case: a hallucinated citation must not resolve."""
    assert anchor("applicants must pay a registration fee of Rs. 500", NOTICE) is None


def test_unrelated_quote_of_similar_length_returns_none():
    assert anchor("the quick brown fox jumped over the lazy dog", NOTICE) is None


def test_span_is_tightened_to_the_aligned_region():
    """A match must not drag in surrounding sentences."""
    match = anchor("Late submissions will not be entertained", NOTICE)

    assert match is not None
    spanned = NOTICE[match.char_start : match.char_end]
    assert "SCHOLARSHIP" not in spanned
    assert len(spanned) < 60


def test_rare_token_anchoring_beats_leading_stopword():
    """Quote starts with 'the', which appears many times; must still resolve."""
    source = " ".join(["the office is closed."] * 40) + " the income certificate is required."
    match = anchor("the income certificate is required", source)

    assert match is not None
    assert "income" in source[match.char_start : match.char_end]


@pytest.mark.parametrize("quote", ["", "   ", "\n"])
def test_blank_quotes_are_rejected(quote):
    assert anchor(quote, NOTICE) is None


def test_blank_source_is_rejected():
    assert anchor("anything", "   ") is None


def test_precomputed_normalization_matches_fresh_normalization():
    """The caching path must not change results."""
    cached = normalize(NOTICE)
    quote = "income certificate to the college office"

    fresh = anchor(quote, NOTICE)
    reused = anchor(quote, NOTICE, source_normalized=cached)

    assert fresh is not None and reused is not None
    assert (fresh.char_start, fresh.char_end) == (reused.char_start, reused.char_end)


def test_threshold_is_enforced():
    loose = anchor("students third year submit form", NOTICE, threshold=0.4)
    strict = anchor("students third year submit form", NOTICE, threshold=0.99)

    assert loose is not None
    assert strict is None


def test_repeated_phrase_resolves_to_a_real_occurrence():
    source = "Submit the form. Collect the receipt. Submit the form again."
    match = anchor("Submit the form", source)

    assert match is not None
    assert source[match.char_start : match.char_end].lower().startswith("submit the form")
