"""The persistence layer, run twice.

Every contract test here executes against both the in-process store and a real
SQL one. A persistence layer tested only in the configuration nobody deploys is
not tested, and the two implementations disagreeing quietly is exactly the class
of bug that reaches production intact.

SQLite stands in for Postgres. It is the same SQLAlchemy code path and the same
SQL; what it does not exercise is Postgres-specific behaviour under concurrency,
which is stated here rather than pretended away.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.auth import Principal, current_user
from app.main import app
from app.modules.ingestion.document import from_text
from app.modules.reasoning.relevance import Profile
from app.store import MemoryStore, SqlStore, StoredDocument, build_store

NOTICE = "Students must submit the form before 18 September 2026."


@pytest.fixture(params=["memory", "sql"])
def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryStore()
    else:
        yield SqlStore(f"sqlite:///{tmp_path / 'test.db'}")


def make(
    document_id: str,
    owner_id: str,
    *,
    text: str = NOTICE,
    filename: str = "notice.txt",
    completed: frozenset[str] = frozenset(),
    profile: Profile | None = None,
) -> StoredDocument:
    return StoredDocument(
        id=document_id,
        owner_id=owner_id,
        filename=filename,
        content_hash="deadbeef",
        document=from_text(text),
        completed=completed,
        profile=profile,
    )


class TestContract:
    def test_a_document_round_trips(self, store):
        store.put(make("d1", "alice"))
        found = store.get("d1", owner_id="alice")

        assert found is not None
        assert found.document.text == NOTICE
        assert found.filename == "notice.txt"

    def test_the_page_map_survives(self, store):
        """Offsets are what every citation in the product is built on."""
        text = "Page one text here.\f\fPage two text here."
        store.put(make("d1", "alice", text=text))

        found = store.get("d1", owner_id="alice")

        assert found is not None
        assert len(found.document.pages) == len(from_text(text).pages)
        for page in found.document.pages:
            assert found.document.text[page.char_start : page.char_end] == page.text

    def test_another_owner_cannot_read_it(self, store):
        store.put(make("d1", "alice"))

        assert store.get("d1", owner_id="bob") is None

    def test_a_missing_document_is_none(self, store):
        assert store.get("nope", owner_id="alice") is None

    def test_listing_is_scoped_to_the_owner(self, store):
        store.put(make("d1", "alice"))
        store.put(make("d2", "bob"))

        assert [item.id for item in store.recent(owner_id="alice")] == ["d1"]

    def test_listing_is_most_recent_first(self, store):
        for index in range(3):
            store.put(make(f"d{index}", "alice"))

        assert [item.id for item in store.recent(owner_id="alice")] == ["d2", "d1", "d0"]

    def test_listing_is_bounded(self, store):
        for index in range(30):
            store.put(make(f"d{index}", "alice"))

        assert len(store.recent(owner_id="alice", limit=5)) == 5

    def test_completion_state_survives(self, store):
        store.put(make("d1", "alice", completed=frozenset({"action-1", "action-2"})))

        found = store.get("d1", owner_id="alice")

        assert found is not None
        assert found.completed == {"action-1", "action-2"}

    def test_a_profile_survives_with_its_types(self, store):
        """Round-tripping through JSON must not turn 72.5 into a string."""
        store.put(
            make("d1", "alice", profile=Profile(year=3, programme="CSE", score=72.5))
        )

        found = store.get("d1", owner_id="alice")

        assert found is not None
        assert found.profile == Profile(year=3, programme="CSE", score=72.5)

    def test_an_absent_profile_stays_absent(self, store):
        store.put(make("d1", "alice"))
        found = store.get("d1", owner_id="alice")

        assert found is not None
        assert found.profile is None

    def test_putting_the_same_id_updates_the_state(self, store):
        store.put(make("d1", "alice"))
        store.put(make("d1", "alice", completed=frozenset({"action-1"})))

        found = store.get("d1", owner_id="alice")

        assert found is not None
        assert found.completed == {"action-1"}
        assert len(store.recent(owner_id="alice")) == 1

    def test_deleting_is_scoped_to_the_owner(self, store):
        store.put(make("d1", "alice"))

        assert store.delete("d1", owner_id="bob") is False
        assert store.get("d1", owner_id="alice") is not None

    def test_deleting_removes_it(self, store):
        store.put(make("d1", "alice"))

        assert store.delete("d1", owner_id="alice") is True
        assert store.get("d1", owner_id="alice") is None

    def test_deleting_everything_is_scoped_to_the_owner(self, store):
        store.put(make("d1", "alice"))
        store.put(make("d2", "alice"))
        store.put(make("d3", "bob"))

        assert store.delete_all(owner_id="alice") == 2
        assert store.get("d3", owner_id="bob") is not None

    def test_text_confidence_survives(self, store):
        """OCR confidence caps every claim, so losing it inflates them all."""
        document = from_text(NOTICE)
        stored = StoredDocument(
            id="d1",
            owner_id="alice",
            filename="scan.pdf",
            content_hash="x",
            document=type(document)(
                text=document.text,
                pages=document.pages,
                source_kind=document.source_kind,
                text_confidence=0.61,
                ocr_engine="tesseract",
            ),
        )
        store.put(stored)

        found = store.get("d1", owner_id="alice")

        assert found is not None
        assert found.document.text_confidence == 0.61
        assert found.document.ocr_engine == "tesseract"


class TestMemoryEviction:
    def test_the_oldest_is_evicted_beyond_capacity(self):
        """Memory stays bounded no matter how many documents are analysed."""
        small = MemoryStore(capacity=2)
        for index in range(3):
            small.put(make(f"d{index}", "alice"))

        assert len(small.recent(owner_id="alice")) == 2
        assert small.get("d0", owner_id="alice") is None
        assert small.get("d2", owner_id="alice") is not None


class TestSelection:
    def test_no_database_url_selects_memory(self, monkeypatch):
        """An unconfigured database costs history; unconfigured auth costs privacy.

        Only one of those is safe to guess at, which is why this falls back and
        authentication refuses.
        """
        monkeypatch.delenv("DATABASE_URL", raising=False)

        assert isinstance(build_store(), MemoryStore)

    def test_a_url_selects_sql(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'x.db'}")

        assert isinstance(build_store(), SqlStore)

    def test_a_heroku_style_url_is_normalised(self, monkeypatch):
        """postgres:// has no driver, and SQLAlchemy refuses it outright."""
        monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@host/db")

        with pytest.raises(Exception) as caught:
            build_store()

        # The driver is absent in this environment, which is fine: what matters
        # is that the URL was rewritten before SQLAlchemy saw it.
        assert "psycopg" in str(caught.value).lower()


