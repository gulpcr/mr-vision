from __future__ import annotations

from app.interface.middleware.tenant import _extract_subdomain, _resolve_tenant_slug

ROOT_DOMAIN = "mr-vision.ai"


class _FakeURL:
    def __init__(self, hostname: str | None):
        self.hostname = hostname


class _FakeRequest:
    """Just enough of Request's surface for _resolve_tenant_slug."""

    def __init__(self, hostname: str | None, headers: dict | None = None, query: dict | None = None):
        self.url = _FakeURL(hostname)
        self.headers = headers or {}
        self.query_params = query or {}


class TestExtractSubdomain:
    def test_extracts_subdomain(self):
        assert _extract_subdomain("hospital-a.mr-vision.ai", ROOT_DOMAIN) == "hospital-a"

    def test_ignores_www(self):
        assert _extract_subdomain("www.mr-vision.ai", ROOT_DOMAIN) is None

    def test_ignores_api_app_admin(self):
        for sub in ("api", "app", "admin"):
            assert _extract_subdomain(f"{sub}.mr-vision.ai", ROOT_DOMAIN) is None

    def test_bare_root_domain_has_no_subdomain(self):
        assert _extract_subdomain("mr-vision.ai", ROOT_DOMAIN) is None

    def test_unrelated_domain_returns_none(self):
        assert _extract_subdomain("hospital-a.evil.com", ROOT_DOMAIN) is None

    def test_ip_address_returns_none(self):
        assert _extract_subdomain("127.0.0.1", ROOT_DOMAIN) is None

    def test_empty_host_returns_none(self):
        assert _extract_subdomain("", ROOT_DOMAIN) is None

    def test_case_insensitive(self):
        assert _extract_subdomain("Hospital-A.MR-VISION.AI", ROOT_DOMAIN) == "hospital-a"


class TestResolveTenantSlug:
    def test_prefers_subdomain(self):
        req = _FakeRequest(
            hostname="hospital-a.mr-vision.ai",
            headers={"X-Tenant-Slug": "hospital-b"},
            query={"slug": "hospital-c"},
        )
        assert _resolve_tenant_slug(req, ROOT_DOMAIN) == "hospital-a"

    def test_falls_back_to_header(self):
        req = _FakeRequest(hostname="localhost", headers={"X-Tenant-Slug": "hospital-b"})
        assert _resolve_tenant_slug(req, ROOT_DOMAIN) == "hospital-b"

    def test_falls_back_to_query_param(self):
        req = _FakeRequest(hostname="localhost", query={"slug": "hospital-c"})
        assert _resolve_tenant_slug(req, ROOT_DOMAIN) == "hospital-c"

    def test_returns_none_when_unresolvable(self):
        req = _FakeRequest(hostname="localhost")
        assert _resolve_tenant_slug(req, ROOT_DOMAIN) is None

    def test_header_slug_lowercased_and_trimmed(self):
        req = _FakeRequest(hostname="localhost", headers={"X-Tenant-Slug": "  Hospital-B  "})
        assert _resolve_tenant_slug(req, ROOT_DOMAIN) == "hospital-b"
