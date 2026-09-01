"""End-to-end analysis of a realistic notice.

Unit tests cover each stage; these assert the properties that only hold once
the stages compose -- that every claim is sourced, that the schedule reflects
the dependency graph, and that the engine reports what the document leaves out.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.claims import ClaimClass
from app.modules.action_engine.planner import ActionVerb, Priority
from app.modules.extraction.requirements import RequirementKind
from app.modules.ingestion.document import from_text
from app.pipeline import analyse

TODAY = date(2026, 9, 1)

SCHOLARSHIP_NOTICE = """NATIONAL MERIT SCHOLARSHIP 2026

Eligible students of the third year must submit the completed application form
along with their income certificate to the designated office before
18 September 2026.

Candidates should obtain the income certificate from the Tehsildar's office.

Applicants must verify their eligibility before applying.

A nominal fee is payable at the time of submission.
"""


@pytest.fixture
def analysis():
    return analyse(from_text(SCHOLARSHIP_NOTICE), today=TODAY)


class TestEvidence:
    def test_every_fact_and_inference_cites_a_span(self, analysis):
        claims = [*analysis.deadlines]
        claims.extend(item.action.claim for item in analysis.plan.scheduled)
        if analysis.title:
            claims.append(analysis.title)

        sourced = [
            claim
            for claim in claims
            if claim.classification in (ClaimClass.FACT, ClaimClass.INFERENCE)
        ]
        assert sourced
        assert all(claim.evidence is not None for claim in sourced)

    def test_cited_spans_slice_back_to_the_document(self, analysis):
        document = from_text(SCHOLARSHIP_NOTICE)
        for claim in analysis.deadlines:
            span = claim.evidence
            assert span is not None
            assert document.slice(span.char_start, span.char_end) == span.text

    def test_spans_resolve_to_a_real_page(self, analysis):
        for claim in analysis.deadlines:
            assert claim.evidence is not None
            assert 1 <= claim.evidence.page <= analysis.page_count


class TestDeadlines:
    def test_the_stated_deadline_is_found(self, analysis):
        assert any(claim.value == date(2026, 9, 18) for claim in analysis.deadlines)

    def test_the_primary_deadline_is_the_soonest_defensible_one(self, analysis):
        primary = analysis.primary_deadline

        assert primary is not None
        assert primary.value == date(2026, 9, 18)
        assert primary.classification is ClaimClass.FACT

    def test_the_deadline_quotes_the_sentence_that_states_it(self, analysis):
        primary = analysis.primary_deadline

        assert primary is not None and primary.evidence is not None
        assert "18 September" in primary.evidence.text


class TestPlan:
    def test_actions_are_extracted(self, analysis):
        verbs = {item.action.verb for item in analysis.plan.scheduled}

        assert ActionVerb.SUBMIT in verbs
        assert ActionVerb.OBTAIN in verbs

    def test_obtaining_the_certificate_precedes_submitting_it(self, analysis):
        order = {item.action.verb: item.order for item in analysis.plan.scheduled}

        assert order[ActionVerb.OBTAIN] < order[ActionVerb.SUBMIT]

    def test_the_submission_carries_the_stated_deadline(self, analysis):
        submit = next(
            item for item in analysis.plan.scheduled if item.action.verb is ActionVerb.SUBMIT
        )

        assert submit.action.deadline == date(2026, 9, 18)

    def test_the_certificate_inherits_a_deadline_it_never_stated(self, analysis):
        """The backward-scheduling payoff: an undated prerequisite gets a date."""
        obtain = next(
            item for item in analysis.plan.scheduled if item.action.verb is ActionVerb.OBTAIN
        )

        assert obtain.action.deadline is None
        assert obtain.effective_deadline is not None
        assert obtain.effective_deadline < date(2026, 9, 18)

    def test_the_certificate_has_a_latest_safe_start_date(self, analysis):
        obtain = next(
            item for item in analysis.plan.scheduled if item.action.verb is ActionVerb.OBTAIN
        )

        assert obtain.latest_start is not None
        assert obtain.slack_days is not None

    def test_next_actions_are_unblocked(self, analysis):
        for item in analysis.plan.next_actions:
            assert item.blocked_by == ()

    def test_every_action_carries_a_priority_and_a_reason(self, analysis):
        for item in analysis.plan.scheduled:
            assert isinstance(item.priority, Priority)
            assert item.rationale.strip()

    def test_the_graph_is_well_formed(self, analysis):
        assert analysis.plan.broken_cycles == ()
        assert analysis.plan.dangling_dependencies == ()


class TestGaps:
    def test_the_unnamed_office_is_reported(self, analysis):
        assert any("office" in gap.question.lower() for gap in analysis.gaps)

    def test_the_unstated_fee_is_reported(self, analysis):
        assert any("fee" in gap.question.lower() for gap in analysis.gaps)

    def test_every_gap_offers_a_next_step(self, analysis):
        for gap in analysis.gaps:
            assert gap.suggested_resolution
            assert gap.prompted_by is not None

    def test_unresolved_count_reflects_open_questions(self, analysis):
        assert analysis.unresolved_count >= len(analysis.gaps)


class TestDocumentType:
    def test_a_notice_is_classified(self, analysis):
        assert analysis.document_type.value == "Notice"

    def test_the_type_is_never_asserted_as_a_bare_fact(self, analysis):
        """A type is read off language, so it is an inference at best."""
        assert analysis.document_type.classification is not ClaimClass.FACT

    def test_the_type_cites_the_language_that_identified_it(self, analysis):
        claim = analysis.document_type

        if claim.classification is not ClaimClass.UNCERTAIN:
            assert claim.evidence is not None
            assert claim.evidence.text.strip()


class TestRequirements:
    def test_requirements_are_grouped_by_kind(self, analysis):
        kinds = {item.kind for item in analysis.requirements}

        assert RequirementKind.DOCUMENT in kinds

    def test_the_income_certificate_is_a_document_to_fetch(self, analysis):
        documents = [
            item.text.lower()
            for item in analysis.requirements
            if item.kind is RequirementKind.DOCUMENT
        ]

        assert any("income certificate" in text for text in documents)

    def test_requirements_are_deduplicated_across_actions(self, analysis):
        texts = [item.text.lower() for item in analysis.requirements]

        assert len(texts) == len(set(texts))

    def test_a_step_lists_the_same_requirements_as_the_summary(self, analysis):
        """Two cue patterns match one phrase, yielding both wordings.

        Collapsing them in the document-wide list but not on the step made the
        step say three things where the summary said two, and a reader who
        notices that stops trusting either.
        """
        summary = {item.text for item in analysis.requirements}

        for item in analysis.plan.scheduled:
            assert set(item.action.requires) <= summary


class TestOcrConfidence:
    """Certainty about a sentence cannot exceed certainty about its characters."""

    @staticmethod
    def ocr_document(text: str, confidence: float):
        from dataclasses import replace

        from app.modules.ingestion.document import SourceKind

        return replace(
            from_text(text),
            source_kind=SourceKind.IMAGE,
            text_confidence=confidence,
            ocr_engine="fake",
        )

    def test_a_clean_read_leaves_confidence_untouched(self):
        result = analyse(self.ocr_document(SCHOLARSHIP_NOTICE, 1.0), today=TODAY)

        assert result.primary_deadline is not None
        assert result.primary_deadline.confidence.score > 0.9

    def test_a_poor_read_caps_every_derived_claim(self):
        result = analyse(self.ocr_document(SCHOLARSHIP_NOTICE, 0.61), today=TODAY)

        assert result.primary_deadline is not None
        assert result.primary_deadline.confidence.score <= 0.61
        for item in result.plan.scheduled:
            assert item.action.claim.confidence.score <= 0.61

    def test_a_poor_read_demotes_a_stated_fact_to_an_inference(self):
        clean = analyse(from_text(SCHOLARSHIP_NOTICE), today=TODAY)
        scanned = analyse(self.ocr_document(SCHOLARSHIP_NOTICE, 0.61), today=TODAY)

        assert clean.primary_deadline is not None
        assert scanned.primary_deadline is not None
        assert clean.primary_deadline.classification is ClaimClass.FACT
        assert scanned.primary_deadline.classification is not ClaimClass.FACT

    def test_the_rationale_says_the_text_came_from_ocr(self):
        result = analyse(self.ocr_document(SCHOLARSHIP_NOTICE, 0.61), today=TODAY)

        assert result.primary_deadline is not None
        assert "ocr" in result.primary_deadline.confidence.rationale.lower()


class TestDocumentMetadata:
    def test_the_title_is_taken_from_the_heading(self, analysis):
        assert analysis.title is not None
        assert "SCHOLARSHIP" in analysis.title.value

    def test_the_title_is_an_inference_not_a_fact(self, analysis):
        """It is read off layout, not stated as a title, and says so."""
        assert analysis.title is not None
        assert analysis.title.classification is ClaimClass.INFERENCE

    def test_timing_is_recorded(self, analysis):
        assert analysis.duration_ms > 0

    def test_a_document_id_is_derived(self, analysis):
        assert len(analysis.document_id) == 16

    def test_identical_documents_produce_identical_ids(self):
        first = analyse(from_text(SCHOLARSHIP_NOTICE), today=TODAY)
        second = analyse(from_text(SCHOLARSHIP_NOTICE), today=TODAY)

        assert first.document_id == second.document_id


class TestDegenerateInput:
    def test_prose_with_no_instructions_yields_an_empty_plan(self):
        result = analyse(
            from_text("The scholarship was instituted in 1998 by the trust."),
            today=TODAY,
        )

        assert result.plan.scheduled == ()
        assert result.primary_deadline is None

    def test_analysis_is_deterministic(self):
        first = analyse(from_text(SCHOLARSHIP_NOTICE), today=TODAY)
        second = analyse(from_text(SCHOLARSHIP_NOTICE), today=TODAY)

        assert [item.action.id for item in first.plan.scheduled] == [
            item.action.id for item in second.plan.scheduled
        ]
        assert [claim.value for claim in first.deadlines] == [
            claim.value for claim in second.deadlines
        ]


def test_an_impossible_plan_is_reported_as_infeasible():
    """A certificate that takes a week, needed in two days, cannot be met."""
    urgent = """Students must submit the form along with their income certificate
to the office before 3 September 2026.

Candidates should obtain the income certificate from the Tehsildar's office.
"""
    result = analyse(from_text(urgent), today=TODAY)

    assert not result.plan.is_feasible
    assert any(item.priority is Priority.OVERDUE for item in result.plan.scheduled)
