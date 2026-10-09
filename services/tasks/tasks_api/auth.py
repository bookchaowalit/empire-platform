"""Verify platform-issued bearer tokens and turn them into a principal.

Tokens are short-lived RS256 JWTs issued by the platform after the human
auth provider (or the agent runtime) has authenticated the caller. Claims:

- ``sub``: identity performing the request
- ``ws``: workspace the request is bound to
- ``del`` / ``obo``: delegation and the identity it acts for (required together)
- ``act.sub``: initiating identity when it differs from ``sub`` (RFC 8693)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import jwt

AUDIENCE = "tasks-api"
MAX_LIFETIME_SECONDS = 3600  # tokens without a delegation, in every mode
HARD_MAX_LIFETIME_SECONDS = 30 * 86400  # no configuration may allow more than this
CHANNELS = {"api", "mcp", "workflow", "cli", "event"}


class Unauthenticated(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    actor: uuid.UUID
    workspace: uuid.UUID
    initiated_by: uuid.UUID
    on_behalf_of: uuid.UUID
    delegation: uuid.UUID | None
    channel: str
    scopes: frozenset[str] | None = None  # set only on exchanged tokens; narrows, never widens


@dataclass(frozen=True)
class TokenVerifier:
    public_key_pem: str
    issuer: str
    audience: str = AUDIENCE
    max_lifetime: int = MAX_LIFETIME_SECONDS  # delegated tokens only (hosted: up to 30 days); every call re-checks the delegation

    def __post_init__(self) -> None:
        if not 0 < self.max_lifetime <= HARD_MAX_LIFETIME_SECONDS:
            raise ValueError(f"max_lifetime must be between 1 and {HARD_MAX_LIFETIME_SECONDS} seconds")

    def verify(self, authorization: str | None, channel: str | None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise Unauthenticated("missing bearer token")
        try:
            claims = jwt.decode(
                authorization.removeprefix("Bearer "),
                self.public_key_pem,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "sub", "jti"]},
            )
            lifetime = claims["exp"] - claims["iat"]
            # A token without a delegation (owner token) has no revocation record: always short-lived.
            limit = self.max_lifetime if claims.get("del") else min(self.max_lifetime, MAX_LIFETIME_SECONDS)
            if lifetime > limit:
                raise Unauthenticated("token lifetime too long")
            actor = uuid.UUID(claims["sub"])
            workspace = uuid.UUID(claims["ws"])
            delegation = uuid.UUID(claims["del"]) if claims.get("del") else None
            on_behalf_of = uuid.UUID(claims["obo"]) if claims.get("obo") else None
            initiated_by = uuid.UUID(claims["act"]["sub"]) if claims.get("act") else actor
        except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
            raise Unauthenticated("invalid token") from exc
        if (delegation is None) != (on_behalf_of is None):
            raise Unauthenticated("delegation and obo must be given together")
        channel = channel or "api"
        if channel not in CHANNELS:
            raise Unauthenticated("unknown channel")
        scopes = frozenset(str(claims["scope"]).split()) if claims.get("scope") else None
        return Principal(
            scopes=scopes,
            actor=actor,
            workspace=workspace,
            initiated_by=initiated_by,
            on_behalf_of=on_behalf_of or actor,
            delegation=delegation,
            channel=channel,
        )
