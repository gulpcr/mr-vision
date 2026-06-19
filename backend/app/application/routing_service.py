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
        """Return the use-case names that match this study, priority-ordered."""
        candidates = self._evaluate_candidates(study, series)["candidates"]
        matched = [c for c in candidates if c["matched"]]
        matched.sort(key=lambda c: c["priority"], reverse=True)
        return [c["usecase"] for c in matched]

    def preview_routing(self, study: Study, series: list[Series]) -> dict:
        """Dry-run of :meth:`route_study`: report what auto-classification WOULD
        match, with a per-use-case reason, without creating any jobs."""
        ev = self._evaluate_candidates(study, series)
        # Matched first (priority desc), then the rest (priority desc) for context.
        ev["candidates"].sort(key=lambda c: (c["matched"], c["priority"]), reverse=True)
        ev["matched"] = [c["usecase"] for c in ev["candidates"] if c["matched"]]
        return ev

    def _evaluate_candidates(self, study: Study, series: list[Series]) -> dict:
        """Evaluate every registered use case against the study's DICOM tags.

        Single source of truth shared by route_study and preview_routing.
        """
        all_rules = self._build_effective_rules()
        require_region = get_settings().routing_require_region_match

        body_part = (
            study.body_part_examined.value if study.body_part_examined else ""
        ).upper()
        study_desc = (study.study_description or "").upper()
        series_descs = [(s.series_description or "").upper() for s in series]
        # A study may be multi-modality (PET/CT carries both PT and CT series),
        # while study.modality is a single inferred value. Match against the full
        # set of modalities present so a PT rule still applies to a PET/CT study
        # whose study-level modality was inferred as CT.
        modalities = {
            m.upper()
            for m in ([study.modality or ""] + [(s.modality or "") for s in series])
            if m
        }

        candidates: list[dict] = []
        for usecase_name, rules in all_rules.items():
            usecase = self._registry.usecases.get(usecase_name)
            if not usecase or not usecase.enabled:
                continue

            matched = False
            reason = "no enabled rule matched"
            for rule in rules:
                if not rule.get("enabled", True):
                    continue
                ok, why = self._rule_match_detail(
                    rule, body_part, study_desc, series_descs,
                    modalities, require_region,
                )
                reason = why  # keep the most informative (last-evaluated) reason
                if ok:
                    matched = True
                    break

            if matched:
                logger.info(
                    "routing_match",
                    study_uid=study.study_instance_uid,
                    usecase=usecase_name,
                    reason=reason,
                )
            candidates.append({
                "usecase": usecase_name,
                "matched": matched,
                "reason": reason,
                "priority": self._get_priority(rules),
            })

        return {
            "study_uid": study.study_instance_uid,
            "require_region_match": require_region,
            "tags_used": {
                "modality": (study.modality or "").upper() or None,
                "modalities_present": sorted(modalities),
                "body_part": body_part or None,
                "study_description": study_desc or None,
                "series_descriptions": [s for s in series_descs if s],
            },
            "candidates": candidates,
        }

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
    def _rule_match_detail(
        rule: dict,
        body_part: str,
        study_desc: str,
        series_descs: list[str],
        study_modalities: "set[str] | frozenset[str] | list[str]" = frozenset(),
        require_region: bool = True,
    ) -> tuple[bool, str]:
        """Evaluate one rule against a study's tags. Returns (matched, reason).

        Semantics (default, ``require_region=True``):
          1. Modality is a HARD GATE — when the rule specifies a modality, that
             modality must be present in the study (``study_modalities`` is the SET
             of modalities across the study and all its series, so a PT rule still
             applies to a PET/CT study). If the study has no modality info the gate
             is skipped.
          2. Region evidence is NECESSARY — the study must positively match at
             least one declared region condition (body part / study or series
             description). A missing/empty study field cannot satisfy this, so an
             MR study with no region tags no longer matches every MR use case.
          3. A modality-only rule (no region conditions) matches on the gate alone.

        With ``require_region=False`` the legacy OR behaviour is restored (modality
        match OR any region match).
        """
        rule_modality = (rule.get("modality") or "").upper()
        mods = {m.upper() for m in study_modalities if m}
        if rule_modality and mods and rule_modality not in mods:
            return False, f"modality {'/'.join(sorted(mods))} lacks {rule_modality}"

        body_parts = [bp.upper() for bp in rule.get("body_parts", [])]
        study_patterns = rule.get("study_description_patterns", [])
        series_patterns = rule.get("series_description_patterns", [])
        has_region = bool(body_parts or study_patterns or series_patterns)

        region_matched = False
        region_reason = ""
        if body_parts and body_part and body_part in body_parts:
            region_matched, region_reason = True, f"body_part {body_part}"
        if not region_matched and study_patterns and study_desc:
            for pat in study_patterns:
                if re.search(pat, study_desc, re.IGNORECASE):
                    region_matched, region_reason = True, f"study_description ~ /{pat}/"
                    break
        if not region_matched and series_patterns and series_descs:
            for sd in series_descs:
                if not sd:
                    continue
                for pat in series_patterns:
                    if re.search(pat, sd, re.IGNORECASE):
                        region_matched, region_reason = True, f"series '{sd}' ~ /{pat}/"
                        break
                if region_matched:
                    break

        # Modality-only rule: the gate is the entire test; a rule with no
        # conditions at all never matches.
        if not has_region:
            if rule_modality and mods:
                return True, f"modality {rule_modality} (no region conditions)"
            return False, "rule has no evaluable conditions"

        if region_matched:
            mod_note = f"modality {rule_modality} + " if (rule_modality and mods) else ""
            return True, f"{mod_note}{region_reason}"

        if require_region:
            return False, "no region evidence (body_part/description did not match)"

        # Legacy OR mode: a bare modality match is sufficient.
        if rule_modality and rule_modality in mods:
            return True, f"modality {rule_modality} (legacy OR)"
        return False, "no condition matched"

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
