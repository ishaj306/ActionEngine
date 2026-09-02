"""Authentication and tenant isolation.

The isolation suite is the one that matters. Before it, `GET /v1/documents`
returned every document every user had ever uploaded, and people upload
marksheets and identity documents. A regression here is not a bug report, it is
a disclosure.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.auth import Principal, current_user, settings
from app.main import app, store

NOTICE = "Students must submit the form before 18 September 2026."


@pytest.fixture
def anonymous(authenticated, monkeypatch):
    """A client with no identity at all, and no development escape hatch."""
    app.dependency_overrides.clear()
    monkeypatch.delenv("CLERK_ISSUER", raising=False)
    monkeypatch.delenv("AUTH_DEV_USER", raising=False)
    settings.reload()
    yield TestClient(app)
    settings.reload()


def _call(client: TestClient, method: str, path: str):
    """GET and DELETE take no body in httpx; POST and PATCH need one here."""
    if method in {"post", "patch"}:
        return getattr(client, method)(path, json={})
    return getattr(client, method)(path)


def as_user(user_id: str) -> TestClient:
    app.dependency_overrides[current_user] = lambda: Principal(user_id=user_id)
    return TestClient(app)


class TestRefusesByDefault:
    """With nothing configured, the service refuses rather than serving openly.

    This is the single most important behaviour in the file. The failure mode
    it prevents is not an attack -- it is a deployment where someone forgot to
    set an environment variable, and nobody noticed because everything worked.
    """

    def test_health_stays_open(self, anonymous):
        """A load balancer has no token."""
        assert anonymous.get("/health").status_code == 200

    def test_health_says_auth_is_unconfigured(self, anonymous):
        assert anonymous.get("/health").json()["auth"] == "unconfigured"

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/v1/documents"),
            ("get", "/v1/documents/anything"),
            ("post", "/v1/documents/text"),
            ("patch", "/v1/documents/anything"),
            ("delete", "/v1/documents/anything"),
            ("delete", "/v1/documents"),
            ("get", "/v1/documents/anything/calendar.ics"),
            ("get", "/v1/documents/anything/enquiry"),
            ("get", "/v1/documents/anything/changes?since=other"),
            ("post", "/v1/portfolio"),
        ],
    )
    def test_every_document_route_refuses(self, anonymous, method, path):
        response = _call(anonymous, method, path)

        assert response.status_code == 503, f"{method.upper()} {path} was served"

    def test_the_refusal_explains_itself(self, anonymous):
        detail = anonymous.get("/v1/documents").json()["detail"]

        assert "not configured" in detail


class TestDevelopmentUser:
    def test_an_explicit_dev_user_is_honoured(self, authenticated, monkeypatch):
        app.dependency_overrides.clear()
        monkeypatch.setenv("AUTH_DEV_USER", "local")
        settings.reload()
        client = TestClient(app)

        assert client.post("/v1/documents/text", json={"text": NOTICE}).status_code == 201
        settings.reload()

    def test_it_is_reported_as_development_not_as_configured(
        self, authenticated, monkeypatch
    ):
        app.dependency_overrides.clear()
        monkeypatch.setenv("AUTH_DEV_USER", "local")
        settings.reload()

        assert TestClient(app).get("/health").json()["auth"] == "development"
        settings.reload()


class TestTenantIsolation:
    """One user must not be able to reach another's document by any route."""

    @pytest.fixture
    def theirs(self, authenticated):
        client = as_user("user_a")
        created = client.post("/v1/documents/text", json={"text": NOTICE}).json()
        return created["document_id"]

    @pytest.mark.parametrize(
        ("method", "suffix"),
        [
            ("get", ""),
            ("patch", ""),
            ("delete", ""),
            ("get", "/calendar.ics"),
            ("get", "/enquiry"),
        ],
    )
    def test_another_user_gets_404_on_every_route(self, theirs, method, suffix):
        intruder = as_user("user_b")

        response = _call(intruder, method, f"/v1/documents/{theirs}{suffix}")

        assert response.status_code == 404

    def test_a_probe_cannot_distinguish_theirs_from_nonexistent(self, theirs):
        """A 403 would confirm the id exists, which is what a probe wants."""
        intruder = as_user("user_b")

        real = intruder.get(f"/v1/documents/{theirs}")
        imaginary = intruder.get("/v1/documents/0123456789abcdef")

        assert real.status_code == imaginary.status_code == 404
        assert real.json() == imaginary.json()

    def test_listing_shows_only_your_own(self, theirs):
        mine = as_user("user_b")
        mine.post("/v1/documents/text", json={"text": "Submit by 1 October 2026."})

        listed = mine.get("/v1/documents").json()

        assert len(listed) == 1
        assert listed[0]["document_id"] != theirs

    def test_cross_document_comparison_cannot_reach_across_users(self, theirs):
        intruder = as_user("user_b")
        ours = intruder.post("/v1/documents/text", json={"text": NOTICE}).json()

        response = intruder.get(
            f"/v1/documents/{ours['document_id']}/changes?since={theirs}"
        )

        assert response.status_code == 404

    def test_the_portfolio_cannot_reach_across_users(self, theirs):
        intruder = as_user("user_b")
        ours = intruder.post("/v1/documents/text", json={"text": NOTICE}).json()

        response = intruder.post(
            "/v1/portfolio",
            json={"document_ids": [ours["document_id"], theirs]},
        )

        assert response.status_code == 404

    def test_deleting_everything_leaves_other_users_untouched(self, theirs):
        intruder = as_user("user_b")
        intruder.post("/v1/documents/text", json={"text": NOTICE})

        removed = intruder.delete("/v1/documents").json()["deleted"]

        assert removed == 1
        assert store.get(theirs, owner_id="user_a") is not None


