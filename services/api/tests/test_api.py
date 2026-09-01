"""HTTP contract.

The invariant worth testing at this layer is the serialization guarantee: a
claim cannot reach a client without its classification, its confidence and its
source span. Everything else here is ordinary request handling.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.main import app, store

NOTICE = """NATIONAL MERIT SCHOLARSHIP 2026

Eligible students of the third year must submit the completed application form
along with their income certificate to the designated office before
18 September 2026.

Candidates should obtain the income certificate from the Tehsildar's office.
"""


@pytest.fixture
def client():
    store._items.clear()
    return TestClient(app)


@pytest.fixture
def analysed(client):
    response = client.post("/v1/documents/text", json={"text": NOTICE})
    assert response.status_code == 201
    return response.json()


def test_health_reports_ok(client):
    assert client.get("/health").json()["status"] == "ok"


class TestUpload:
    def test_text_upload_returns_an_analysis(self, analysed):
        assert analysed["actions"]
        assert analysed["primary_deadline"]["value"] == "2026-09-18"

    def test_file_upload_returns_an_analysis(self, client):
        response = client.post(
            "/v1/documents",
            files={"file": ("notice.txt", io.BytesIO(NOTICE.encode()), "text/plain")},
        )

        assert response.status_code == 201
        assert response.json()["filename"] == "notice.txt"

    def test_empty_text_is_rejected(self, client):
        response = client.post("/v1/documents/text", json={"text": "   "})

        assert response.status_code == 400

    def test_empty_file_is_rejected(self, client):
        response = client.post(
            "/v1/documents",
            files={"file": ("empty.txt", io.BytesIO(b""), "text/plain")},
        )

        assert response.status_code == 400

    def test_binary_upload_is_rejected_with_a_remedy(self, client):
        response = client.post(
            "/v1/documents",
            files={"file": ("logo.png", io.BytesIO(b"\x89PNG\r\n\x1a\n\x00" * 4), "image/png")},
        )

        assert response.status_code == 415
        assert response.json()["remedy"]


class TestSerializationInvariant:
    def test_every_action_carries_classification_and_confidence(self, analysed):
        for action in analysed["actions"]:
            claim = action["claim"]
            assert claim["classification"] in {
                "FACT",
                "INFERENCE",
                "UNCERTAIN",
                "MISSING",
            }
            assert 0.0 <= claim["confidence"] <= 1.0
            assert claim["rationale"]

    def test_facts_and_inferences_arrive_with_evidence(self, analysed):
        claims = [action["claim"] for action in analysed["actions"]]
        claims.extend(analysed["deadlines"])

        for claim in claims:
            if claim["classification"] in {"FACT", "INFERENCE"}:
                assert claim["evidence"] is not None
                assert claim["evidence"]["text"]

    def test_evidence_offsets_index_the_returned_text(self, analysed):
        text = analysed["text"]

        for claim in analysed["deadlines"]:
            evidence = claim["evidence"]
            assert text[evidence["char_start"] : evidence["char_end"]] == evidence["text"]

    def test_every_gap_carries_a_next_step(self, analysed):
        for gap in analysed["gaps"]:
            assert gap["suggested_resolution"]

    def test_scheduling_fields_are_exposed(self, analysed):
        obtain = next(a for a in analysed["actions"] if a["verb"] == "obtain")

        assert obtain["effective_deadline"] is not None
        assert obtain["latest_start"] is not None
        assert obtain["slack_days"] is not None


class TestRetrieval:
    def test_an_analysis_can_be_fetched_by_id(self, client, analysed):
        response = client.get(f"/v1/documents/{analysed['document_id']}")

        assert response.status_code == 200
        assert response.json()["document_id"] == analysed["document_id"]

    def test_unknown_id_returns_404(self, client):
        assert client.get("/v1/documents/nope").status_code == 404

    def test_listing_returns_most_recent_first(self, client):
        first = client.post("/v1/documents/text", json={"text": NOTICE}).json()
        second = client.post(
            "/v1/documents/text",
            json={"text": "Submit the report before 20 October 2026."},
        ).json()

        listed = [item["document_id"] for item in client.get("/v1/documents").json()]

        assert listed[0] == second["document_id"]
        assert first["document_id"] in listed

    def test_a_document_can_be_deleted(self, client, analysed):
        document_id = analysed["document_id"]

        assert client.delete(f"/v1/documents/{document_id}").status_code == 204
        assert client.get(f"/v1/documents/{document_id}").status_code == 404

    def test_deleting_an_unknown_document_returns_404(self, client):
        assert client.delete("/v1/documents/nope").status_code == 404


class TestWorkingState:
    """Completion and profile are the reader's state, not the document's."""

    def test_ticking_a_step_off_recomputes_the_plan(self, client, analysed):
        obtain = next(a for a in analysed["actions"] if a["verb"] == "obtain")
        blocked = [a for a in analysed["actions"] if obtain["id"] in a["blocked_by"]]
        assert blocked, "the sample has a dependency worth unblocking"

        updated = client.patch(
            f"/v1/documents/{analysed['document_id']}",
            json={"completed": [obtain["id"]]},
        ).json()

        after = next(a for a in updated["actions"] if a["id"] == blocked[0]["id"])
        assert obtain["id"] not in after["blocked_by"]
        assert updated["completed"] == [obtain["id"]]

    def test_completion_survives_a_later_read(self, client, analysed):
        document_id = analysed["document_id"]
        first = analysed["actions"][0]["id"]
        client.patch(f"/v1/documents/{document_id}", json={"completed": [first]})

        assert client.get(f"/v1/documents/{document_id}").json()["completed"] == [first]

    def test_a_completed_step_stops_counting_against_feasibility(self, client):
        """A step finished last week is not a scheduling problem."""
        text = (
            "Candidates must obtain the income certificate from the Tehsildar.\n"
            "Students must submit the application before 2 September 2026."
        )
        created = client.post("/v1/documents/text", json={"text": text}).json()
        assert not created["is_feasible"]

        late = [a for a in created["actions"] if (a["slack_days"] or 0) < 0]
        updated = client.patch(
            f"/v1/documents/{created['document_id']}",
            json={"completed": [a["id"] for a in late]},
        ).json()

        assert updated["is_feasible"]

    def test_an_unknown_action_id_is_rejected(self, client, analysed):
        response = client.patch(
            f"/v1/documents/{analysed['document_id']}",
            json={"completed": ["action-999"]},
        )

        assert response.status_code == 422
        assert "action-999" in response.json()["detail"]

    def test_a_profile_produces_a_relevance_verdict(self, client, analysed):
        updated = client.patch(
            f"/v1/documents/{analysed['document_id']}",
            json={"profile": {"year": 1}},
        ).json()

        assert updated["relevance_verdict"] == "does_not_apply"
        assert updated["relevance"]["classification"] == "INFERENCE"
        assert updated["relevance"]["evidence"] is not None

    def test_relevance_is_absent_until_a_profile_is_given(self, analysed):
        assert analysed["relevance"] is None
        assert analysed["relevance_verdict"] is None

    def test_conditions_are_listed_even_without_a_profile(self, analysed):
        assert any(item["attribute"] == "year" for item in analysed["conditions"])
        assert all(item["match"] is None for item in analysed["conditions"])

    def test_a_profile_can_be_forgotten(self, client, analysed):
        document_id = analysed["document_id"]
        client.patch(f"/v1/documents/{document_id}", json={"profile": {"year": 3}})

        cleared = client.patch(
            f"/v1/documents/{document_id}", json={"clear_profile": True}
        ).json()

        assert cleared["relevance"] is None

    def test_an_out_of_range_profile_value_is_rejected(self, client, analysed):
        response = client.patch(
            f"/v1/documents/{analysed['document_id']}",
            json={"profile": {"score": 140}},
        )

        assert response.status_code == 422

    def test_patching_an_unknown_document_returns_404(self, client):
        assert client.patch("/v1/documents/nope", json={}).status_code == 404

    def test_the_browser_is_allowed_to_send_a_patch(self, client, analysed):
        """A method missing from the CORS allow-list fails at preflight.

        The request never reaches the handler, so every server-side test still
        passes while the feature is dead in a browser.
        """
        response = client.options(
            f"/v1/documents/{analysed['document_id']}",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": "content-type",
            },
        )

        assert response.status_code == 200
        assert "PATCH" in response.headers["access-control-allow-methods"]


class TestExportEndpoints:
    def test_the_calendar_is_served_as_a_download(self, client, analysed):
        response = client.get(f"/v1/documents/{analysed['document_id']}/calendar.ics")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/calendar")
        assert "attachment" in response.headers["content-disposition"]
        assert response.text.startswith("BEGIN:VCALENDAR")

    def test_a_non_ascii_filename_still_downloads(self, client):
        """Header values are latin-1; a Devanagari title must not raise."""
        created = client.post(
            "/v1/documents/text",
            json={"text": NOTICE, "filename": "छात्रवृत्ति सूचना.pdf"},
        ).json()

        response = client.get(f"/v1/documents/{created['document_id']}/calendar.ics")

        assert response.status_code == 200
        assert response.headers["content-disposition"].isascii()

    def test_the_enquiry_draft_asks_the_open_questions(self, client, analysed):
        draft = client.get(f"/v1/documents/{analysed['document_id']}/enquiry").json()

        assert draft["question_count"] == len(analysed["gaps"])
        assert draft["subject"]

    def test_exports_404_for_an_unknown_document(self, client):
        assert client.get("/v1/documents/nope/calendar.ics").status_code == 404
        assert client.get("/v1/documents/nope/enquiry").status_code == 404


REVISED = NOTICE.replace("18 September 2026", "14 September 2026")

CIRCULAR = """DEPARTMENTAL CIRCULAR — MERIT SCHOLARSHIP

