"""Rotate the platform master key (JWT_SECRET_KEY) without losing protected data.

Every derive_secret() subkey comes from JWT_SECRET_KEY:
  * jwt:{tenant_id}             — access/viewer token signing → all sessions end (by design)
  * totp-secret-encryption-v1   — TOTP secrets at rest        → re-encrypted here
  * audit-chain-v1              — audit hash chain HMAC       → verified with the OLD key up
                                  to the cut-over seq printed here (set in .env)

Run inside the backend container while the OLD key is still configured, BEFORE switching
.env to the new key. Connects as the schema owner (POSTGRES_USER) so RLS does not hide
other tenants' users:

    docker compose exec -e NEW_JWT_SECRET_KEY=... backend python scripts/rotate_master_key.py [--apply]

Without --apply it only reports what it would do. With --apply it re-encrypts TOTP
secrets in one transaction and prints the two .env lines to add:
    AUDIT_CHAIN_PREVIOUS_MASTER_KEY=<old key>
    AUDIT_CHAIN_ROTATED_AFTER_SEQ=<cut-over seq>
Stop the API/worker (or accept that audit rows written between this run and the restart
fall on the old side of the cut-over — rerun just before the restart to be exact).
"""
from __future__ import annotations

import argparse
import base64
import os
import sys

import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import derive_secret_with, get_settings  # noqa: E402

_TOTP_PURPOSE = "totp-secret-encryption-v1"


def _fernet(master: str) -> Fernet:
    key = bytes.fromhex(derive_secret_with(master, _TOTP_PURPOSE))
    return Fernet(base64.urlsafe_b64encode(key))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write changes")
    args = parser.parse_args()

    settings = get_settings()
    old_key = settings.jwt_secret_key
    new_key = os.environ.get("NEW_JWT_SECRET_KEY", "")
    if len(new_key) < 32:
        print("NEW_JWT_SECRET_KEY must be set and at least 32 characters", file=sys.stderr)
        return 2
    if new_key == old_key:
        print("NEW_JWT_SECRET_KEY equals the current key; nothing to rotate", file=sys.stderr)
        return 2

    # Schema owner, not settings.database_url: that is the RLS-bound app role when
    # POSTGRES_APP_USER is set, which sees no users/audit rows outside a tenant scope.
    owner_url = (
        f"postgresql://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    )
    from app.infrastructure.tls import db_sync_url

    engine = sa.create_engine(db_sync_url(owner_url, settings), pool_pre_ping=True)
    old_f, new_f = _fernet(old_key), _fernet(new_key)

    with engine.begin() as conn:
        rows = conn.execute(
            sa.text("SELECT id, totp_secret FROM users WHERE totp_secret IS NOT NULL")
        ).all()
        re_encrypted: list[tuple[str, str]] = []
        unreadable: list[str] = []
        for user_id, token in rows:
            try:
                plain = old_f.decrypt(token.encode("utf-8"))
            except InvalidToken:
                unreadable.append(str(user_id))
                continue
            re_encrypted.append((str(user_id), new_f.encrypt(plain).decode("utf-8")))

        cutover = conn.execute(sa.text("SELECT COALESCE(MAX(seq), 0) FROM audit_log")).scalar()

        print(f"TOTP secrets: {len(re_encrypted)} re-encryptable, {len(unreadable)} unreadable")
        if unreadable:
            print("  unreadable (users must re-enrol MFA):", ", ".join(unreadable))
        print(f"Audit chain cut-over seq: {cutover}")

        if not args.apply:
            print("Dry run — rerun with --apply to write.")
            conn.rollback()
            return 0

        for user_id, token in re_encrypted:
            conn.execute(
                sa.text("UPDATE users SET totp_secret = :t WHERE id = :i"),
                {"t": token, "i": user_id},
            )

    print("Applied. Now set in .env (together with the new JWT_SECRET_KEY):")
    print(f"AUDIT_CHAIN_PREVIOUS_MASTER_KEY={old_key}")
    print(f"AUDIT_CHAIN_ROTATED_AFTER_SEQ={cutover}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
