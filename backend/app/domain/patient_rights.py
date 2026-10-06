from __future__ import annotations

"""Individual rights: request types, the 30-day clock, restriction channels (PRV-04).

164.524(b)(2): act on a request for access within 30 days of receipt; one extension of
up to 30 more days if the individual is told, in writing within the first 30 days, why
and when. The register applies the same clock to amendment (164.526(b)(2): 60 days),
accounting (164.528(c): 60 days), restriction and confidential-communication requests.

164.522(a): a restriction the covered entity agrees to (or must accept: disclosures to
a health plan for items paid in full out of pocket) binds every later disclosure. The
platform enforces agreed restrictions per outbound channel.
"""

from datetime import datetime, timedelta

REQUEST_DAYS = {
    "access": 30,
    "amendment": 60,
    "accounting": 60,
    "restriction": 30,
    "confidential_communication": 30,
}
EXTENSION_DAYS = 30
OPEN_STATUSES = ("open", "extended")
CLOSED_STATUSES = ("fulfilled", "denied")

# Outbound channels a restriction can cover; "all" covers every one.
RESTRICTION_CHANNELS = ("fhir", "dicom_export", "webhooks", "share_links", "all")


class InvalidRequest(ValueError):
    pass


def due_date(request_type: str, received_at: datetime) -> datetime:
    if request_type not in REQUEST_DAYS:
        raise InvalidRequest(f"Unknown request type: {request_type}")
    return received_at + timedelta(days=REQUEST_DAYS[request_type])


def extended_due_date(request_type: str, received_at: datetime) -> datetime:
    return due_date(request_type, received_at) + timedelta(days=EXTENSION_DAYS)


def is_overdue(status: str, due_at: datetime, now: datetime) -> bool:
    return status in OPEN_STATUSES and now > due_at


def channel_covered(restricted: str, channel: str) -> bool:
    return restricted == channel or restricted == "all"
