"""Document classification and requirement grouping.

The behaviour that matters in both is restraint: a genuinely ambiguous document
must come back at low confidence rather than picking a winner, and an
unrecognised requirement must stay unclassified rather than being forced into
a bucket.
"""

from __future__ import annotations

import pytest

from app.modules.extraction.classify import DocumentType, classify
from app.modules.extraction.requirements import (
    Requirement,
    RequirementKind,
    classify_requirement,
    group,
    of_kind,
)


class TestDocumentType:
    def test_a_college_notice_is_recognised(self):
        result = classify(
            "NOTICE\n\nAll students are hereby informed that the last date for "
            "the scholarship application is 18 September 2026."
        )

        assert result.document_type is DocumentType.NOTICE
        assert result.confidence >= 0.7

    def test_a_job_description_is_recognised(self):
        result = classify(
            "Software Engineer — Backend\n\nResponsibilities:\n"
            "Build and maintain services.\n\nQualifications:\n"
            "3 years of experience in Python. Competitive salary and benefits."
        )

        assert result.document_type is DocumentType.JOB_DESCRIPTION

    def test_a_form_is_recognised_by_its_fill_in_rules(self):
        result = classify(
            "APPLICATION FORM\n\nName: ____________________\n"
            "Roll No: ____________________\n\nFor office use only"
        )

        assert result.document_type is DocumentType.FORM

    def test_a_policy_is_recognised(self):
        result = classify(
            "LEAVE POLICY\n\nThis policy comes into force on 1 April 2026 and is "
            "applicable to all employees. Clause 3 governs carry-forward."
        )

        assert result.document_type is DocumentType.POLICY

    def test_a_notice_that_mentions_a_form_is_not_a_form(self):
        """"Submit the completed application form" is a notice about a form.

        Notices name the application form far more often than forms name
        themselves, so that phrase alone must not outweigh notice language.
        """
        result = classify(
            "NATIONAL MERIT SCHOLARSHIP 2026\n\n"
            "Eligible students of the third year must submit the completed "
            "application form to the office before 18 September 2026."
        )

        assert result.document_type is DocumentType.NOTICE

    def test_unremarkable_prose_is_not_forced_into_a_type(self):
        result = classify("The building was completed in 1998 and renovated later.")

        assert result.document_type is DocumentType.OTHER
        assert result.confidence < 0.5

    def test_an_ambiguous_document_reports_low_confidence(self):
        """A notice announcing a policy scores as both; neither should win big."""
        result = classify(
            "NOTICE\n\nThe revised leave policy comes into force on 1 April. "
            "Clause 3 applies to all employees."
        )

        assert result.confidence <= 0.72
        assert "policy" in result.rationale.lower() or "notice" in result.rationale.lower()

    def test_the_strongest_signal_is_located_in_the_text(self):
        text = "NOTICE\n\nAll students are hereby informed of the last date."
        result = classify(text)

        assert result.strongest_signal is not None
        start, end = result.strongest_signal
        assert text[start:end].strip()

    def test_only_the_opening_is_scanned(self):
        """A notice that quotes a policy at length stays a notice."""
        notice = "NOTICE\n\nAll students are hereby informed of the last date.\n\n"
        policy_tail = "clause section 4 policy guidelines comes into force " * 60

        assert classify(notice + policy_tail).document_type is DocumentType.NOTICE


class TestRequirementGrouping:
    @pytest.mark.parametrize(
        ("phrase", "expected"),
        [
            ("Income certificate", RequirementKind.DOCUMENT),
            ("Previous marksheet", RequirementKind.DOCUMENT),
            ("Aadhaar card", RequirementKind.DOCUMENT),
            ("Two passport size photographs", RequirementKind.DOCUMENT),
            ("Updated resume", RequirementKind.DOCUMENT),
            ("Bank account details", RequirementKind.INFORMATION),
            ("Mobile number", RequirementKind.INFORMATION),
            ("Permanent address", RequirementKind.INFORMATION),
            ("Minimum 60% marks", RequirementKind.CONDITION),
            ("Not less than 18 years of age", RequirementKind.CONDITION),
            ("Eligible under the reserved category", RequirementKind.CONDITION),
        ],
    )
    def test_phrases_land_in_the_right_bucket(self, phrase, expected):
        assert classify_requirement(phrase) is expected

    def test_a_criterion_mentioning_a_document_is_still_a_criterion(self):
        """"Minimum 60% in the qualifying marksheet" is a bar, not a request."""
        assert (
            classify_requirement("Minimum 60% in the qualifying marksheet")
            is RequirementKind.CONDITION
        )

    def test_an_unrecognised_phrase_stays_unclassified(self):
        assert classify_requirement("Whatever else applies") is (
            RequirementKind.UNCLASSIFIED
        )

    def test_blank_phrases_are_unclassified(self):
        assert classify_requirement("   ") is RequirementKind.UNCLASSIFIED

    def test_grouping_drops_case_insensitive_duplicates(self):
        grouped = group(("Income certificate", "income  certificate", "Aadhaar"))

        assert len(grouped) == 2

    def test_a_less_specific_restatement_is_dropped(self):
        """Overlapping cues yield both forms; the specific one is the useful one."""
        grouped = group(
            ("Self-attested copy of previous marksheet", "Previous marksheet")
        )

        assert [item.text for item in grouped] == [
            "Self-attested copy of previous marksheet"
        ]

    def test_distinct_requirements_are_both_kept(self):
        grouped = group(("Income certificate", "Caste certificate"))

        assert len(grouped) == 2

    def test_subsumption_ignores_ordering(self):
        forward = group(("Previous marksheet", "Attested copy of previous marksheet"))
        backward = group(("Attested copy of previous marksheet", "Previous marksheet"))

        assert len(forward) == len(backward) == 1

    def test_grouping_preserves_document_order(self):
        grouped = group(("Aadhaar", "Minimum 60% marks", "Mobile number"))

        assert [item.kind for item in grouped] == [
            RequirementKind.DOCUMENT,
            RequirementKind.CONDITION,
            RequirementKind.INFORMATION,
        ]

    def test_filtering_by_kind_returns_the_original_text(self):
        grouped = group(("Income certificate", "Mobile number"))

        assert of_kind(grouped, RequirementKind.DOCUMENT) == ("Income certificate",)

    def test_only_documents_count_as_artefacts_to_fetch(self):
        assert Requirement("Income certificate", RequirementKind.DOCUMENT).is_actionable_artefact
        assert not Requirement("Mobile number", RequirementKind.INFORMATION).is_actionable_artefact

    def test_empty_input_yields_nothing(self):
        assert group(()) == ()
