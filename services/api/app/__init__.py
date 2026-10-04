"""Load local .env files before anything reads configuration.

The API reads configuration straight from the process environment -- a key, an
issuer URL, a database URL. In a deployment those are real environment
variables and this does nothing. In local development they live in a .env file,
and without this the file is inert: you set GEMINI_API_KEY, run the server, and
the model arm silently stays off because nothing ever read the file.

Three rules keep it safe:

- Real environment variables always win (`override=False`). Production and CI
  are never overridden by a stray file on a developer's disk.
- It does nothing under pytest, so the suite's own environment -- set through
  fixtures and monkeypatch -- is never disturbed by whatever .env a developer
  happens to have.
- A `.env.local` is read before a plain `.env`, and a file beside the API
  before one at the repo root, so the most specific value wins.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _load_local_env() -> None:
    if "pytest" in sys.modules:
        return
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        return

    here = Path(__file__).resolve()
    api_dir = here.parents[1]          # app/ -> services/api
    repo_root = here.parents[3]        # services/api -> services -> repo root

    # First match wins, because load_dotenv runs with override=False.
    for base in (api_dir, repo_root):
        for name in (".env.local", ".env"):
            candidate = base / name
            if candidate.is_file():
                load_dotenv(candidate, override=False)


_load_local_env()
