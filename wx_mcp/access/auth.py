"""
Sign-in tokens: JwtVerifier checks each request's bearer token against the
issuer's public keys (found by discovery), issuer, audience, and expiry. Standards
only (OAuth 2.0, OpenID Connect, JWT), so switching providers is a settings change.
"""

import logging
import anyio.to_thread
import httpx
import jwt
from mcp.server.auth.provider import AccessToken

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# where an issuer describes itself: OpenID Connect first (more providers serve it), then RFC 8414
DISCOVERY_PATHS = (
    "/.well-known/openid-configuration",
    "/.well-known/oauth-authorization-server",
)
ALGORITHMS = ["RS256"]
DISCOVERY_TIMEOUT_SECONDS = 10


class AuthError(Exception):
    pass


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def _get_json(url: str) -> dict:
    response = httpx.get(url, timeout=DISCOVERY_TIMEOUT_SECONDS, follow_redirects=True)
    response.raise_for_status()
    return response.json()


def discover(issuer: str) -> dict:
    """The issuer's metadata from the first discovery path that answers; it must name the same issuer."""
    problems = []
    for path in DISCOVERY_PATHS:
        try:
            meta = _get_json(issuer.rstrip("/") + path)
        except (httpx.HTTPError, ValueError) as exc:
            problems.append(f"{path}: {type(exc).__name__}")
            continue
        if meta.get("issuer") != issuer:
            raise AuthError(f"{issuer} describes itself as issuer {meta.get('issuer')!r}; check WX_MCP_ISSUER")
        if not meta.get("jwks_uri"):
            raise AuthError(f"{issuer}'s metadata has no jwks_uri")
        return meta
    raise AuthError(f"couldn't read {issuer}'s metadata ({'; '.join(problems)})")


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


class JwtVerifier:
    """The SDK's TokenVerifier: verify_token(token) -> AccessToken, or None to refuse it."""

    def __init__(self, issuer: str, resource: str):
        meta = discover(issuer)
        self.issuer, self.resource = issuer, resource
        # keys cached; an unknown key id (the issuer rotated keys) fetches them again
        self.keys = jwt.PyJWKClient(meta["jwks_uri"], cache_keys=True)

    async def verify_token(self, token: str) -> AccessToken | None:
        # fetching keys is blocking I/O
        return await anyio.to_thread.run_sync(self._verify, token)

    def _verify(self, token: str) -> AccessToken | None:
        try:
            key = self.keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=ALGORITHMS,
                issuer=self.issuer,
                audience=self.resource,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            # the reason's type only, never the token
            logger.info("token refused: %s", type(exc).__name__)
            return None
        # an agent's own token, not a person's (WorkOS marks these; other providers never set it)
        if claims.get("sub_profile") == "ai_agent":
            logger.info("token refused: agent token")
            return None
        return AccessToken(
            token=token,
            client_id=claims.get("client_id") or claims.get("azp") or "",
            scopes=claims.get("scope", "").split(),
            expires_at=claims["exp"],
            resource=self.resource,
            subject=claims["sub"],
            claims={"iss": claims["iss"]},
        )
