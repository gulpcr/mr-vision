from __future__ import annotations

from unittest.mock import MagicMock

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
        supported_body_parts=["BRAIN", "HEAD"],
        required_sequences=["T1", "FLAIR"],
        model_type="segresnet",
        enabled=True,
        description="Brain MRI segmentation",
    )

    mock_registry = MagicMock()
    mock_registry.usecases = {"brain_mri": sample_uc}
    mock_registry.get_ui_schema.return_value = {"type": "object"}
    mock_registry.get_output_schema.return_value = {"type": "object"}

    mock_routing = MagicMock()
    dependencies.set_registry(mock_registry)
    dependencies.set_routing_service(mock_routing)

    client = TestClient(app)
    return client, mock_registry


class TestListUsecases:
    def test_list(self, app_client):
        client, _ = app_client
        resp = client.get("/api/usecases")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["usecases"]) == 1
        uc = data["usecases"][0]
        assert uc["name"] == "brain_mri"
        assert uc["model_type"] == "segresnet"
        assert uc["enabled"] is True
        assert "BRAIN" in uc["supported_body_parts"]


class TestUISchema:
    def test_get_schema(self, app_client):
        client, registry = app_client
        resp = client.get("/api/usecases/brain_mri/ui-schema")
        assert resp.status_code == 200
        registry.get_ui_schema.assert_called_with("brain_mri")

    def test_not_found(self, app_client):
        client, registry = app_client
        registry.get_ui_schema.return_value = None
        resp = client.get("/api/usecases/nonexistent/ui-schema")
        assert resp.status_code == 404


class TestOutputSchema:
    def test_get_schema(self, app_client):
        client, registry = app_client
        resp = client.get("/api/usecases/brain_mri/output-schema")
        assert resp.status_code == 200

    def test_not_found(self, app_client):
        client, registry = app_client
        registry.get_output_schema.return_value = None
        resp = client.get("/api/usecases/nonexistent/output-schema")
        assert resp.status_code == 404
