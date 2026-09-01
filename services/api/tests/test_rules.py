"""Rule-based extraction.

These tests define the baseline the hybrid pipeline has to beat, so they are
written against realistic notice prose rather than synthetic sentences.
"""

from __future__ import annotations

import pytest

from app.modules.action_engine.planner import ActionVerb
from app.modules.extraction.rules import (
    extract_actions,
    extract_gaps,
    split_sentences,
)

NOTICE = """NATIONAL MERIT SCHOLARSHIP 2026

Eligible students of the third year must submit the completed application form
along with their income certificate and a self-attested copy of the previous
marksheet to the designated office before 18 September 2026.

Candidates should obtain the income certificate from the Tehsildar's office.

Please verify your bank account details before submission.

Applicants may attend the orientation session on 10 September 2026.

A nominal fee is payable at the time of submission.
"""


class TestSentenceSplitting:
    def test_offsets_slice_back_to_the_sentence(self):
        for sentence in split_sentences(NOTICE):
            assert NOTICE[sentence.char_start : sentence.char_end] == sentence.text

    def test_abbreviations_do_not_end_a_sentence(self):
        sentences = split_sentences("Contact Dr. Sharma at the office. Then submit.")

        assert len(sentences) == 2
        assert "Dr. Sharma" in sentences[0].text

    def test_initials_do_not_end_a_sentence(self):
        sentences = split_sentences("Address it to R. K. Sharma. Submit by Friday.")

        assert len(sentences) == 2

    def test_numbered_list_items_split(self):
        sentences = split_sentences("Steps:\n1. Collect the form\n2. Fill it in")

        assert len(sentences) >= 2


class TestActionExtraction:
    @pytest.fixture
    def actions(self):
        return extract_actions(NOTICE)

    def test_obligation_sentences_become_actions(self, actions):
        assert any(item.verb is ActionVerb.SUBMIT for item in actions)
        assert any(item.verb is ActionVerb.OBTAIN for item in actions)

    def test_verbs_map_to_coarse_buckets(self, actions):
        verbs = {item.verb for item in actions}

        assert verbs <= set(ActionVerb)
        assert ActionVerb.CONFIRM in verbs  # "Please verify your bank account"

    def test_action_spans_locate_a_non_empty_source_region(self, actions):
        for item in actions:
            assert item.char_end > item.char_start
            spanned = NOTICE[item.char_start : item.char_end]
            assert spanned == spanned.strip()

    def test_must_scores_higher_than_may(self, actions):
        must = next(item for item in actions if item.verb is ActionVerb.SUBMIT)
        may = next(item for item in actions if item.verb is ActionVerb.ATTEND)

        assert must.confidence > may.confidence

    def test_description_is_rewritten_as_an_instruction(self, actions):
        submit = next(item for item in actions if item.verb is ActionVerb.SUBMIT)

        assert submit.description.lower().startswith("submit")
        assert "must" not in submit.description.lower()

    def test_requirements_are_pulled_from_the_same_sentence(self, actions):
        submit = next(item for item in actions if item.verb is ActionVerb.SUBMIT)
        joined = " ".join(submit.requires).lower()

        assert "income certificate" in joined

    @pytest.mark.parametrize(
        ("sentence", "expected"),
        [
            ("Registrations must be completed within 10 days.", ActionVerb.PREPARE),
            ("The form must be submitted to the office.", ActionVerb.SUBMIT),
            ("Eligibility must be verified before applying.", ActionVerb.CONFIRM),
            ("A certificate must be obtained from the Tehsildar.", ActionVerb.OBTAIN),
            ("Documents should be uploaded to the portal.", ActionVerb.SUBMIT),
            (
                "Students are required to register for the programme.",
                ActionVerb.SUBMIT,
            ),
            ("Candidates must enrol before the last date.", ActionVerb.SUBMIT),
        ],
    )
    def test_passive_and_inflected_verbs_are_recognised(self, sentence, expected):
        """Notices are written in the passive far more often than the imperative.

        Matching only base forms missed "must be completed" entirely, which is
        how most obligations in real notices are phrased.
        """
        found = extract_actions(sentence)

        assert found, f"no action extracted from {sentence!r}"
        assert found[0].verb is expected

    @pytest.mark.parametrize(
        ("sentence", "expected"),
        [
            (
                "Registrations must be completed within 10 days.",
                "Complete registrations within 10 days",
            ),
            (
                "The form must be submitted to the office.",
                "Submit the form to the office",
            ),
            (
                "Eligibility must be verified before applying.",
                "Verify eligibility before applying",
            ),
        ],
    )
    def test_passive_sentences_are_rewritten_as_instructions(self, sentence, expected):
        """Deleting the modal alone leaves a fragment, not an instruction.

        "Registrations must be completed" became "Completed within 10 days",
        which reads as a status rather than something to do.
        """
        found = extract_actions(sentence)

        assert found
        assert found[0].description == expected

    def test_instructions_address_the_reader_directly(self):
        """The notice writes about students; the plan writes to one."""
        found = extract_actions("Students must upload their updated resume.")

        assert found
        assert found[0].description == "Upload your updated resume"

    def test_long_descriptions_truncate_on_a_word_boundary(self):
        long_sentence = (
            "Students must submit the completed application form along with "
            "the income certificate, the caste certificate, the domicile "
            "certificate, the previous marksheet and two passport size "
            "photographs to the designated office before 18 September 2026."
        )
        found = extract_actions(long_sentence)

        assert found
        description = found[0].description
        assert description.endswith("…")
        assert not description.rstrip("…").endswith(" ")
        # A cut mid-word would leave a fragment of the following token.
        assert long_sentence.count(description.rstrip("…").split()[-1]) >= 1

    def test_a_long_subject_is_dropped_rather_than_folded_in(self):
        found = extract_actions(
            "All third-year students enrolled in the Computer Science programme "
            "must be verified before the deadline."
        )

        assert found
        assert found[0].description.startswith("Verify")

    def test_prose_without_instructions_yields_no_actions(self):
        text = "The scholarship was instituted in 1998 and is funded by the trust."

        assert extract_actions(text) == []

    def test_headings_are_not_actions(self):
        assert extract_actions("REQUIRED DOCUMENTS") == []


