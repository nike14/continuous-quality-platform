"""Local stand-in for GitLab's CI token. Creates a dev signing key once, then prints a token.

The private key stays in dev/private; dev-idp serves only dev/public/jwks.json to the APIs.
"""

import json
import secrets
import sys
import time
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

DEV = Path(__file__).parent.parent / "dev"
KEY = DEV / "private" / "key.pem"
JWKS = DEV / "public" / "jwks.json"
KID = "cqp-dev"


def signing_key() -> bytes:
    # Made once and reused, so earlier tokens stay valid and dev-idp keeps serving the same key.
    if not KEY.exists():
        KEY.parent.mkdir(parents=True, exist_ok=True)
        JWKS.parent.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        KEY.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        JWKS.write_text(json.dumps({"keys": [{**jwk, "kid": KID, "use": "sig", "alg": "RS256"}]}))
    return KEY.read_bytes()


def main() -> None:
    now = int(time.time())
    sha = sys.argv[1] if len(sys.argv) > 1 else "9f3c" * 10  # optional: the commit to claim
    role = sys.argv[2] if len(sys.argv) > 2 else None  # optional: approver | security
    project = sys.argv[3] if len(sys.argv) > 3 else "payments/payments-api"
    # The same claims GitLab puts in a CI job's id_token. cqp_role stands in for a group lookup.
    claims = {
        "aud": "cqp",
        "iat": now,
        "exp": now + 3600,
        "project_path": project,
        "sha": sha,
        "pipeline_id": 1,
        "job_id": 1000 + secrets.randbelow(99000),
        "ref_protected": "true",
        # A person with a role is someone else than the developer, as approvals need four eyes.
        "user_login": role or "dev",
    }
    if role:
        claims["cqp_role"] = role
    print(jwt.encode(claims, signing_key(), algorithm="RS256", headers={"kid": KID}))


if __name__ == "__main__":
    main()
