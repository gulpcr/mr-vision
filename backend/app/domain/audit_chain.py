"""Hash-chain computation for the tamper-evident audit log.

Pure domain module (stdlib only) — shared by PgAuditRepository (writes the
chain) and AuditIntegrityService (verifies it), so the two can never drift.
"""
from __future__ import annotations

import hmac
import hashlib
import json
from typing import Any


def compute_audit_row_hash(
    *,
    secret: str,
    seq: int,
    action: str,
    entity_type: str,
    entity_id: str,
    actor: str | None,
    details: dict[str, Any] | None,
    tenant_id: str | None,
    prev_hash: str | None,
) -> str:
    """Deterministic HMAC-SHA256 digest chaining this row to its predecessor.

    HMAC-keyed (not a plain hash) so that tamper-evidence holds even against
    someone with direct DB write access: recomputing a self-consistent forged
    chain requires the secret, not just the public hash function. `secret`
    should be a purpose-derived subkey (see app.config.derive_secret), not the
    platform master secret directly.

    Excludes the DB-assigned `timestamp` (server_default=func.now(), not known
    client-side before insert) — tamper-evidence covers the row's meaning
    (action/entity/actor/details/tenant) and its position in the chain
    (seq + prev_hash), not wall-clock time. Editing any covered field on any
    row, or splicing/reordering/deleting rows, changes a hash somewhere in the
    chain from that point forward — detectable by re-walking and recomputing.
    """
    canonical = json.dumps(
        {
            "seq": seq,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "actor": actor,
            "details": details or {},
            "tenant_id": tenant_id,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        default=str,
    )
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