class TestGapDetection:
    @pytest.fixture
    def gaps(self):
        return extract_gaps(NOTICE)

    def test_unnamed_office_is_reported(self, gaps):
        assert any("office" in gap.question.lower() for gap in gaps)

    def test_unstated_fee_is_reported(self, gaps):
        assert any("fee" in gap.question.lower() for gap in gaps)

    def test_gap_spans_locate_the_vague_phrase(self, gaps):
        for gap in gaps:
            assert NOTICE[gap.char_start : gap.char_end] == gap.matched_text

    def test_gaps_are_returned_in_document_order(self, gaps):
        offsets = [gap.char_start for gap in gaps]

        assert offsets == sorted(offsets)

    def test_a_portal_with_a_url_is_not_a_gap(self):
        resolved = extract_gaps("Apply through the online portal at admissions.example.edu.")

        assert not any("portal" in gap.question.lower() for gap in resolved)

    def test_a_portal_without_a_url_is_a_gap(self):
        unresolved = extract_gaps("Applications are accepted through the online portal only.")

        assert any("portal" in gap.question.lower() for gap in unresolved)

    def test_a_stated_fee_is_not_a_gap(self):
        resolved = extract_gaps("A nominal fee of Rs. 200 is payable on submission.")

        assert not any("fee" in gap.question.lower() for gap in resolved)

    def test_each_phrase_is_reported_once(self, gaps):
        spans = [(gap.char_start, gap.char_end) for gap in gaps]

        assert len(spans) == len(set(spans))

    def test_clear_prose_produces_no_gaps(self):
        text = "Submit the form to Room 12, Administrative Block, before 18 September 2026."

        assert extract_gaps(text) == []
