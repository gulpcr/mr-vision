"""Per-tenant JWT signing key derivation.

Tokens are signed with HMAC(master_secret, tenant_id) rather than the master
secret directly — a token minted for one tenant must be rejected if replayed
with a different tenant_id claim (the derived verification key won't match),
and a completely malformed/tampered token must never decode.
"""
from __future__ import annotations

import uuid

import pytest

from app.application.auth_service import AuthService, derive_tenant_jwt_secret


class TestDeriveTenantJwtSecret:
    def test_deterministic_for_same_tenant(self):
        assert derive_tenant_jwt_secret("hospital-a") == derive_tenant_jwt_secret("hospital-a")

    def test_distinct_across_tenants(self):
        assert derive_tenant_jwt_secret("hospital-a") != derive_tenant_jwt_secret("hospital-b")

    def test_output_does_not_leak_master_secret(self):
        from app.config import get_settings
        secret = derive_tenant_jwt_secret("hospital-a")
        assert get_settings().jwt_secret_key not in secret


class TestTokenRoundTrip:
    def test_valid_token_round_trips(self):
        svc = AuthService(session=None)
        token = svc.create_access_token(
            subject=str(uuid.uuid4()), username="alice", role="admin",
            tenant_id="hospital-a", is_platform_admin=False,
        )
        payload = svc.decode_token(token)
        assert payload is not None
        assert payload["tenant_id"] == "hospital-a"
        assert payload["username"] == "alice"

    def test_token_signed_for_one_tenant_is_not_forgeable_by_editing_the_claim(self):
        """Swapping the tenant_id claim in an otherwise-valid token must fail —
        the signature was computed over the ORIGINAL tenant_id, so re-verifying
        with the new claim's derived key can never match.
        """
        import jose.jwt as jose_jwt

        svc = AuthService(session=None)
        token = svc.create_access_token(
            subject=str(uuid.uuid4()), username="mallory", role="admin",
            tenant_id="hospital-a", is_platform_admin=False,
        )
        claims = jose_jwt.get_unverified_claims(token)
        assert claims["tenant_id"] == "hospital-a"

        # An attacker who edits the claim and re-signs with hospital-a's key
        # (the only key they could plausibly have obtained, e.g. via a leak
        # scoped to that tenant) produces a token whose signature no longer
        # verifies once decode_token derives hospital-b's key for the new claim.
        forged_claims = dict(claims)
        forged_claims["tenant_id"] = "hospital-b"
        forged_claims["is_platform_admin"] = True
        forged_token = jose_jwt.encode(
            forged_claims, derive_tenant_jwt_secret("hospital-a"), algorithm="HS256"
        )
        assert svc.decode_token(forged_token) is None

    def test_token_missing_tenant_id_claim_is_rejected(self):
        import jose.jwt as jose_jwt

        svc = AuthService(session=None)
        token = jose_jwt.encode(
            {"sub": "x", "username": "x", "role": "admin"},
            derive_tenant_jwt_secret("hospital-a"),
            algorithm="HS256",
        )
        assert svc.decode_token(token) is None

    def test_garbage_token_is_rejected(self):
        svc = AuthService(session=None)
        assert svc.decode_token("not.a.jwt") is None
        assert svc.decode_token("") is None
