"""Temporal extraction.

The behaviours that matter are less about parsing breadth than about honesty:
a date the document does not fully specify must come back with reduced
confidence and a rationale saying why, not a confident guess.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.modules.extraction.temporal import (
    TemporalKind,
    extract_temporal,
)

REFERENCE = date(2026, 9, 1)


def find(text: str, *, reference: date = REFERENCE):
    return extract_temporal(text, reference=reference)


def only(text: str, *, reference: date = REFERENCE):
    results = find(text, reference=reference)
    assert len(results) == 1, f"expected one expression, got {results}"
    return results[0]


class TestAbsoluteDates:
    @pytest.mark.parametrize(
        "phrase",
        [
            "18 September 2026",
            "18th September 2026",
            "18 Sept, 2026",
            "September 18, 2026",
            "Sep 18 2026",
        ],
    )
    def test_fully_specified_dates_resolve_with_high_confidence(self, phrase):
        found = only(f"Submit before {phrase}.")

        assert found.resolved == date(2026, 9, 18)
        assert found.confidence > 0.95
        assert not found.is_ambiguous

    def test_missing_year_resolves_forward_and_lowers_confidence(self):
        found = only("Applications close on 18 September.")

        assert found.resolved == date(2026, 9, 18)
        assert found.confidence < 0.8
        assert "no year is stated" in found.rationale.lower()

    def test_missing_year_rolls_into_the_next_year_when_already_past(self):
        found = only("Submit by 18 March.", reference=date(2026, 9, 1))

        assert found.resolved == date(2027, 3, 18)

    def test_impossible_dates_are_not_reported(self):
        assert find("Reference 45 September in the register.") == []


class TestNumericAmbiguity:
    def test_ambiguous_numeric_date_reports_both_readings(self):
        found = only("The last date is 03/04/2026.")

        assert found.is_ambiguous
        assert found.resolved == date(2026, 4, 3)
        assert found.alternate == date(2026, 3, 4)
        assert found.confidence < 0.5
        assert "confirm" in found.rationale.lower()

    def test_unambiguous_numeric_date_resolves_confidently(self):
        found = only("The last date is 18/09/2026.")

        assert found.resolved == date(2026, 9, 18)
        assert not found.is_ambiguous
        assert found.confidence > 0.8

    def test_two_digit_year_expands_to_current_century(self):
        found = only("Dated 18/09/26.")

        assert found.resolved == date(2026, 9, 18)


class TestRelativeDates:
    def test_calendar_days_count_from_the_reference_date(self):
        found = only("Respond within 7 days of receiving this notice.")

        assert found.kind is TemporalKind.RELATIVE
        assert found.resolved == date(2026, 9, 8)
        assert found.is_deadline

    def test_working_days_skip_weekends(self):
        # 2026-09-01 is a Tuesday; five working days lands the following Tuesday.
        found = only("Reply within 5 working days.")

        assert found.resolved == date(2026, 9, 8)

    def test_spelled_out_counts_are_understood(self):
        found = only("Submit within seven days.")

        assert found.resolved == date(2026, 9, 8)

    def test_relative_dates_carry_low_confidence_and_explain_why(self):
        found = only("Respond within 7 days.")

        assert found.confidence < 0.7
        assert "does not state" in found.rationale.lower()


class TestWindows:
    def test_application_window_captures_both_ends(self):
        found = only("Applications open from 1 September to 20 September 2026.")

        assert found.kind is TemporalKind.WINDOW
        assert found.resolved == date(2026, 9, 1)
        assert found.window_end == date(2026, 9, 20)

    def test_between_and_is_equivalent_to_from_to(self):
        found = only("Apply between 1 September and 20 September 2026.")

        assert found.kind is TemporalKind.WINDOW

    def test_window_suppresses_its_own_endpoints_as_separate_dates(self):
        results = find("Applications open from 1 September to 20 September 2026.")

        assert len(results) == 1

    def test_reversed_window_is_rejected(self):
        results = find("Open from 20 September 2026 to 1 September 2026.")

        assert all(item.kind is not TemporalKind.WINDOW for item in results)


class TestDeadlineCues:
    @pytest.mark.parametrize(
        "sentence",
        [
            "Submit before 18 September 2026.",
            "Forms are due by 18 September 2026.",
            "The last date is 18 September 2026.",
            "Apply no later than 18 September 2026.",
        ],
    )
    def test_cue_words_mark_a_date_as_a_deadline(self, sentence):
        assert only(sentence).is_deadline

    def test_a_date_without_a_cue_is_not_a_deadline(self):
        found = only("The orientation was held on 18 September 2026.")

        assert not found.is_deadline


class TestSpans:
    def test_offsets_locate_the_phrase_in_the_source(self):
        text = "Eligible students must apply before 18 September 2026 at the office."
        found = only(text)

        assert text[found.char_start : found.char_end] == "18 September 2026"

    def test_multiple_dates_are_returned_in_document_order(self):
        text = (
            "Registration closes 5 September 2026. "
            "The examination is on 20 October 2026."
        )
        results = find(text)

        assert [item.resolved for item in results] == [
            date(2026, 9, 5),
            date(2026, 10, 20),
        ]


def test_prose_without_dates_yields_nothing():
    assert find("Students are advised to read the instructions carefully.") == []
