"""Calendar and email export.

An .ics file is parsed by software that will not forgive a missing CRLF or an
unescaped comma, so the mechanics get as much attention here as the content.
The content tests exist to pin one decision: the event lands on the start date,
not the deadline.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.export import calendar_for, enquiry_for
from app.api.schemas import AnalysisOut
from app.main import app

NOTICE = """NATIONAL MERIT SCHOLARSHIP 2026

Eligible students of the third year must submit the completed application form
along with their income certificate to the designated office before
18 September 2026.

Candidates should obtain the income certificate from the Tehsildar's office.

A nominal fee is payable at the time of submission.
"""

STAMP = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture
def client():
    with TestClient(app) as instance:
        yield instance


@pytest.fixture
def analysis(client) -> AnalysisOut:
    response = client.post(
        "/v1/documents/text",
        json={"text": NOTICE, "filename": "Scholarship notice.pdf"},
    )
    return AnalysisOut(**response.json())


@pytest.fixture
def ics(analysis) -> str:
    return calendar_for(analysis, now=STAMP)


class TestCalendarStructure:
    def test_it_is_a_well_formed_calendar(self, ics):
        assert ics.startswith("BEGIN:VCALENDAR\r\n")
        assert ics.endswith("END:VCALENDAR\r\n")
        assert "VERSION:2.0" in ics

    def test_every_line_ends_with_crlf(self, ics):
        assert "\n" not in ics.replace("\r\n", "")

    def test_events_are_balanced(self, ics):
        assert ics.count("BEGIN:VEVENT") == ics.count("END:VEVENT")
        assert ics.count("BEGIN:VALARM") == ics.count("END:VALARM")

    def test_every_event_carries_a_unique_id(self, ics):
        uids = [line for line in ics.split("\r\n") if line.startswith("UID:")]

        assert uids
        assert len(uids) == len(set(uids))

    def test_no_content_line_exceeds_the_octet_limit(self, ics):
        for line in ics.split("\r\n"):
            assert len(line.encode("utf-8")) <= 75

    def test_folding_round_trips(self, ics):
        """Unfolding removes CRLF and exactly one space, per RFC 5545 §3.1.

        The subtlety is a fold that lands where the text already had a space:
        the continuation line then starts with two, and only one of them is
        the fold. Stripping both would silently eat a word boundary from every
        long description.
        """
        assert "\r\n " in ics, "the descriptions here are long enough to fold"

        unfolded = ics.replace("\r\n ", "")
        summaries = [
            line for line in unfolded.split("\r\n") if line.startswith("SUMMARY:")
        ]

        assert any("income certificate" in line for line in summaries)
        assert not any("  " in line for line in summaries)

    def test_commas_and_semicolons_are_escaped(self, ics):
        for line in ics.split("\r\n"):
            if not line.startswith(("SUMMARY:", "DESCRIPTION:")):
                continue
            body = line.split(":", 1)[1]
            for index, character in enumerate(body):
                if character in ",;":
                    assert body[index - 1] == "\\"


class TestCalendarContent:
    def test_the_event_is_the_start_date_not_the_deadline(self, analysis, ics):
        obtain = next(a for a in analysis.actions if a.verb == "obtain")
        assert obtain.latest_start is not None
        assert obtain.latest_start != obtain.effective_deadline

        expected = obtain.latest_start.strftime("%Y%m%d")
        assert f"DTSTART;VALUE=DATE:{expected}" in ics

    def test_an_all_day_event_ends_the_following_day(self, analysis, ics):
        """DTEND is exclusive; equal dates render as a zero-length event."""
        starts = _values(ics, "DTSTART;VALUE=DATE:")
        ends = _values(ics, "DTEND;VALUE=DATE:")

        assert len(starts) == len(ends)
        for start, end in zip(starts, ends):
            assert int(end) > int(start)

    def test_the_stated_deadline_gets_its_own_entry(self, ics):
        assert "SUMMARY:Deadline" in ics

    def test_the_source_sentence_travels_into_the_calendar(self, ics):
        unfolded = ics.replace("\r\n ", "")

        assert "Source:" in unfolded

    def test_a_low_confidence_entry_is_marked_tentative(self, analysis):
        low = [a for a in analysis.actions if a.claim.confidence < 0.75]
        ics = calendar_for(analysis, now=STAMP)

        if low:
            assert "STATUS:TENTATIVE" in ics
        else:
            assert "STATUS:CONFIRMED" in ics

    def test_the_confidence_reads_as_a_word_not_a_python_enum(self, ics):
        """`f"{claim.classification}"` renders "ClaimClass.FACT" and looks it."""
        unfolded = ics.replace("\r\n ", "")

        assert "ClaimClass." not in unfolded
        assert "Confidence: FACT" in unfolded or "Confidence: INFERENCE" in unfolded

    def test_durations_are_pluralised(self, ics):
        unfolded = ics.replace("\r\n ", "")

        assert "day(s)" not in unfolded

    def test_an_inherited_date_says_it_was_inherited(self, ics):
        unfolded = ics.replace("\r\n ", "")

        assert "not stated in the document" in unfolded

    def test_undated_actions_produce_no_event(self, client):
        response = client.post(
            "/v1/documents/text",
            json={"text": "Students must submit the completed form."},
        )
        analysis = AnalysisOut(**response.json())
        ics = calendar_for(analysis, now=STAMP)

        assert "BEGIN:VEVENT" not in ics

    def test_a_document_with_nothing_dated_still_parses(self, client):
        response = client.post(
            "/v1/documents/text",
            json={"text": "Students must submit the completed form."},
        )
        ics = calendar_for(AnalysisOut(**response.json()), now=STAMP)

        assert ics.startswith("BEGIN:VCALENDAR")
        assert ics.endswith("END:VCALENDAR\r\n")


class TestEnquiry:
    def test_open_questions_become_a_numbered_draft(self, analysis):
        draft = enquiry_for(analysis)

        assert draft.question_count == len(analysis.gaps)
        assert "1." in draft.body

    def test_the_draft_quotes_the_phrase_that_raised_each_question(self, analysis):
        draft = enquiry_for(analysis)

        assert "designated office" in draft.body

    def test_the_deadline_is_mentioned_so_the_reply_is_urgent(self, analysis):
        assert "September 2026" in enquiry_for(analysis).body

    def test_a_document_with_no_gaps_produces_nothing_to_ask(self, client):
        response = client.post(
            "/v1/documents/text",
            json={"text": "Submit the form to Room 12, Admin Block, by 1 Oct 2026."},
        )
        draft = enquiry_for(AnalysisOut(**response.json()))

        assert draft.question_count == 0
        assert "no unanswered points" in draft.body

    def test_an_unresolved_eligibility_condition_becomes_a_question(self, client):
        """A criterion the engine declined to settle is one the reader must ask."""
        created = client.post(
            "/v1/documents/text",
            json={"text": "Students of the Computer Science department may apply."},
        ).json()
        updated = client.patch(
            f"/v1/documents/{created['document_id']}",
            json={"profile": {"programme": "CSE"}},
        ).json()
        draft = enquiry_for(AnalysisOut(**updated))

        assert draft.question_count == 1
        assert "CSE" in draft.body


def _values(ics: str, prefix: str) -> list[str]:
    return [
        line[len(prefix) :] for line in ics.split("\r\n") if line.startswith(prefix)
    ]
