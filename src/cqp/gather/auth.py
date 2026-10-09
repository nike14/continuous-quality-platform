"""Verifies the short-lived OIDC token GitLab issues to each CI job (`id_tokens`)."""

import jwt


class TokenVerifier:
    """Checks a CI job's token against GitLab's public keys."""

    def __init__(self, jwks_url: str, audience: str = "cqp"):
        self._jwks = jwt.PyJWKClient(
            jwks_url
        )  # caches GitLab's public keys; picks up rotations by kid
        self._audience = audience

    def __call__(self, token: str) -> dict:
        """Returns the token's claims.

        Raises if it's forged, expired or meant for someone else.
        """
        key = self._jwks.get_signing_key_from_jwt(token)
        # Pin the algorithm rather than trusting the token's own "alg" header.
        return jwt.decode(token, key.key, algorithms=["RS256"], audience=self._audience)
