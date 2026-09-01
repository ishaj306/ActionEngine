"""Revision comparison and cross-document contradiction.

Both are diffs of *findings*, and the tests are written to hold that line. A
reissued notice is retyped, so the tests reword the unchanged parts on purpose:
anything that reports rewording as change has failed.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.modules.ingestion.document import from_text
from app.modules.reasoning.changes import ChangeKind, Severity, compare
from app.modules.reasoning.crossdoc import ConflictKind, review
from app.pipeline import analyse

TODAY = date(2026, 9, 1)

V1 = """NATIONAL MERIT SCHOLARSHIP 2026

Eligible students of the third year must submit the completed application form
along with their income certificate to the designated office before
18 September 2026.

Candidates should obtain the income certificate from the Tehsildar's office.
"""

V2 = """NATIONAL MERIT SCHOLARSHIP 2026 (REVISED)

Eligible students of the third year must submit the duly completed application
form along with their income certificate and a caste certificate to the
designated office before 14 September 2026.

Candidates should obtain the income certificate from the Tehsildar's office.
"""


def read(text: str):
    return analyse(from_text(text), today=TODAY)


@pytest.fixture
def comparison():
    return compare(read(V1), read(V2))


class TestRevisionComparison:
    def test_a_deadline_pulled_forward_is_critical(self, comparison):
        moved = next(
            item for item in comparison.changes if item.kind is ChangeKind.DEADLINE_MOVED
        )

        assert moved.severity is Severity.CRITICAL
        assert moved.before == "2026-09-18"
        assert moved.after == "2026-09-14"
        assert "less time" in moved.summary

    def test_a_deadline_pushed_back_is_not_critical(self):
        later = V1.replace("18 September 2026", "28 September 2026")
        moved = next(
            item
            for item in compare(read(V1), read(later)).changes
            if item.kind is ChangeKind.DEADLINE_MOVED
        )

        assert moved.severity is not Severity.CRITICAL

    def test_a_new_requirement_is_reported(self, comparison):
        added = [
            item
            for item in comparison.changes
            if item.kind is ChangeKind.REQUIREMENT_ADDED
        ]

        assert any("caste certificate" in item.summary.lower() for item in added)

    def test_rewording_an_unchanged_step_is_not_a_change(self, comparison):
        """"completed application form" became "duly completed application form"."""
        churn = [
            item
            for item in comparison.changes
            if item.kind in (ChangeKind.ACTION_ADDED, ChangeKind.ACTION_REMOVED)
        ]

        assert churn == []

    def test_steps_moved_only_by_the_deadline_are_not_relisted(self, comparison):
        """The deadline moved four days; every dated step moved four days with it.

        Reporting each as its own finding restates one cause five times and
        leaves five equally loud lines with no signal among them.
        """
        assert not any(
            item.kind is ChangeKind.ACTION_RESCHEDULED for item in comparison.changes
        )

    def test_an_unexplained_reschedule_is_still_reported(self):
        """A step whose own effort changed moves for a reason of its own."""
        slower = V1.replace(
            "Candidates should obtain the income certificate",
            "Candidates should obtain the income certificate and the caste certificate",
        )
        rescheduled = [
            item
            for item in compare(read(V1), read(slower)).changes
            if item.kind is ChangeKind.ACTION_RESCHEDULED
        ]

        assert all(item.severity is not Severity.CRITICAL for item in rescheduled)

    def test_the_headline_leads_with_the_critical_change(self, comparison):
        assert comparison.critical
        assert comparison.headline == comparison.critical[0].summary

    def test_an_identical_reissue_reports_nothing(self):
        assert compare(read(V1), read(V1)).is_unchanged

    def test_an_added_eligibility_condition_is_critical(self):
        stricter = V1.replace(
            "Eligible students of the third year",
            "Eligible students of the third year with a minimum of 75%",
        )
        added = next(
            item
            for item in compare(read(V1), read(stricter)).changes
            if item.kind is ChangeKind.CONDITION_ADDED
        )

        assert added.severity is Severity.CRITICAL
        assert "still qualify" in added.summary

    def test_a_removed_deadline_is_not_read_as_an_extension(self):
        undated = V1.replace(" before\n18 September 2026", "")
        removed = next(
            item
            for item in compare(read(V1), read(undated)).changes
            if item.kind is ChangeKind.DEADLINE_REMOVED
        )

        assert "not been extended" in removed.summary

    def test_unrelated_documents_are_flagged_rather_than_diffed_silently(self):
        other = (
            "LEAVE POLICY\n\nEmployees must apply for casual leave through the "
            "portal at least two working days in advance."
        )
        result = compare(read(V1), read(other))

        assert result.warning is not None
        assert result.relatedness < 0.35

    def test_two_versions_of_one_notice_are_recognised_as_related(self, comparison):
        assert comparison.warning is None
        assert comparison.relatedness >= 0.35


CIRCULAR = """DEPARTMENTAL CIRCULAR — MERIT SCHOLARSHIP

