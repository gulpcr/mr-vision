from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.domain.models import UseCase


@pytest.fixture
def app_client():
    from app.main import create_app
    from app.interface.api import dependencies

    app = create_app()

    sample_uc = UseCase(
        name="brain_mri",
        version="1.0.0",
        supported_body_parts=["BRAIN"],
        required_sequences=["T1"],
        model_type="segresnet",
    )

    mock_registry = MagicMock()
    mock_registry.usecases = {"brain_mri": sample_uc}
    mock_registry.get_manifest.return_value = {"name": "brain_mri", "version": "1.0.0"}

    mock_routing = MagicMock()
    mock_routing.get_all_rules.return_value = {
        "brain_mri": [{"body_parts": ["BRAIN"], "priority": 10, "enabled": True}]
    }

    dependencies.set_registry(mock_registry)
    dependencies.set_routing_service(mock_routing)

    client = TestClient(app)
    return client, mock_routing, mock_registry


class TestGetRoutingRules:
    def test_get_rules(self, app_client):
        client, routing, _ = app_client
        resp = client.get("/api/admin/routing-rules")
        assert resp.status_code == 200
        data = resp.json()
        assert "brain_mri" in data["routing_rules"]
        routing.get_all_rules.assert_called_once()


class TestUpdateRoutingRules:
    def test_update_rules(self, app_client):
        client, routing, _ = app_client
        new_rules = [{"usecase_name": "brain_mri", "body_parts": ["BRAIN", "HEAD"], "priority": 20}]

        resp = client.put(
            "/api/admin/routing-rules",
            json={"rules": new_rules},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        routing.update_site_rules.assert_called_once_with(new_rules)


class TestAdminListUsecases:
    def test_list(self, app_client):
        client, _, _ = app_client
        resp = client.get("/api/admin/usecases")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["usecases"]) == 1
        assert data["usecases"][0]["name"] == "brain_mri"


class TestGetManifest:
    def test_get_manifest(self, app_client):
        client, _, registry = app_client
        resp = client.get("/api/admin/usecases/brain_mri/manifest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "brain_mri"

    def test_manifest_not_found(self, app_client):
        client, _, registry = app_client
        registry.get_manifest.return_value = None
        resp = client.get("/api/admin/usecases/nonexistent/manifest")
        assert resp.status_code == 404


class TestSiteConfig:
    @patch("app.config.get_settings")
    def test_get_site_config_no_file(self, mock_settings, app_client):
        client, _, _ = app_client
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        mock_settings.return_value.site_config_path = mock_path
        mock_settings.return_value.site_id = "test_site"
        mock_settings.return_value.api_key = ""
        mock_settings.return_value.allowed_origins = "http://localhost"

        resp = client.get("/api/admin/site-config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["site_id"] == "test_site"
        assert data["config"] == {}

    @patch("app.config.get_settings")
    def test_update_site_config(self, mock_settings, app_client):
        client, _, _ = app_client
        mock_path = MagicMock()
        mock_path.parent.mkdir = MagicMock()
        mock_settings.return_value.site_config_path = mock_path
        mock_settings.return_value.site_id = "test_site"
        mock_settings.return_value.api_key = ""
        mock_settings.return_value.allowed_origins = "http://localhost"

        from unittest.mock import mock_open

        with patch("builtins.open", mock_open()):
            resp = client.put(
                "/api/admin/site-config",
                json={
                    "site_name": "Test Hospital",
                    "dicom_ae_title": "MRI_AI",
                    "routing_overrides": [],
                },
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
