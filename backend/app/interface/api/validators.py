from __future__ import annotations

import re

from fastapi import HTTPException

# DICOM UID: digits and dots, 2-64 chars, must start/end with digit
_DICOM_UID_RE = re.compile(r"^[0-9][0-9.]{0,62}[0-9]$")


def validate_dicom_uid(uid: str) -> str:
    """Validate a DICOM UID and return it unchanged, or raise HTTP 422."""
    if not _DICOM_UID_RE.match(uid):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid DICOM UID '{uid}'. "
                "Must contain only digits and dots, 2-64 characters, "
                "and start/end with a digit."
            ),
        )
    return uid
