from __future__ import annotations

"""TLS options for every internal connection (HIPAA 164.312(e) transmission security).

One place turns the settings (db_ssl_mode, redis_tls, minio_ca_cert, orthanc_ca_cert)
into the option each client library expects. Every setting defaults to the historical
plaintext behaviour so development and the current single-host deployment are
unchanged; docker-compose.prod.yml turns them on with the internal CA from
ops/pki/make-certs.sh.
"""

import ssl
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.config import Settings, get_settings


def _context(ca_file: str, verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=ca_file or None)
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def db_async_connect_args(settings: Settings | None = None) -> dict:
    """asyncpg ``connect_args`` for create_async_engine."""
    s = settings or get_settings()
    mode = (s.db_ssl_mode or "disable").lower()
    if mode == "disable":
        return {}
    return {"ssl": _context(s.db_ssl_root_cert, verify=(mode == "verify-full"))}


def db_sync_url(url: str, settings: Settings | None = None) -> str:
    """psycopg2 URL with sslmode / sslrootcert query parameters added."""
    s = settings or get_settings()
    mode = (s.db_ssl_mode or "disable").lower()
    if mode == "disable":
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["sslmode"] = mode
    if s.db_ssl_root_cert:
        query["sslrootcert"] = s.db_ssl_root_cert
    return urlunsplit(parts._replace(query=urlencode(query)))


def redis_kwargs(settings: Settings | None = None) -> dict:
    """Keyword arguments for redis.Redis / redis.asyncio.Redis."""
    s = settings or get_settings()
    kwargs: dict = {
        "host": s.redis_host, "port": s.redis_port, "password": s.redis_password or None,
    }
    if s.redis_tls:
        kwargs.update(ssl=True, ssl_cert_reqs="required", ssl_ca_certs=s.redis_ca_cert or None)
    return kwargs


def celery_ssl_options(settings: Settings | None = None) -> dict | None:
    """broker_use_ssl / redis_backend_use_ssl for Celery (None when TLS is off)."""
    s = settings or get_settings()
    if not s.redis_tls:
        return None
    opts: dict = {"ssl_cert_reqs": ssl.CERT_REQUIRED}
    if s.redis_ca_cert:
        opts["ssl_ca_certs"] = s.redis_ca_cert
    return opts


def httpx_verify(ca_file: str) -> bool | str:
    """``verify=`` for httpx: the internal CA bundle when set, else system trust."""
    return ca_file or True


def minio_http_client(settings: Settings | None = None):
    """urllib3 pool for the MinIO SDK that trusts the internal CA (None = SDK default)."""
    s = settings or get_settings()
    if not (s.minio_secure and s.minio_ca_cert):
        return None
    import certifi  # noqa: F401  (urllib3 dependency; ensures the module is present)
    import urllib3

    return urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=10, read=300),
        cert_reqs="CERT_REQUIRED", ca_certs=s.minio_ca_cert,
        retries=urllib3.Retry(total=3, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504]),
    )
