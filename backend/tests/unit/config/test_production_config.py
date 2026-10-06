"""PRODUCTION_MODE startup guard and the external-AI BAA gate."""
from __future__ import annotations

from app.config import Settings, config_warnings, insecure_config_problems, production_config_problems

_SECURE_BASE = dict(
    auth_mode="jwt",
    jwt_secret_key="k" * 40,
    phi_hash_salt="s" * 20,
    orthanc_password="o" * 20,
    orthanc_webhook_secret="w" * 20,
    redis_password="r" * 20,
)

_PROD_OK = dict(
    _SECURE_BASE,
    production_mode=True,
    db_ssl_mode="verify-full",
    db_ssl_root_cert="/run/pki/ca.crt",
    redis_tls=True,
    celery_broker_url="rediss://:x@redis:6379/0",
    celery_result_backend="rediss://:x@redis:6379/1",
    minio_secure=True,
    orthanc_scheme="https",
    minio_kms_configured=True,
    orthanc_storage_encrypted=True,
    require_rls=True,
    viewer_cookie_secure=True,
    api_docs_enabled=False,
    debug_routes_enabled=False,
    allow_insecure_outbound=False,
    allowed_origins="https://pacs.example.org",
    mfa_required_roles="*",
    require_tenant_baa=True,
    require_signed_for_export=True,
    phi_deidentify_enabled=True,
    viewer_minimum_necessary=True,
    audit_archive_enabled=True,
)


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_fully_configured_production_passes():
    assert insecure_config_problems(_settings(**_PROD_OK)) == []


def test_production_checks_only_apply_in_production_mode():
    s = _settings(**_SECURE_BASE)  # dev defaults: plaintext everywhere
    assert not s.production_mode
    assert insecure_config_problems(s) == []
    assert production_config_problems(s)  # would fail if production_mode were on


def test_each_missing_control_is_reported():
    cases = {
        "db_ssl_mode": "require",
        "redis_tls": False,
        "celery_broker_url": "redis://:x@redis:6379/0",
        "minio_secure": False,
        "orthanc_scheme": "http",
        "minio_kms_configured": False,
        "orthanc_storage_encrypted": False,
        "require_rls": False,
        "viewer_cookie_secure": False,
        "allow_insecure_outbound": True,
        "allowed_origins": "https://a.org,http://b.org",
        "mfa_required_roles": "admin,radiologist",
        "require_tenant_baa": False,
        "require_signed_for_export": False,
        "phi_deidentify_enabled": False,
        "viewer_minimum_necessary": False,
        "audit_archive_enabled": False,
    }
    for field, bad in cases.items():
        problems = insecure_config_problems(_settings(**{**_PROD_OK, field: bad}))
        assert len(problems) == 1, (field, problems)


def test_problem_messages_never_contain_values():
    s = _settings(**{**_PROD_OK, "celery_broker_url": "redis://:SECRETPW@redis:6379/0"})
    assert not any("SECRETPW" in p for p in insecure_config_problems(s))


def test_external_ai_needs_baa_declaration_in_production():
    dev = _settings(gemini_api_key="k", production_mode=False)
    assert dev.external_ai_allowed  # live/dev behaviour unchanged
    prod = _settings(gemini_api_key="k", production_mode=True)
    assert not prod.external_ai_allowed
    assert any("EXTERNAL_AI_BAA_CONFIRMED" in w for w in config_warnings(prod))
    declared = _settings(gemini_api_key="k", production_mode=True, external_ai_baa_confirmed=True)
    assert declared.external_ai_allowed
    assert not _settings(production_mode=True, external_ai_baa_confirmed=True).external_ai_allowed


def test_outbound_policy_kwargs_follow_settings():
    s = _settings(production_mode=True, outbound_allowed_hosts="fhir.h.org, .v.com")
    assert s.outbound_policy == {
        "allow_insecure": False,
        "allowed_hosts": ("fhir.h.org", ".v.com"),
        "require_listed": True,
    }