class TestDocumentIds:
    def test_the_same_text_uploaded_twice_gets_different_ids(self, authenticated):
        """A content hash is a document id anyone can guess for a public notice."""
        client = as_user("user_a")

        first = client.post("/v1/documents/text", json={"text": NOTICE}).json()
        second = client.post("/v1/documents/text", json={"text": NOTICE}).json()

        assert first["document_id"] != second["document_id"]

    def test_ids_are_long_enough_not_to_be_enumerable(self, authenticated):
        client = as_user("user_a")
        created = client.post("/v1/documents/text", json={"text": NOTICE}).json()

        assert len(created["document_id"]) >= 32

    def test_an_id_survives_a_working_state_update(self, authenticated):
        """Ticking a step off must not re-key the document."""
        client = as_user("user_a")
        created = client.post("/v1/documents/text", json={"text": NOTICE}).json()

        updated = client.patch(
            f"/v1/documents/{created['document_id']}", json={"completed": []}
        ).json()

        assert updated["document_id"] == created["document_id"]


class TestHardening:
    def test_uploads_are_rate_limited_per_user(self, authenticated, monkeypatch):
        """One account should not be able to make the service unusable.

        Analysis is CPU-bound and synchronous, so a retry loop in somebody's
        script is enough -- no malice required.
        """
        from app.api.limits import uploads

        monkeypatch.setattr(uploads, "allowance", 3)
        client = as_user("user_busy")

        codes = [
            client.post("/v1/documents/text", json={"text": NOTICE}).status_code
            for _ in range(5)
        ]

        assert codes[:3] == [201, 201, 201]
        assert codes[3:] == [429, 429]

    def test_the_limit_says_when_to_come_back(self, authenticated, monkeypatch):
        from app.api.limits import uploads

        monkeypatch.setattr(uploads, "allowance", 1)
        client = as_user("user_busy2")
        client.post("/v1/documents/text", json={"text": NOTICE})

        refused = client.post("/v1/documents/text", json={"text": NOTICE})

        assert refused.status_code == 429
        assert int(refused.headers["Retry-After"]) > 0

    def test_one_users_limit_does_not_affect_another(self, authenticated, monkeypatch):
        from app.api.limits import uploads

        monkeypatch.setattr(uploads, "allowance", 1)
        as_user("user_one").post("/v1/documents/text", json={"text": NOTICE})

        second = as_user("user_two").post("/v1/documents/text", json={"text": NOTICE})

        assert second.status_code == 201

    def test_a_pdf_declaring_too_many_pages_is_refused(self, authenticated, monkeypatch):
        """A compressed page tree makes a small file expensive to parse.

        The 20 MB upload cap bounds bytes, not work: a few kilobytes can
        declare tens of thousands of pages.
        """
        import pypdf

        from app.modules.ingestion.document import (
            MAX_PDF_PAGES,
            UnsupportedDocument,
            parse,
        )

        class Fake:
            is_encrypted = False
            pages = [object()] * (MAX_PDF_PAGES + 1)

        monkeypatch.setattr(pypdf, "PdfReader", lambda _stream: Fake())

        with pytest.raises(UnsupportedDocument, match="pages"):
            parse(b"%PDF-1.4\n" + b"0" * 64, filename="huge.pdf")

    def test_the_upload_size_cap_is_enforced_before_reading(self, authenticated):
        import io

        client = as_user("user_big")
        response = client.post(
            "/v1/documents",
            files={"file": ("big.txt", io.BytesIO(b"x" * (21 * 1024 * 1024)), "text/plain")},
        )

        assert response.status_code == 413
