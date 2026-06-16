from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any

import jsonschema
import structlog
import yaml

from app.config import get_settings
from app.domain.interfaces import UseCaseRegistryRepository
from app.domain.models import UseCase

logger = structlog.get_logger(__name__)

MANIFEST_REQUIRED_KEYS = {"name", "version", "supported_body_parts", "required_sequences", "model_type"}


class UseCaseRegistry:
    """Discovers, validates, and manages use-case plugin modules at startup."""

    def __init__(self, repository: UseCaseRegistryRepository | None = None):
        self._repository = repository
        self._usecases: dict[str, UseCase] = {}
        self._pipelines: dict[str, Any] = {}
        self._manifests: dict[str, dict] = {}
        self._routing_rules: dict[str, list[dict]] = {}
        self._ui_schemas: dict[str, dict] = {}
        self._output_schemas: dict[str, dict] = {}

    @property
    def usecases(self) -> dict[str, UseCase]:
        return dict(self._usecases)

    def get_pipeline(self, usecase_name: str) -> Any:
        if usecase_name not in self._pipelines:
            raise ValueError(f"Use case '{usecase_name}' not registered or has no pipeline")
        return self._pipelines[usecase_name]

    def get_manifest(self, usecase_name: str) -> dict:
        return self._manifests.get(usecase_name, {})

    def get_routing_rules(self, usecase_name: str) -> list[dict]:
        return self._routing_rules.get(usecase_name, [])

    def get_all_routing_rules(self) -> dict[str, list[dict]]:
        return dict(self._routing_rules)

    def get_ui_schema(self, usecase_name: str) -> dict:
        return self._ui_schemas.get(usecase_name, {})

    def get_output_schema(self, usecase_name: str) -> dict:
        return self._output_schemas.get(usecase_name, {})

    async def discover_and_register(self):
        settings = get_settings()
        usecases_dir = settings.usecases_dir
        if not usecases_dir.exists():
            logger.warning("usecases_directory_not_found", path=str(usecases_dir))
            return

        for entry in sorted(usecases_dir.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.startswith("_"):
                continue
            manifest_path = entry / "manifest.yaml"
            if not manifest_path.exists():
                logger.warning("missing_manifest", usecase_dir=entry.name)
                continue
            try:
                await self._register_usecase(entry)
            except Exception as exc:
                logger.error(
                    "usecase_registration_failed",
                    usecase=entry.name,
                    error=str(exc),
                )

    async def _register_usecase(self, usecase_dir: Path):
        manifest = self._load_yaml(usecase_dir / "manifest.yaml")
        missing_keys = MANIFEST_REQUIRED_KEYS - set(manifest.keys())
        if missing_keys:
            raise ValueError(f"Manifest missing required keys: {missing_keys}")

        usecase_name = manifest["name"]
        logger.info("registering_usecase", name=usecase_name, version=manifest["version"])

        routing_rules_path = usecase_dir / "routing_rules.yaml"
        routing_rules = []
        if routing_rules_path.exists():
            routing_data = self._load_yaml(routing_rules_path)
            routing_rules = routing_data.get("rules", [])

        ui_schema_path = usecase_dir / "ui_schema.json"
        ui_schema = {}
        if ui_schema_path.exists():
            import json
            ui_schema = json.loads(ui_schema_path.read_text())

        output_schema_path = usecase_dir / "outputs_schema.json"
        output_schema = {}
        if output_schema_path.exists():
            import json
            output_schema = json.loads(output_schema_path.read_text())

        pipeline = None
        pipeline_path = usecase_dir / "pipeline.py"
        if pipeline_path.exists():
            module_name = f"app.usecases.{usecase_name}.pipeline"
            try:
                module = importlib.import_module(module_name)
                pipeline_cls = getattr(module, "Pipeline", None)
                if pipeline_cls:
                    pipeline = pipeline_cls()
                    logger.info("loaded_pipeline", usecase=usecase_name)
                else:
                    logger.warning("no_pipeline_class", usecase=usecase_name)
            except Exception as exc:
                logger.warning(
                    "pipeline_import_failed",
                    usecase=usecase_name,
                    error=str(exc),
                )

        usecase = UseCase(
            name=usecase_name,
            version=manifest["version"],
            supported_body_parts=manifest.get("supported_body_parts", []),
            required_sequences=manifest.get("required_sequences", []),
            model_type=manifest.get("model_type", ""),
            enabled=manifest.get("enabled", True),
            module_path=str(usecase_dir),
            description=manifest.get("description", ""),
        )

        self._usecases[usecase_name] = usecase
        self._manifests[usecase_name] = manifest
        self._routing_rules[usecase_name] = routing_rules
        self._ui_schemas[usecase_name] = ui_schema
        self._output_schemas[usecase_name] = output_schema
        if pipeline:
            self._pipelines[usecase_name] = pipeline

        if self._repository:
            await self._repository.save(usecase)

        logger.info(
            "usecase_registered",
            name=usecase_name,
            version=manifest["version"],
            has_pipeline=pipeline is not None,
            routing_rules_count=len(routing_rules),
        )

    @staticmethod
    def _load_yaml(path: Path) -> dict:
        with open(path, "r") as f:
            return yaml.safe_load(f) or {}
