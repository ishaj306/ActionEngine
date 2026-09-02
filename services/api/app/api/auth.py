"""Who is asking.

Clerk issues an RS256 JWT to the browser; this verifies it against Clerk's
published JWKS and returns the subject. Nothing else in the application is
allowed to decide who the caller is -- in particular, no handler reads a user
id out of a request body or a header, because a client-supplied identity is not
an identity.

**The default is to refuse.** With no issuer configured, every authenticated
route returns 503 rather than falling through to an open service. That costs a
little friction in local development and buys the one guarantee worth having:
this cannot be deployed unauthenticated by forgetting to set something. The
escape hatch for local work is `AUTH_DEV_USER`, which has to be set on purpose,
is logged loudly on every startup that uses it, and names itself in the logs.

Google-only sign-in is a Clerk dashboard setting, not something this file can
enforce; `ALLOWED_EMAIL_DOMAINS` is available for the narrower case where a
deployment should also be restricted to one institution's addresses.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

logger = logging.getLogger("action_engine.auth")

__all__ = ["Principal", "current_user", "settings"]

#: Clock skew tolerated when checking `exp` and `nbf`, in seconds.
_LEEWAY = 30

#: How long a fetched key set is reused. Clerk rotates rarely; refetching on
#: every request would put a network call in front of every API call.
_JWKS_TTL = 3600


@dataclass(frozen=True, slots=True)
class Principal:
    """The verified caller."""

    user_id: str
    email: str | None = None

    def __str__(self) -> str:  # for log lines
        return self.user_id


class Settings:
    """Auth configuration, read once and re-readable in tests."""

    def __init__(self) -> None:
        self.reload()

    def reload(self) -> None:
        self.issuer = (os.getenv("CLERK_ISSUER") or "").rstrip("/")
        self.audience = os.getenv("CLERK_AUDIENCE") or None
        self.dev_user = os.getenv("AUTH_DEV_USER") or None
        self.allowed_domains = tuple(
            part.strip().lower()
            for part in (os.getenv("ALLOWED_EMAIL_DOMAINS") or "").split(",")
            if part.strip()
        )
        self._jwks: PyJWKClient | None = None
        self._jwks_fetched_at = 0.0

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"

    @property
    def is_configured(self) -> bool:
        return bool(self.issuer)

    def jwks(self) -> PyJWKClient:
        now = time.monotonic()
        if self._jwks is None or now - self._jwks_fetched_at > _JWKS_TTL:
            self._jwks = PyJWKClient(self.jwks_url, cache_keys=True)
            self._jwks_fetched_at = now
        return self._jwks


settings = Settings()

#: `auto_error=False` so a missing header produces our 401 with a remedy rather
#: than FastAPI's bare 403.
_bearer = HTTPBearer(auto_error=False)


def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    """Verify the bearer token and return the caller."""
    if settings.dev_user:
        # Logged every time, not once: a warning nobody sees is decoration.
        logger.warning(
            "AUTH_DEV_USER is set -- serving %s as %r with no token verification. "
            "This must never be set in a deployed environment.",
            request.url.path,
            settings.dev_user,
        )
        return Principal(user_id=settings.dev_user)

    if not settings.is_configured:
        raise HTTPException(
            status_code=503,
            detail=(
                "Authentication is not configured, so this service is refusing "
                "requests rather than serving them to anyone."
            ),
        )

    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="A bearer token is required.")

    return _verify(credentials.credentials)


def _verify(token: str) -> Principal:
    try:
        key = settings.jwks().get_signing_key_from_jwt(token).key
    except Exception as exc:  # network, malformed header, unknown kid
        logger.warning("could not resolve a signing key: %s", exc)
        raise HTTPException(status_code=401, detail="Token signature could not be checked.") from exc

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=settings.issuer,
            audience=settings.audience,
            leeway=_LEEWAY,
            options={
                "require": ["exp", "iat", "sub"],
                "verify_aud": settings.audience is not None,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="Session expired. Sign in again.") from exc
    except jwt.InvalidTokenError as exc:
        # Deliberately not echoed to the caller: the reason a token failed is
        # useful to an attacker and useless to a legitimate user.
        logger.warning("rejected a token: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid token.") from exc

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise HTTPException(status_code=401, detail="Token carries no subject.")

    email = claims.get("email")
    email = email if isinstance(email, str) else None
    _check_domain(email)
    return Principal(user_id=subject, email=email)


def _check_domain(email: str | None) -> None:
    if not settings.allowed_domains:
        return
    domain = email.rsplit("@", 1)[-1].lower() if email and "@" in email else ""
    if domain not in settings.allowed_domains:
        raise HTTPException(
            status_code=403,
            detail="This deployment is restricted to a specific email domain.",
        )