Third year students seeking the merit scholarship must submit the application
form and income certificate to the department office before 22 September 2026.
"""


class TestCrossDocument:
    def test_a_reissue_reports_what_moved(self, client, analysed):
        revised = client.post("/v1/documents/text", json={"text": REVISED}).json()

        result = client.get(
            f"/v1/documents/{revised['document_id']}/changes",
            params={"since": analysed["document_id"]},
        ).json()

        moved = next(c for c in result["changes"] if c["kind"] == "deadline_moved")
        assert moved["severity"] == "critical"
        assert result["headline"] == moved["summary"]
        assert result["warning"] is None

    def test_comparing_a_document_with_itself_is_rejected(self, client, analysed):
        response = client.get(
            f"/v1/documents/{analysed['document_id']}/changes",
            params={"since": analysed["document_id"]},
        )

        assert response.status_code == 422

    def test_comparing_against_an_unknown_document_returns_404(self, client, analysed):
        response = client.get(
            f"/v1/documents/{analysed['document_id']}/changes",
            params={"since": "nope"},
        )

        assert response.status_code == 404

    def test_two_documents_that_disagree_produce_a_conflict(self, client, analysed):
        circular = client.post("/v1/documents/text", json={"text": CIRCULAR}).json()

        result = client.post(
            "/v1/portfolio",
            json={"document_ids": [analysed["document_id"], circular["document_id"]]},
        ).json()

        assert not result["is_consistent"]
        assert result["conflicts"][0]["kind"] == "deadline"
        assert len(result["conflicts"][0]["positions"]) == 2

    def test_the_merged_timeline_attributes_every_step(self, client, analysed):
        circular = client.post("/v1/documents/text", json={"text": CIRCULAR}).json()
        ids = {analysed["document_id"], circular["document_id"]}

        result = client.post("/v1/portfolio", json={"document_ids": list(ids)}).json()

        assert {item["document_id"] for item in result["timeline"]} == ids

    def test_a_portfolio_needs_two_distinct_documents(self, client, analysed):
        document_id = analysed["document_id"]
        response = client.post(
            "/v1/portfolio", json={"document_ids": [document_id, document_id]}
        )

        assert response.status_code == 422


def test_store_evicts_the_oldest_beyond_capacity(client):
    """Memory stays bounded no matter how many documents are analysed."""
    from app.main import Store, store as live

    ids = [
        client.post(
            "/v1/documents/text",
            json={"text": f"Submit form {index} before 1 October 2026."},
        ).json()["document_id"]
        for index in range(3)
    ]

    small = Store(capacity=2)
    for document_id in ids:
        record = live.get(document_id)
        assert record is not None
        small.put(record)

    assert len(small.recent()) == 2
    assert small.get(ids[0]) is None
    assert small.get(ids[2]) is not None