class TestThroughTheApi:
    """The whole HTTP surface, against a real database rather than a dict."""

    @pytest.fixture
    def sql_client(self, authenticated, tmp_path, monkeypatch):
        import app.main as main

        monkeypatch.setattr(main, "store", SqlStore(f"sqlite:///{tmp_path / 'api.db'}"))
        app.dependency_overrides[current_user] = lambda: Principal(user_id="alice")
        return TestClient(app)

    def test_a_document_survives_upload_and_retrieval(self, sql_client):
        created = sql_client.post("/v1/documents/text", json={"text": NOTICE}).json()

        fetched = sql_client.get(f"/v1/documents/{created['document_id']}")

        assert fetched.status_code == 200
        assert fetched.json()["primary_deadline"]["value"] == "2026-09-18"

    def test_completion_survives_a_round_trip(self, sql_client):
        created = sql_client.post("/v1/documents/text", json={"text": NOTICE}).json()
        first = created["actions"][0]["id"]

        sql_client.patch(
            f"/v1/documents/{created['document_id']}", json={"completed": [first]}
        )
        again = sql_client.get(f"/v1/documents/{created['document_id']}").json()

        assert again["completed"] == [first]

    def test_a_profile_survives_a_round_trip(self, sql_client):
        text = "Eligible third year students must submit the form."
        created = sql_client.post("/v1/documents/text", json={"text": text}).json()

        sql_client.patch(
            f"/v1/documents/{created['document_id']}", json={"profile": {"year": 3}}
        )
        again = sql_client.get(f"/v1/documents/{created['document_id']}").json()

        assert again["relevance_verdict"] == "applies"

    def test_isolation_holds_against_the_database(self, sql_client):
        created = sql_client.post("/v1/documents/text", json={"text": NOTICE}).json()
        app.dependency_overrides[current_user] = lambda: Principal(user_id="mallory")

        assert sql_client.get(f"/v1/documents/{created['document_id']}").status_code == 404
        assert sql_client.get("/v1/documents").json() == []

    def test_evidence_offsets_still_index_the_text(self, sql_client):
        """The page map is rebuilt from the row; a wrong rebuild breaks citations."""
        created = sql_client.post("/v1/documents/text", json={"text": NOTICE}).json()
        fetched = sql_client.get(f"/v1/documents/{created['document_id']}").json()

        text = fetched["text"]
        for claim in fetched["deadlines"]:
            span = claim["evidence"]
            assert text[span["char_start"] : span["char_end"]] == span["text"]
