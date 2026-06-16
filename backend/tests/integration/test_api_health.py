from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client():
    from app.main import create_app
    from app.interface.api import dependencies

    mock_registry = MagicMock()
    mock_registry.usecases = {}
    mock_routing = MagicMock()
    dependencies.set_registry(mock_registry)
    dependencies.set_routing_service(mock_routing)

    app = create_app()
    return TestClient(app)


class TestHealthEndpoint:
    @patch("app.interface.api.health._check_minio")
    @patch("app.interface.api.health._check_redis")
    @patch("app.interface.api.health._check_db")
    def test_all_healthy(self, mock_db, mock_redis, mock_minio, app_client):
        mock_db.return_value = {"status": "ok", "latency_ms": 1.0}
        mock_redis.return_value = {"status": "ok", "latency_ms": 0.5}
        mock_minio.return_value = {"status": "ok", "latency_ms": 2.0}

        resp = app_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["services"]["database"]["status"] == "ok"
        assert data["services"]["redis"]["status"] == "ok"
        assert data["services"]["minio"]["status"] == "ok"

    @patch("app.interface.api.health._check_minio")
    @patch("app.interface.api.health._check_redis")
    @patch("app.interface.api.health._check_db")
    def test_degraded_when_db_down(self, mock_db, mock_redis, mock_minio, app_client):
        mock_db.return_value = {"status": "error", "error": "connection refused"}
        mock_redis.return_value = {"status": "ok", "latency_ms": 0.5}
        mock_minio.return_value = {"status": "ok", "latency_ms": 2.0}

        resp = app_client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["services"]["database"]["status"] == "error"

    @patch("app.interface.api.health._check_minio")
    @patch("app.interface.api.health._check_redis")
    @patch("app.interface.api.health._check_db")
    def test_degraded_when_redis_down(self, mock_db, mock_redis, mock_minio, app_client):
        mock_db.return_value = {"status": "ok", "latency_ms": 1.0}
        mock_redis.return_value = {"status": "error", "error": "timeout"}
        mock_minio.return_value = {"status": "ok", "latency_ms": 2.0}

        resp = app_client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["status"] == "degraded"

    @patch("app.interface.api.health._check_minio")
    @patch("app.interface.api.health._check_redis")
    @patch("app.interface.api.health._check_db")
    def test_includes_version(self, mock_db, mock_redis, mock_minio, app_client):
        mock_db.return_value = {"status": "ok", "latency_ms": 1.0}
        mock_redis.return_value = {"status": "ok", "latency_ms": 0.5}
        mock_minio.return_value = {"status": "ok", "latency_ms": 2.0}

        resp = app_client.get("/health")
        assert resp.json()["version"] == "1.0.0"
