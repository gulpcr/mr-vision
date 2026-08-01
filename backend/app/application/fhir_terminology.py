"""Per-plugin terminology maps: which result fields become Observations, and under what code.

Loads ``app/usecases/<name>/fhir_map.yaml`` — a sibling of the existing ``manifest.yaml`` /
``ui_schema.json``, so each plugin stays self-contained per the plugin rule.

**Strict whitelist.** A field with no map entry is never exported, and a plugin with no
``fhir_map.yaml`` exports nothing at all. That default is the entire point: the previous
export looped over ``measurements`` with ``isinstance(value, (int, float))`` and published
``image_dimensions`` and ``candidate_slices`` into patient charts as clinical findings,
while silently dropping SUVmax/MTV/TLG because those sit inside nested dicts
(FHIR_R4_COMPLIANCE_AUDIT.md §3.5). Whitelisting inverts that failure mode: the worst case
becomes a missing Observation, never a fabricated one.

Most plugins therefore have no map on purpose. The MedGemma narrative family
(brain/neck/chest/abdomen/lumbar_spine + the ct_* set) produces prose and rendered images,
not measurements — ``lumbar_spine_mri``'s summary holds ``candidate_slices``,
``image_dimensions`` and ``anomaly_slices``, none of which is a clinical value. Those
plugins correctly emit narrative and PDF only.

**Two codes per concept, and why.** Every entry carries a *local* code, always safe to use:
it names the platform's own concept and can never wrongly assert a standard one. An entry
may additionally declare a *standard* code (LOINC/SNOMED), which is used ONLY when marked
``verified: true``. Unverified standard codes ship as ``<TBD-verify>`` placeholders and are
ignored until someone qualified confirms them against a current release — a wrong LOINC is
worse than a missing one, because the receiving EHR trends the value under the wrong
concept and nothing surfaces the error. UCUM units need no such gate; unit strings are
mechanical.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
import yaml

from app.domain.enums import ObservationCategory
from app.domain.fhir_terminology import LOCAL_OBSERVATION_SYSTEM as LOCAL_SYSTEM  # noqa: F401

logger = structlog.get_logger(__name__)

MAP_FILENAME = "fhir_map.yaml"


# Any standard code containing this marker is a placeholder awaiting clinical sign-off.
_TBD_MARKER = "TBD"


@dataclass(frozen=True)
class ObservationSpec:
    """One whitelisted result field and how to code it."""

    path: str
    code: str
    display: str
    category: ObservationCategory
    unit: str | None = None                  # UCUM code; required for numeric values
    value_codes: bool = False                # treat a string/bool value as a coded answer
    body_site_code: str | None = None        # SNOMED body structure
    standard_system: str | None = None
    standard_code: str | None = None
    standard_verified: bool = False

    @property
    def has_usable_standard_code(self) -> bool:
        """True only when a standard code exists AND has been signed off."""
        return bool(
            self.standard_verified
            and self.standard_system
            and self.standard_code
            and _TBD_MARKER not in self.standard_code
        )


@dataclass(frozen=True)
class RepeatSpec:
    """A list of sub-records (e.g. pet_ct lesions), each yielding its own Observations."""

    each: str                                # dotted path to the list
    label_field: str | None = None           # per-item field used in the display text
    fields: tuple[ObservationSpec, ...] = ()


@dataclass(frozen=True)
class PluginMap:
    observations: tuple[ObservationSpec, ...] = ()
    repeats: tuple[RepeatSpec, ...] = ()
    excluded: tuple[str, ...] = field(default=())   # documented non-exports

    @property
    def is_empty(self) -> bool:
        return not self.observations and not self.repeats


_EMPTY = PluginMap()


def _parse_spec(raw: dict[str, Any], where: str) -> ObservationSpec | None:
    path = str(raw.get("path") or raw.get("field") or "").strip()
    code = str(raw.get("code") or "").strip()
    display = str(raw.get("display") or "").strip()
    if not path or not code or not display:
        logger.warning("fhir_map_entry_incomplete", where=where, entry=str(raw)[:120])
        return None

    try:
        category = ObservationCategory(str(raw.get("category", "imaging")).strip())
    except ValueError:
        logger.warning("fhir_map_bad_category", where=where, category=raw.get("category"))
        return None

    standard = raw.get("standard") or {}
    unit = raw.get("unit")

    # A body_site_code awaiting sign-off must not be written anywhere: dropped here so
    # the placeholder string can never reach the database or a payload. The map keeps the
    # marker as documentation of what still needs verifying.
    body_site = str(raw["body_site_code"]).strip() if raw.get("body_site_code") else None
    if body_site and _TBD_MARKER in body_site:
        logger.info(
            "fhir_map_body_site_unverified", where=where, path=path, value=body_site
        )
        body_site = None

    return ObservationSpec(
        path=path,
        code=code,
        display=display,
        category=category,
        unit=str(unit).strip() if unit not in (None, "") else None,
        value_codes=bool(raw.get("value_codes", False)),
        body_site_code=body_site,
        standard_system=(
            str(standard.get("system")).strip() if standard.get("system") else None
        ),
        standard_code=str(standard.get("code")).strip() if standard.get("code") else None,
        standard_verified=bool(standard.get("verified", False)),
    )


def _parse_map(data: dict[str, Any], usecase: str) -> PluginMap:
    observations = tuple(
        s for s in (
            _parse_spec(raw, f"{usecase}.observations")
            for raw in (data.get("observations") or [])
            if isinstance(raw, dict)
        ) if s is not None
    )

    repeats: list[RepeatSpec] = []
    for raw in data.get("repeats") or []:
        if not isinstance(raw, dict):
            continue
        each = str(raw.get("each") or "").strip()
        if not each:
            continue
        fields = tuple(
            s for s in (
                _parse_spec(f, f"{usecase}.repeats[{each}]")
                for f in (raw.get("fields") or [])
                if isinstance(f, dict)
            ) if s is not None
        )
        if fields:
            label = raw.get("label_field")
            repeats.append(
                RepeatSpec(
                    each=each,
                    label_field=str(label).strip() if label else None,
                    fields=fields,
                )
            )

    excluded = tuple(str(x) for x in (data.get("excluded") or []))
    return PluginMap(observations=observations, repeats=tuple(repeats), excluded=excluded)


@lru_cache(maxsize=64)
def load_plugin_map(usecase_name: str) -> PluginMap:
    """Load and cache one plugin's map. A missing or unreadable map exports nothing.

    Cached because it is read per completed job; call ``load_plugin_map.cache_clear()``
    after editing a map on disk.
    """
    from app.config import get_settings

    if not usecase_name:
        return _EMPTY
    path: Path = get_settings().usecases_dir / usecase_name / MAP_FILENAME
    if not path.exists():
        # Not an error: most plugins are narrative-only and must export nothing.
        return _EMPTY
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except Exception as exc:
        # Fail closed — an unparseable map must not fall back to exporting everything.
        logger.warning("fhir_map_unreadable", usecase=usecase_name, error=str(exc))
        return _EMPTY
    parsed = _parse_map(data, usecase_name)
    logger.info(
        "fhir_map_loaded",
        usecase=usecase_name,
        observations=len(parsed.observations),
        repeats=len(parsed.repeats),
        unverified=sum(
            1 for s in parsed.observations
            if s.standard_code and not s.has_usable_standard_code
        ),
    )
    return parsed


def resolve_by_path(payload: dict[str, Any], dotted: str) -> Any:
    """Read ``a.b.c`` out of nested dicts. None if any hop is missing."""
    node: Any = payload
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


@dataclass
class ExtractedValue:
    """A whitelisted value paired with the spec that authorised exporting it."""

    spec: ObservationSpec
    value: Any
    display: str            # spec.display, suffixed with a repeat label where applicable


def extract_values(usecase_name: str, result: dict[str, Any]) -> list[ExtractedValue]:
    """Pull every whitelisted value out of a pipeline result.

    ``result`` is the postprocessed dict (``summary`` / ``measurements`` / ...), so paths in
    the map are written as ``summary.suvmax_body`` — the same shape the pipeline returns.
    Values that are absent, None, or empty strings are skipped rather than exported as
    empty Observations.
    """
    plugin_map = load_plugin_map(usecase_name)
    if plugin_map.is_empty:
        return []

    out: list[ExtractedValue] = []

    for spec in plugin_map.observations:
        value = resolve_by_path(result, spec.path)
        if value is None or value == "":
            continue
        out.append(ExtractedValue(spec=spec, value=value, display=spec.display))

    for repeat in plugin_map.repeats:
        items = resolve_by_path(result, repeat.each)
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            label = ""
            if repeat.label_field:
                raw_label = item.get(repeat.label_field)
                if raw_label:
                    label = str(raw_label)
            suffix = f" — {label or f'#{index}'}"
            for spec in repeat.fields:
                value = resolve_by_path(item, spec.path)
                if value is None or value == "":
                    continue
                out.append(
                    ExtractedValue(spec=spec, value=value, display=f"{spec.display}{suffix}")
                )

    return out
