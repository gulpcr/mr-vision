from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import structlog
import yaml

from app.config import get_settings
from app.application.usecase_registry import UseCaseRegistry
from app.domain.models import RoutingRule, Series, Study

logger = structlog.get_logger(__name__)


class RoutingService:
    """Routes incoming studies to applicable use cases based on DICOM tags and YAML rules."""

    def __init__(self, registry: UseCaseRegistry):
        self._registry = registry
        self._site_overrides: dict[str, list[RoutingRule]] = {}
        self._load_site_overrides()

    def _load_site_overrides(self):
        settings = get_settings()
        site_config_path = settings.site_config_path
        if site_config_path.exists():
            with open(site_config_path) as f:
                config = yaml.safe_load(f) or {}
            overrides = config.get("routing_overrides", [])
            for override in overrides:
                rule = RoutingRule(
                    usecase_name=override["usecase_name"],
                    body_parts=override.get("body_parts", []),
                    study_description_patterns=override.get("study_description_patterns", []),
                    series_description_patterns=override.get("series_description_patterns", []),
                    modality=override.get("modality", "MR"),
                    priority=override.get("priority", 100),
                    enabled=override.get("enabled", True),
                )
                uc_name = rule.usecase_name
                if uc_name not in self._site_overrides:
                    self._site_overrides[uc_name] = []
                self._site_overrides[uc_name].append(rule)
            logger.info(
                "loaded_site_routing_overrides",
                site=settings.site_id,
                override_count=len(overrides),
            )

    def route_study(self, study: Study, series: list[Series]) -> list[str]:
        """Return a list of use-case names that match this study."""
        matched = []
        all_rules = self._build_effective_rules()

        body_part = (
            study.body_part_examined.value if study.body_part_examined else ""
        ).upper()

        study_desc = (study.study_description or "").upper()
        series_descs = [
            (s.series_description or "").upper() for s in series
        ]
        study_modality = (study.modality or "").upper()

        for usecase_name, rules in all_rules.items():
            usecase = self._registry.usecases.get(usecase_name)
            if not usecase or not usecase.enabled:
                continue

            for rule in rules:
                if not rule.get("enabled", True):
                    continue
                if self._rule_matches(rule, body_part, study_desc, series_descs, study_modality):
                    matched.append(usecase_name)
                    logger.info(
                        "routing_match",
                        study_uid=study.study_instance_uid,
                        usecase=usecase_name,
                        body_part=body_part,
                    )
                    break

        matched.sort(
            key=lambda uc: self._get_priority(all_rules.get(uc, [])),
            reverse=True,
        )

        return matched

    def _build_effective_rules(self) -> dict[str, list[dict]]:
        """Merge per-usecase routing_rules.yaml with site overrides."""
        effective = {}

        for uc_name in self._registry.usecases:
            base_rules = self._registry.get_routing_rules(uc_name)
            effective[uc_name] = list(base_rules)

        for uc_name, override_rules in self._site_overrides.items():
            if uc_name not in effective:
                effective[uc_name] = []
            for rule in override_rules:
                effective[uc_name].append({
                    "body_parts": rule.body_parts,
                    "study_description_patterns": rule.study_description_patterns,
                    "series_description_patterns": rule.series_description_patterns,
                    "modality": rule.modality,
                    "priority": rule.priority,
                    "enabled": rule.enabled,
                })

        return effective

    @staticmethod
    def _rule_matches(
        rule: dict,
        body_part: str,
        study_desc: str,
        series_descs: list[str],
        study_modality: str = "",
    ) -> bool:
        """Match using OR logic: if ANY non-empty condition matches, the rule matches.

        Empty/missing DICOM fields are skipped (not penalized).
        If no conditions can be evaluated, the rule does NOT match.
        Modality is always evaluated when present in both rule and study.
        """
        matched_any = False
        evaluated_any = False

        # Check modality — always evaluated when the rule specifies one and study has one
        rule_modality = (rule.get("modality") or "").upper()
        if rule_modality and study_modality:
            evaluated_any = True
            if study_modality == rule_modality:
                matched_any = True

        # Check body part
        body_parts = [bp.upper() for bp in rule.get("body_parts", [])]
        if body_parts and body_part:
            evaluated_any = True
            if body_part in body_parts:
                matched_any = True

        # Check study description
        study_patterns = rule.get("study_description_patterns", [])
        if study_patterns and study_desc:
            evaluated_any = True
            if any(re.search(pat, study_desc, re.IGNORECASE) for pat in study_patterns):
                matched_any = True

        # Check series descriptions
        series_patterns = rule.get("series_description_patterns", [])
        if series_patterns and series_descs:
            evaluated_any = True
            for series_desc in series_descs:
                if series_desc and any(
                    re.search(pat, series_desc, re.IGNORECASE) for pat in series_patterns
                ):
                    matched_any = True
                    break

        # If no conditions could be evaluated (all fields empty), no match
        if not evaluated_any:
            return False

        return matched_any

    @staticmethod
    def _get_priority(rules: list[dict]) -> int:
        if not rules:
            return 0
        return max(r.get("priority", 0) for r in rules)

    def get_all_rules(self) -> dict[str, list[dict]]:
        return self._build_effective_rules()

    def update_site_rules(self, rules_data: list[dict]):
        settings = get_settings()
        site_config_path = settings.site_config_path
        existing = {}
        if site_config_path.exists():
            with open(site_config_path) as f:
                existing = yaml.safe_load(f) or {}
        existing["routing_overrides"] = rules_data
        site_config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(site_config_path, "w") as f:
            yaml.dump(existing, f, default_flow_style=False)
        self._site_overrides.clear()
        self._load_site_overrides()
        logger.info("updated_site_routing_rules", count=len(rules_data))