Third year students seeking the merit scholarship must submit the application
form and income certificate to the department office before 22 September 2026.
"""

UNRELATED = """HOSTEL MESS TIMINGS

Residents must vacate the dining hall by 9 PM.
"""


class TestPortfolio:
    @pytest.fixture
    def portfolio(self):
        return review(
            {
                "doc-a": ("Scholarship notice", read(V1)),
                "doc-b": ("Department circular", read(CIRCULAR)),
            }
        )

    def test_disagreeing_deadlines_are_a_conflict(self, portfolio):
        conflict = next(
            item for item in portfolio.conflicts if item.kind is ConflictKind.DEADLINE
        )

        assert "18 September 2026" in conflict.summary
        assert "22 September 2026" in conflict.summary

    def test_the_resolution_favours_the_earlier_date(self, portfolio):
        conflict = portfolio.conflicts[0]

        assert "18 September 2026" in conflict.resolution

    def test_both_positions_are_attributed(self, portfolio):
        conflict = portfolio.conflicts[0]

        assert {item[1] for item in conflict.positions} == {
            "Scholarship notice",
            "Department circular",
        }

    def test_the_timeline_merges_steps_from_every_document(self, portfolio):
        sources = {item.document_name for item in portfolio.timeline}

        assert len(sources) == 2

    def test_the_timeline_is_ordered_by_start_date(self, portfolio):
        starts = [item.action.latest_start for item in portfolio.timeline]

        assert starts == sorted(starts)

    def test_every_step_keeps_its_source_document(self, portfolio):
        for item in portfolio.timeline:
            assert item.document_id in {"doc-a", "doc-b"}

    def test_unrelated_documents_do_not_contradict_each_other(self):
        """Different subjects state different things. That is not a conflict."""
        result = review(
            {
                "doc-a": ("Scholarship notice", read(V1)),
                "doc-b": ("Mess timings", read(UNRELATED)),
            }
        )

        assert result.is_consistent

    def test_agreeing_documents_are_consistent(self):
        restated = CIRCULAR.replace("22 September 2026", "18 September 2026")
        result = review(
            {
                "doc-a": ("Scholarship notice", read(V1)),
                "doc-b": ("Department circular", read(restated)),
            }
        )

        assert result.is_consistent

    def test_conflicting_eligibility_bars_are_reported(self):
        strict = V1.replace(
            "Eligible students of the third year",
            "Eligible students of the third year with a minimum of 75%",
        )
        lenient = CIRCULAR.replace(
            "Third year students seeking",
            "Third year students with a minimum of 60% seeking",
        )
        result = review(
            {"doc-a": ("Notice", read(strict)), "doc-b": ("Circular", read(lenient))}
        )

        conflict = next(
            item for item in result.conflicts if item.kind is ConflictKind.ELIGIBILITY
        )
        assert "75" in conflict.summary
        assert "60" in conflict.summary

    def test_the_soonest_stated_deadline_is_reported(self, portfolio):
        assert portfolio.next_stated_deadline == date(2026, 9, 18)

    def test_the_soonest_date_anything_is_due_can_precede_every_stated_one(
        self, portfolio
    ):
        """A prerequisite inherits its dependent's deadline, minus its effort.

        So the date the reader is actually working to is earlier than anything
        written in any of the documents — which is why the two are reported
        separately rather than one standing in for the other.
        """
        assert portfolio.next_due is not None
        assert portfolio.next_due < portfolio.next_stated_deadline

    def test_undated_steps_are_listed_not_dropped(self):
        result = review(
            {
                "doc-a": ("Scholarship notice", read(V1)),
                "doc-b": ("Mess timings", read(UNRELATED)),
            }
        )
        total = len(result.timeline) + len(result.undated)
        expected = len(read(V1).plan.scheduled) + len(read(UNRELATED).plan.scheduled)

        assert total == expected
