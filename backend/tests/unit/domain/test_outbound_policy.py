"""Outbound transport policy: PHI leaves only over https."""
from __future__ import annotations

import pytest

from app.domain.outbound_policy import InsecureOutboundError, check_outbound_url, url_host


def test_https_is_accepted():
    assert check_outbound_url("https://hooks.example.org/x?token=1") == "https://hooks.example.org/x?token=1"


@pytest.mark.parametrize("url", ["http://hooks.example.org/x", "ftp://x.example", "hooks.example.org", ""])
def test_everything_else_is_refused(url):
    with pytest.raises(InsecureOutboundError):
        check_outbound_url(url)


def test_http_allowed_only_when_explicitly_enabled():
    assert check_outbound_url("http://localhost:9000/hook", allow_insecure=True)


def test_url_host_never_returns_path_or_query():
    assert url_host("https://hooks.example.org/secret-path?token=abc") == "hooks.example.org"


# ── BAA register (OUTBOUND_ALLOWED_HOSTS) ──────────────────────────────────────

def test_parse_host_list_normalises():
    from app.domain.outbound_policy import parse_host_list

    assert parse_host_list(" FHIR.Hospital.org, .vendor.com ,,") == ("fhir.hospital.org", ".vendor.com")


def test_listed_host_and_subdomain_entries():
    allowed = ("fhir.hospital.org", ".vendor.com")
    assert check_outbound_url("https://fhir.hospital.org/R4", allowed_hosts=allowed)
    assert check_outbound_url("https://hooks.vendor.com/x", allowed_hosts=allowed)
    for url in ("https://evil.org/x", "https://vendor.com.evil.org/x", "https://xfhir.hospital.org/"):
        with pytest.raises(InsecureOutboundError):
            check_outbound_url(url, allowed_hosts=allowed)
    # ".vendor.com" covers subdomains only, not the bare domain.
    with pytest.raises(InsecureOutboundError):
        check_outbound_url("https://vendor.com/x", allowed_hosts=allowed)


def test_production_with_empty_register_refuses_everything():
    with pytest.raises(InsecureOutboundError):
        check_outbound_url("https://fhir.hospital.org/R4", require_listed=True)


def test_empty_register_outside_production_allows_any_https_host():
    assert check_outbound_url("https://anything.example/x", allowed_hosts=(), require_listed=False)
