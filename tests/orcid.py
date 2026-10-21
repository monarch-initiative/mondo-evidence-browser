"""A stand-in for ORCID's OpenID Connect keys, for tests.

ORCID is an external system. Tests sign id_tokens with a locally generated RSA key
and serve the matching key set from ``{ORCID_BASE}/oauth/jwks`` through an
``httpx.MockTransport``. Token verification itself runs for real (PyJWT).
"""

import json
import time
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ORCID_BASE = "https://sandbox.orcid.org"
CLIENT_ID = "APP-TESTCLIENT"
CARBERRY = "0000-0002-1825-0097"
STRANGER = "0000-0001-0000-0009"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    jwk: dict[str, Any] = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


def id_token(
    sub: str = CARBERRY,
    *,
    aud: str = CLIENT_ID,
    iss: str = ORCID_BASE,
    expires_in: int = 600,
    forged: bool = False,
    **claims: Any,
) -> str:
    """An id_token as ORCID would issue it (``forged`` signs with an unknown key)."""
    now = int(time.time())
    payload = {
        "sub": sub,
        "aud": aud,
        "iss": iss,
        "iat": now,
        "exp": now + expires_in,
        "given_name": "Josiah",
        "family_name": "Carberry",
        **claims,
    }
    key = _OTHER_KEY if forged else _KEY
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "test-key"})


def jwks_endpoint() -> httpx.MockTransport:
    """ORCID's key set endpoint, publishing the test key."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) != f"{ORCID_BASE}/oauth/jwks":
            return httpx.Response(404)
        return httpx.Response(200, json={"keys": [_jwk(_KEY, "test-key")]})

    return httpx.MockTransport(handler)
