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


def test_store_evicts_the_oldest_beyond_capacity(client):
    """Memory stays bounded no matter how many documents are analysed."""
    from app.api.schemas import AnalysisOut
    from app.main import Store

    small = Store(capacity=2)
    analyses = [
        AnalysisOut(
            **client.post(
                "/v1/documents/text",
                json={"text": f"Submit form {index} before 1 October 2026."},
            ).json()
        )
        for index in range(3)
    ]
    for analysis in analyses:
        small.put(analysis)

    assert len(small.recent()) == 2
    assert small.get(analyses[0].document_id) is None
    assert small.get(analyses[2].document_id) is not None
