"""TokenVerifier with a key made in the test, served as a key set the way GitLab serves it."""

import json
import time

import jwt
import pytest
from cqp.gather.auth import TokenVerifier
from cryptography.hazmat.primitives.asymmetric import rsa

KID = "test-key"


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def verifier(key, monkeypatch):
    v = TokenVerifier("https://gitlab.example/oauth/discovery/keys")
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    # Serve the public key from memory instead of fetching it over the network.
    monkeypatch.setattr(
        v._jwks, "fetch_data", lambda: {"keys": [{**jwk, "kid": KID, "alg": "RS256", "use": "sig"}]}
    )
    return v


def claims(aud="cqp", expires_in=3600):
    now = int(time.time())
    return {
        "aud": aud,
        "iat": now,
        "exp": now + expires_in,
        "project_path": "payments/payments-api",
        "sha": "DUMMY_9f3c",
    }


def sign(signing_key, body):
    return jwt.encode(body, signing_key, algorithm="RS256", headers={"kid": KID})


def test_valid_token_returns_its_claims(verifier, key):
    assert verifier(sign(key, claims()))["project_path"] == "payments/payments-api"


def test_token_for_another_audience_is_rejected(verifier, key):
    with pytest.raises(jwt.InvalidAudienceError):
        verifier(sign(key, claims(aud="someone-else")))


def test_expired_token_is_rejected(verifier, key):
    with pytest.raises(jwt.ExpiredSignatureError):
        verifier(sign(key, claims(expires_in=-60)))


def test_token_signed_with_another_key_is_rejected(verifier):
    # Same key ID, different key: what a forged token would look like.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(jwt.InvalidSignatureError):
        verifier(sign(other, claims()))


def test_unsigned_token_is_rejected(verifier):
    # The algorithm is pinned to RS256, so a token claiming "none" never gets through.
    unsigned = jwt.encode(claims(), "", algorithm="none", headers={"kid": KID})
    with pytest.raises(jwt.PyJWTError):
        verifier(unsigned)
