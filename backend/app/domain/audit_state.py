from __future__ import annotations

"""Previous / new state of a changed record, as hashes (TEC-07). Pure.

A state-changing audit entry carries ``state_before_sha256`` / ``state_after_sha256``:
canonical SHA-256 of the record before and after the change. The values themselves may be
PHI and are not copied into the log; the hashes prove what changed and let a later copy of
the record be matched to the audit trail. Both hashes are inside the entry's chained
row hash, so they are tamper-evident too.
"""

import hashlib
import json
from typing import Any


def state_sha256(state: Any) -> str | None:
    if state is None:
        return None
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def state_change_details(before: Any = None, after: Any = None) -> dict[str, str]:
    out = {}
    if before is not None:
        out["state_before_sha256"] = state_sha256(before)
    if after is not None:
        out["state_after_sha256"] = state_sha256(after)
    return out
