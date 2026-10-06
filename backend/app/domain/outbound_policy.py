"""Outbound transport policy (HIPAA 164.312(e) transmission security, 164.308(b)).

PHI may leave the platform only over TLS: every integration endpoint (webhooks, FHIR
servers) must be ``https://``. Plain ``http://`` is accepted only when the operator has
explicitly allowed it for local development.

In production the destination must also be on the operator's register of recipients
covered by a Business Associate Agreement (``OUTBOUND_ALLOWED_HOSTS``): an entry
``example.org`` allows exactly that host, ``.example.org`` allows its subdomains.
Pure functions, no I/O.
"""
from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit


class InsecureOutboundError(ValueError):
    """The destination URL would send data unencrypted."""


def parse_host_list(raw: str) -> tuple[str, ...]:
    """``"a.org, .b.org"`` -> ``("a.org", ".b.org")`` (lower-cased, blanks dropped)."""
    return tuple(h.strip().lower() for h in (raw or "").split(",") if h.strip())


def host_allowed(host: str, allowed_hosts: Iterable[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    for entry in allowed_hosts:
        if entry.startswith("."):
            if host.endswith(entry) and len(host) > len(entry):
                return True
        elif host == entry:
            return True
    return False


def check_outbound_url(
    url: str,
    allow_insecure: bool = False,
    allowed_hosts: Iterable[str] = (),
    require_listed: bool = False,
) -> str:
    """Return the URL if it is an acceptable destination, else raise.

    ``allowed_hosts`` (the BAA register) restricts destinations when non-empty;
    ``require_listed`` (production) refuses every destination while it is empty.
    """
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise InsecureOutboundError("Destination must be a full https:// URL")
    if parts.scheme != "https" and not allow_insecure:
        raise InsecureOutboundError(
            "Destination must use https:// (patient data is only sent encrypted)"
        )
    allowed = tuple(allowed_hosts)
    if (allowed or require_listed) and not host_allowed(parts.hostname, allowed):
        raise InsecureOutboundError(
            f"Destination host {parts.hostname} is not on the approved recipient list "
            "(OUTBOUND_ALLOWED_HOSTS: recipients covered by a Business Associate Agreement)"
        )
    return url.strip()


def url_host(url: str) -> str:
    """Host part only, for logging (never log full URLs: they may carry tokens)."""
    return urlsplit(url or "").hostname or ""
