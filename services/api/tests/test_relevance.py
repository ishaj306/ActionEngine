"""Eligibility matching.

The behaviour under test is mostly restraint. Telling someone a scholarship is
not for them is the most consequential thing this system says, so the tests
here spend most of their attention on when that must *not* happen.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from app.modules.reasoning.relevance import (
    Attribute,
    Comparator,
    Match,
    Profile,
    Relevance,
    assess,
    extract_criteria,
)

NOTICE = """NATIONAL MERIT SCHOLARSHIP 2026

Eligible third year students of the Computer Science department with a minimum
of 60% aggregate may apply. Applicants must be under 25 years of age.

OBC category candidates are eligible for a fee waiver.
"""


def check(text: str, profile: Profile):
    return assess(extract_criteria(text), profile)


class TestCriterionExtraction:
    @pytest.fixture
    def criteria(self):
        return extract_criteria(NOTICE)

    def test_year_of_study_is_read(self, criteria):
        year = next(item for item in criteria if item.attribute is Attribute.YEAR)

        assert year.value == 3
        assert year.comparator is Comparator.EQUALS

    def test_aggregate_marks_are_read(self, criteria):
        score = next(item for item in criteria if item.attribute is Attribute.SCORE)

        assert score.value == 60
        assert score.comparator is Comparator.AT_LEAST

    def test_an_age_ceiling_is_read(self, criteria):
        age = next(item for item in criteria if item.attribute is Attribute.AGE)

        assert age.value == 25
        assert age.comparator is Comparator.AT_MOST

    def test_a_reservation_category_is_read(self, criteria):
        category = next(item for item in criteria if item.attribute is Attribute.CATEGORY)

        assert category.value == ("OBC",)

    def test_a_department_is_read(self, criteria):
        programme = next(item for item in criteria if item.attribute is Attribute.PROGRAMME)

        assert "Computer Science" in programme.requirement

    def test_spans_locate_the_stated_condition(self, criteria):
        for item in criteria:
            assert NOTICE[item.char_start : item.char_end] == item.matched_text

    def test_criteria_are_returned_in_document_order(self, criteria):
        offsets = [item.char_start for item in criteria]

        assert offsets == sorted(offsets)

    def test_each_condition_is_reported_once(self, criteria):
        for attribute in {item.attribute for item in criteria}:
            spans = [
                (item.char_start, item.char_end)
                for item in criteria
                if item.attribute is attribute
            ]
            for left, right in pairwise(spans):
                assert left[1] <= right[0]

    def test_a_final_year_restriction_is_kept_but_not_evaluated(self):
        """"Final year" is a real condition whose number depends on the course."""
        criteria = extract_criteria("Open only to final year students.")
        year = next(item for item in criteria if item.attribute is Attribute.YEAR)

        assert year.comparator is Comparator.NOT_COMPARABLE
        assert year.value is None

    def test_years_of_experience_are_not_an_age(self):
        """A job description asking for experience must not set an age floor."""
        criteria = extract_criteria("Candidates need at least 3 years of experience.")

        assert not any(item.attribute is Attribute.AGE for item in criteria)

    def test_a_degree_named_outside_an_eligibility_sentence_is_not_a_condition(self):
        """Notices mention their own audience in passing all the time."""
        criteria = extract_criteria("The MBA orientation will be held in Hall 2.")

        assert not any(item.attribute is Attribute.PROGRAMME for item in criteria)

    def test_a_degree_named_as_a_restriction_is_a_condition(self):
        criteria = extract_criteria("Only MBA students are eligible to apply.")

        assert any(item.attribute is Attribute.PROGRAMME for item in criteria)

    def test_prose_without_conditions_yields_nothing(self):
        assert extract_criteria("The trust was founded in 1998.") == ()


class TestAssessment:
    def test_a_matching_profile_applies(self):
        result = check(
            NOTICE,
            Profile(year=3, programme="Computer Science", score=72, age=21, category="OBC"),
        )

        assert result.verdict is Relevance.APPLIES
        assert result.confidence >= 0.85

    def test_a_failing_number_does_not_apply(self):
        result = check(NOTICE, Profile(year=3, score=48))

        assert result.verdict is Relevance.DOES_NOT_APPLY
        assert any("60" in item.explanation for item in result.conflicts)

    def test_the_conflict_names_both_sides(self):
        result = check(NOTICE, Profile(score=48))
        conflict = result.conflicts[0]

        assert conflict.profile_value == "48"
        assert "60" in conflict.criterion.requirement

    def test_a_wrong_year_does_not_apply(self):
        assert check(NOTICE, Profile(year=1)).verdict is Relevance.DOES_NOT_APPLY

    def test_an_age_over_the_ceiling_does_not_apply(self):
        assert check(NOTICE, Profile(age=31)).verdict is Relevance.DOES_NOT_APPLY

    def test_a_closed_vocabulary_mismatch_conflicts(self):
        """SC / ST / OBC is a fixed set, so a mismatch really is a mismatch."""
        result = check("OBC category candidates may apply.", Profile(category="SC"))

        assert result.verdict is Relevance.DOES_NOT_APPLY

    def test_an_open_vocabulary_mismatch_does_not_conflict(self):
        """"CSE" and "Computer Science" name one department, not two.

        Reporting that as ineligible would be the single most damaging thing
        this system could get wrong, so an unmatched free-text value is left
        open for the reader instead.
        """
        result = check(
            "Students of the Computer Science department may apply.",
            Profile(programme="CSE"),
        )

        assert result.verdict is not Relevance.DOES_NOT_APPLY
        assert result.unresolved
        assert "CSE" in result.unresolved[0].explanation

    def test_an_open_vocabulary_match_is_found_through_filler(self):
        result = check(
            "Students of the Computer Science department may apply.",
            Profile(programme="Computer Science Engineering"),
        )

        assert result.findings[0].match is Match.MATCHES

    def test_an_empty_profile_is_undetermined_not_eligible(self):
        result = check(NOTICE, Profile())

        assert result.verdict is Relevance.UNDETERMINED
        assert result.confidence < 0.5

    def test_a_partial_profile_reports_what_it_could_not_check(self):
        result = check(NOTICE, Profile(score=72))

        assert result.verdict is Relevance.APPLIES
        assert result.confidence < 0.85
        assert "could not be checked" in result.rationale

    def test_a_document_with_no_conditions_is_not_restricted(self):
        result = check("Submit the form before 18 September 2026.", Profile(year=3))

        assert result.verdict is Relevance.NOT_RESTRICTED
        assert result.findings == ()

    def test_every_criterion_produces_exactly_one_finding(self):
        criteria = extract_criteria(NOTICE)
        result = assess(criteria, Profile(year=3))

        assert len(result.findings) == len(criteria)

    def test_an_uncheckable_criterion_explains_why(self):
        result = check("Open only to final year students.", Profile(year=3))

        assert result.unresolved
        assert "programme" in result.unresolved[0].explanation

    def test_an_empty_profile_is_recognised_as_empty(self):
        assert Profile().is_empty
        assert not Profile(year=2).is_empty
