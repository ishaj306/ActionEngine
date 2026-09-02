"""Shared fixtures.

Every route below /health now requires a verified caller. The suites that test
extraction, export and comparison are not testing authentication, so they run as
a fixed principal supplied through FastAPI's dependency override -- which is
also a better test of the wiring than an environment variable would be, because
it exercises the real dependency graph.

`test_auth.py` clears the override and tests the genuine article.
"""

from __future__ import annotations

import pytest

from app.api.auth import Principal, current_user
from app.api.limits import requests as request_limit
from app.api.limits import uploads as upload_limit
from app.main import app, store

#: The principal every non-auth test runs as.
TEST_USER = "user_test_default"


@pytest.fixture(autouse=True)
def authenticated():
    """Run as a fixed signed-in user, and start from an empty store.

    Clearing the store between tests matters more now than it did: records are
    keyed by a random id and filtered by owner, so a leftover record from a
    previous test is invisible rather than merely stale, and an isolation bug
    would hide behind that invisibility.
    """
    app.dependency_overrides[current_user] = lambda: Principal(user_id=TEST_USER)
    store.clear()
    # Limits are per-user and per-process; without this a long suite exhausts
    # the hourly quota and later tests fail for a reason unrelated to them.
    upload_limit.reset()
    request_limit.reset()
    yield
    app.dependency_overrides.clear()
    store.clear()
