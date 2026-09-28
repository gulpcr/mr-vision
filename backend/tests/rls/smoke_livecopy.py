"""Manual smoke test against a migrated copy of a real database (not collected by pytest).

Mints a token for the database's first platform-admin user and exercises the main read
paths through the app while it connects as the RLS-bound role.
"""
from __future__ import annotations

import os
import sys

import sqlalchemy as sa
from fastapi.testclient import TestClient

owner = sa.create_engine(os.environ["RLS_TEST_OWNER_DATABASE_URL"])
with owner.connect() as c:
    u = c.execute(sa.text(
        "SELECT id, username, role, tenant_id FROM users WHERE is_platform_admin ORDER BY created_at LIMIT 1"
    )).first()
owner.dispose()
if u is None:
    sys.exit("no platform admin in this database")

from app.application.auth_service import AuthService  # noqa: E402
from app.main import app  # noqa: E402

token = AuthService(session=None).create_access_token(
    subject=u.id, username=u.username, role=u.role, tenant_id=u.tenant_id, is_platform_admin=True,
)
h = {"Authorization": f"Bearer {token}"}
checks = [
    ("GET", "/api/auth/me/permissions", None),
    ("GET", "/api/studies?limit=200", None),
    ("GET", "/api/critical-alerts/stats", None),
    ("GET", "/api/admin/audit/verify", None),
    ("GET", "/api/dashboards", None),
    ("GET", "/api/tenant/current", None),
    ("GET", "/api/admin/platform/overview", None),
    ("GET", "/api/roles", None),
    ("GET", "/api/auth/users", None),
]
with TestClient(app) as client:
    for method, path, body in checks:
        r = client.request(method, path, headers=h, json=body)
        summary = r.text[:160].replace("\n", " ")
        if path.startswith("/api/studies"):
            summary = f"total={r.json().get('total')}"
        print(f"{r.status_code} {method} {path} :: {summary}")
    boards = client.get("/api/dashboards", headers=h).json()
    admin_board = next(b for b in boards if b["role_default"] == "admin")
    data = client.post(f"/api/dashboards/{admin_board['id']}/data", json={}, headers=h).json()["data"]
    errors = {k: v["error"] for k, v in data.items() if v.get("error")}
    print("dashboard widgets:", len(data), "errors:", errors or "none",
          "| kpi:", data.get("kpi", {}).get("data"))
