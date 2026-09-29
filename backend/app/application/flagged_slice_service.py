from __future__ import annotations

"""Map a CT-report result's slice tiles back to the DICOM images a viewer shows.

The CT report plugins (``abdomen_ct`` + ``ct_*``) render axial tiles from a NIfTI volume
built by SimpleITK/GDCM, and name each tile by its volume index ``z``
(``slc_{order}_z{z}_{window}.png``). That index is NOT something a DICOM viewer shows:
GDCM stacks the series' images by ImagePositionPatient projected onto the slice normal
(ascending — z=0 is the feet end on a standard axial CT), while OHIF orders/labels by
InstanceNumber. Series with missing images or reversed numbering make any "N − z"
arithmetic wrong, and the pipeline may have read a different series than the viewer
opened by default.

This service reproduces the GDCM ordering from the series' per-instance geometry (one
PACS call) and resolves every tile to its exact SOPInstanceUID, plus the tile's HU window
as a viewer VOI range. Works for results produced before the pipelines started recording
``series_instance_uid``/``window_settings`` (series then resolved by description + slice
count, windows from the plugin's config).

MRI report plugins render multi-sequence montages resampled onto a reference grid, so a
tile is not one DICOM image. Their pipelines record, per montage panel, its position in the
image, its source series and the source-volume slice its pixels came from, plus how far
that index varies across the panel (``summary.tile_geometry``). A panel drawn from a single
source slice resolves to that native image (by the same stacking order); a panel whose
index varies by a slice or more was reformatted from a series acquired in another plane
and is left unlinked. Results that predate ``tile_geometry`` are not linked (re-run).
"""

import asyncio
import re
import statistics
from pathlib import Path
from typing import Any

import structlog
import yaml

from app.application.ct_report_regions import CT_REPORT_USECASES
from app.application.result_service import ResultService
from app.domain.interfaces import PACSClient
from app.domain.models import InstanceGeometry

logger = structlog.get_logger(__name__)

# CT members of the report family — MRI tiles are resampled montages (see module doc).
CT_SLICE_LINK_USECASES: frozenset[str] = frozenset(
    u for u in CT_REPORT_USECASES if not u.endswith("_mri")
)

_TILE_RE = re.compile(r"^slc_(\d+)_z(\d+)_(.+)\.png$")
# MRI montage tiles: ``{window}_{order}_z{z}_montage.png`` (pipeline._render_plane).
_MRI_TILE_RE = re.compile(r"^(.+)_(\d+)_z(\d+)_montage\.png$")
# A panel whose source slice index varies by at least this much across the panel samples
# several native slices: it is a reformat from another plane, not one native image.
_MAX_SLICE_SPREAD = 1.0
_WINDOW_TAG_RE = re.compile(r"^\s*\[[^\]]*\]\s*")
_USECASES_DIR = Path(__file__).resolve().parent.parent / "usecases"
# Consecutive slice gaps beyond this multiple of the median gap count as "irregular".
_IRREGULAR_GAP_RATIO = 1.5


def _slug(name: str) -> str:
    """Same rule the CT plugins use to name tiles (``pipeline._slug``)."""
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") or "window"


def _qido_value(series: dict[str, Any], tag: str) -> Any:
    values = (series.get(tag) or {}).get("Value") or []
    return values[0] if values else None


def _slice_normal(iop: tuple[float, ...] | None) -> tuple[float, float, float]:
    if not iop:
        return (0.0, 0.0, 1.0)
    r, c = iop[:3], iop[3:]
    return (
        r[1] * c[2] - r[2] * c[1],
        r[2] * c[0] - r[0] * c[2],
        r[0] * c[1] - r[1] * c[0],
    )


def order_like_volume(instances: list[InstanceGeometry]) -> list[tuple[float, InstanceGeometry]]:
    """Instances in volume-index order (index == pipeline ``z``), with each one's
    position along the slice normal. Mirrors GDCM's IPP ordering: ascending distance along
    the normal of the lowest-InstanceNumber image — also for series whose images do not
    share one orientation (multi-slab), verified against the pipeline's SimpleITK volumes."""
    if not instances:
        return []
    first = min(instances, key=lambda g: (g.instance_number is None, g.instance_number or 0))
    n = _slice_normal(first.image_orientation)
    keyed = [
        (sum(p * q for p, q in zip(g.image_position, n)), g) for g in instances
    ]
    keyed.sort(key=lambda t: t[0])
    return keyed


def spacing_is_irregular(positions: list[float]) -> bool:
    gaps = [b - a for a, b in zip(positions, positions[1:])]
    if len(gaps) < 2:
        return False
    med = statistics.median(gaps)
    if med <= 0:
        return True
    return any(g > med * _IRREGULAR_GAP_RATIO or g <= 0 for g in gaps)


def _even_pick(items: list[int], k: int) -> list[int]:
    """Same rule the CT plugins' ``report._even_pick`` uses to cap the Pass-2 levels."""
    if k <= 0 or not items:
        return []
    if len(items) <= k:
        return list(items)
    step = (len(items) - 1) / (k - 1) if k > 1 else 0
    seen: list[int] = []
    for i in range(k):
        v = items[round(i * step)]
        if v not in seen:
            seen.append(v)
    return seen


def _load_plugin_config(usecase: str) -> dict[str, Any]:
    """Fallback for results predating ``window_settings``/``preview_z``/``report_z``: the
    plugin's HU windows (keyed by tile slug), preview count and Pass-2 level cap."""
    path = _USECASES_DIR / usecase / "model" / "inference_config.yaml"
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("flagged_slices_plugin_config_unreadable", usecase=usecase, error=str(exc))
        cfg = {}
    pre = cfg.get("preprocessing") or {}
    windows: dict[str, dict[str, Any]] = {}
    for w in pre.get("windows") or []:
        if isinstance(w, dict) and "level" in w and "width" in w:
            name = str(w.get("name", "window"))
            windows[_slug(name)] = {
                "name": name, "level": float(w["level"]), "width": float(w["width"])
            }
    return {
        "windows": windows,
        # Defaults mirror the pipeline / task hook defaults.
        "preview_count": max(1, int(pre.get("preview_count", 6))),
        "max_report_levels": int((cfg.get("report") or {}).get("max_report_levels", 6)),
    }


class FlaggedSliceService:
    def __init__(self, result_service: ResultService, pacs: PACSClient) -> None:
        self._results = result_service
        self._pacs = pacs

    async def get_slice_links(self, study_uid: str, usecase: str) -> dict[str, Any] | None:
        """Tile → DICOM image mapping for the latest result, or None if no result."""
        result = await self._results.get_result(study_uid, usecase)
        if result is None:
            return None

        base: dict[str, Any] = {
            "supported": usecase in CT_REPORT_USECASES,
            "resolved": False,
            "reason": None,
            "series_instance_uid": None,
            "series_description": None,
            "spacing_irregular": False,
            "tiles": [],
            "flagged_images": [],
        }
        if not base["supported"]:
            base["reason"] = "Viewer links are available for the CT and MRI report use cases only."
            return base
        if usecase not in CT_SLICE_LINK_USECASES:
            return await self._mri_slice_links(study_uid, usecase, result, base)

        summary = result.summary or {}
        dims = summary.get("image_dimensions") or []
        n_slices = int(dims[2]) if len(dims) >= 3 else None
        base["series_description"] = summary.get("series_description")

        series_uid, reason = await self._resolve_series(study_uid, summary, n_slices)
        if series_uid is None:
            base["reason"] = reason
            return base
        base["series_instance_uid"] = series_uid

        try:
            geometry = await self._pacs.get_series_instance_geometry(study_uid, series_uid)
        except Exception as exc:  # PACS unavailable / series deleted
            logger.warning("flagged_slices_geometry_failed", study=study_uid, error=str(exc))
            base["reason"] = "Could not read the series from the PACS."
            return base

        ordered = order_like_volume(geometry)
        if n_slices is not None and len(ordered) != n_slices:
            base["reason"] = (
                f"The series now has {len(ordered)} images but the analysis read {n_slices}; "
                "re-run the analysis to relink."
            )
            return base
        base["spacing_irregular"] = spacing_is_irregular([p for p, _ in ordered])

        plugin_cfg = await asyncio.to_thread(_load_plugin_config, usecase)
        windows = summary.get("window_settings") or plugin_cfg["windows"]
        findings_by_z = self._findings_by_z(summary.get("anomaly_findings"))

        # Slice tiles in stored order: the pipeline registers the overview previews
        # first, then the task hook appends the Pass-2 (reported) levels.
        slice_tiles: list[tuple[str, int, str]] = []
        for art in result.artifacts:
            if art.artifact_type != f"{usecase}_slice_png":
                continue
            m = _TILE_RE.match(art.name)
            if m and 0 <= int(m.group(2)) < len(ordered):
                slice_tiles.append((art.name, int(m.group(2)), m.group(3)))
        preview_z, report_z = self._tile_roles(summary, slice_tiles, findings_by_z, plugin_cfg)

        # OHIF lists a stack by InstanceNumber; expose the 1-based position in that order too.
        by_instance = sorted(
            (g for _, g in ordered),
            key=lambda g: (g.instance_number is None, g.instance_number or 0),
        )
        stack_pos = {g.sop_instance_uid: i + 1 for i, g in enumerate(by_instance)}

        tiles: list[dict[str, Any]] = []
        for name, z, slug in slice_tiles:
            g = ordered[z][1]
            win = windows.get(slug)
            tiles.append({
                "artifact_name": name,
                "z": z,
                "window": (
                    {"name": win["name"], "level": float(win["level"]), "width": float(win["width"])}
                    if win else None
                ),
                "sop_instance_uid": g.sop_instance_uid,
                "instance_number": g.instance_number,
                "stack_position": stack_pos.get(g.sop_instance_uid),
                # reported: got the detailed Pass-2 read and is in the report.
                # overview: one of the evenly-spread previews (stored flagged or not).
                # screen_flagged: Pass-1 (screening) flagged this level.
                "reported": z in report_z,
                "overview": z in preview_z and z not in report_z,
                "screen_flagged": z in findings_by_z,
                "finding": findings_by_z.get(z),
            })

        # Every Pass-1 flag, marked by whether the detailed read covered it — the viewer
        # highlights reported levels strongly and screening-only flags faintly.
        flagged_images = []
        for z in sorted(findings_by_z, reverse=True):  # superior → inferior, as the report
            if 0 <= z < len(ordered):
                g = ordered[z][1]
                flagged_images.append({
                    "z": z,
                    "sop_instance_uid": g.sop_instance_uid,
                    "instance_number": g.instance_number,
                    "stack_position": stack_pos.get(g.sop_instance_uid),
                    "finding": findings_by_z[z],
                    "reported": z in report_z,
                })

        base.update(resolved=True, tiles=tiles, flagged_images=flagged_images)
        return base

    # -- MRI montages -------------------------------------------------------------

    async def _mri_slice_links(
        self, study_uid: str, usecase: str, result: Any, base: dict[str, Any]
    ) -> dict[str, Any]:
        summary = result.summary or {}
        geometry = summary.get("tile_geometry")
        if not isinstance(geometry, dict) or not geometry:
            base["reason"] = (
                "This MRI result predates viewer links; re-run the analysis to make its "
                "slices clickable."
            )
            return base

        findings_by_z = self._findings_by_z(summary.get("anomaly_findings"))
        plugin_cfg = await asyncio.to_thread(_load_plugin_config, usecase)

        z_of: dict[str, int] = {}
        for name in geometry:
            m = _MRI_TILE_RE.match(name)
            if m:
                z_of[name] = int(m.group(3))
        slice_tiles: list[tuple[str, int, str]] = [
            (a.name, z_of[a.name], "")
            for a in result.artifacts
            if a.artifact_type == f"{usecase}_slice_png" and a.name in z_of
        ]
        preview_z, report_z = self._tile_roles(summary, slice_tiles, findings_by_z, plugin_cfg)

        uids = {
            p["series_instance_uid"]
            for g in geometry.values() if isinstance(g, dict)
            for p in g.get("panels") or [] if p.get("series_instance_uid")
        }

        async def fetch(uid: str) -> tuple[str, list[InstanceGeometry] | None]:
            try:
                return uid, await self._pacs.get_series_instance_geometry(study_uid, uid)
            except Exception as exc:  # series deleted / PACS unavailable
                logger.warning("flagged_slices_mri_geometry_failed", series=uid, error=str(exc))
                return uid, None

        fetched = await asyncio.gather(*(fetch(u) for u in sorted(uids)))
        index = {uid: _SeriesIndex.build(inst) for uid, inst in fetched if inst}

        def resolve(level: dict[str, Any]) -> list[dict[str, Any]]:
            ref_seq = level.get("reference_sequence")
            out = []
            for p in level.get("panels") or []:
                uid = p.get("series_instance_uid")
                panel = {
                    "sequence": p.get("sequence") or "",
                    "rect": p.get("rect") or [0.0, 0.0, 1.0, 1.0],
                    "is_reference": p.get("sequence") == ref_seq,
                    "clickable": False,
                    "reason": None,
                    "series_instance_uid": uid,
                    "sop_instance_uid": None,
                    "instance_number": None,
                    "stack_position": None,
                    "n_images": None,
                    "slice_offset": None,
                }
                idx = index.get(uid) if uid else None
                if idx is None:
                    panel["reason"] = "This sequence's series is not available in the PACS."
                else:
                    panel["n_images"] = idx.n_images
                    hit, why = idx.slice_for(p.get("slice_index"), p.get("slice_spread"), p.get("n_slices"))
                    if hit is None:
                        panel["reason"] = why
                    else:
                        g, offset = hit
                        panel.update(
                            clickable=True,
                            sop_instance_uid=g.sop_instance_uid,
                            instance_number=g.instance_number,
                            stack_position=idx.stack_pos.get(g.sop_instance_uid),
                            slice_offset=round(offset, 3),
                        )
                out.append(panel)
            return out

        panels_by_name = {n: resolve(g) for n, g in geometry.items() if isinstance(g, dict)}

        def primary(panels: list[dict[str, Any]]) -> dict[str, Any] | None:
            live = [p for p in panels if p["clickable"]]
            return next((p for p in live if p["is_reference"]), live[0] if live else None)

        tiles: list[dict[str, Any]] = []
        for name, z, _ in slice_tiles:
            panels = panels_by_name.get(name) or []
            head = primary(panels)
            tiles.append({
                "artifact_name": name,
                "z": z,
                "window": None,
                "plane": (geometry.get(name) or {}).get("plane"),
                "series_instance_uid": head["series_instance_uid"] if head else None,
                "sop_instance_uid": head["sop_instance_uid"] if head else None,
                "instance_number": head["instance_number"] if head else None,
                "stack_position": head["stack_position"] if head else None,
                "reported": z in report_z,
                "overview": z in preview_z and z not in report_z,
                "screen_flagged": z in findings_by_z,
                "finding": findings_by_z.get(z),
                "panels": panels,
            })

        # Every flagged level's native images, in every linked series. SOP UIDs are
        # globally unique, so the viewer highlights whichever series is on screen.
        flagged_images = []
        name_by_z = {z: n for n, z in z_of.items()}
        for z in sorted(findings_by_z, reverse=True):
            for p in panels_by_name.get(name_by_z.get(z, ""), []):
                if p["clickable"]:
                    flagged_images.append({
                        "z": z,
                        "series_instance_uid": p["series_instance_uid"],
                        "sop_instance_uid": p["sop_instance_uid"],
                        "instance_number": p["instance_number"],
                        "stack_position": p["stack_position"],
                        "finding": findings_by_z[z],
                        "reported": z in report_z,
                    })

        if not any(t["sop_instance_uid"] for t in tiles):
            base["reason"] = "None of this result's slices could be matched to images in the PACS."
            base["tiles"] = tiles
            return base
        base.update(resolved=True, tiles=tiles, flagged_images=flagged_images)
        return base

    async def _resolve_series(
        self, study_uid: str, summary: dict[str, Any], n_slices: int | None
    ) -> tuple[str | None, str | None]:
        uid = summary.get("series_instance_uid")
        if uid:
            return str(uid), None

        # Older results only recorded the description: match it AND the slice count.
        desc = (summary.get("series_description") or "").strip()
        if not desc or n_slices is None:
            return None, "The result does not record which series was analysed."
        try:
            series_list = await self._pacs.get_series_list(study_uid)
        except Exception as exc:
            logger.warning("flagged_slices_series_list_failed", study=study_uid, error=str(exc))
            return None, "Could not list the study's series from the PACS."
        matches = []
        for s in series_list:
            s_desc = str(_qido_value(s, "0008103E") or "").strip()
            try:
                s_count = int(_qido_value(s, "00201209") or 0)
            except (TypeError, ValueError):
                s_count = 0
            if s_desc == desc and s_count == n_slices:
                matches.append(_qido_value(s, "0020000E"))
        matches = [m for m in matches if m]
        if len(matches) == 1:
            return str(matches[0]), None
        if not matches:
            return None, f"No series '{desc}' with {n_slices} images found in the study."
        return None, f"Several series match '{desc}' with {n_slices} images; re-run to relink."

    @staticmethod
    def _tile_roles(
        summary: dict[str, Any],
        slice_tiles: list[tuple[str, int, str]],
        findings_by_z: dict[int, str],
        plugin_cfg: dict[str, Any],
    ) -> tuple[set[int], set[int]]:
        """(overview z-levels, reported z-levels) for the result's tiles.

        New runs record both in the summary. Older results are reconstructed: previews
        are the first ``preview_count`` tiles (the pipeline registers them before the hook
        appends the reported levels), and the reported levels are the Pass-1 flags capped
        with the plugin's ``_even_pick`` rule. The reconstruction is cross-checked against
        the stored tile order and falls back to it if the config changed since the run.
        """
        n_preview = plugin_cfg["preview_count"]
        stored_preview = [z for _, z, _ in slice_tiles[:n_preview]]
        appended = {z for _, z, _ in slice_tiles[n_preview:]}

        if "preview_z" in summary:
            preview = {int(z) for z in summary.get("preview_z") or []}
        else:
            preview = set(stored_preview)

        if "report_z" in summary:
            return preview, {int(z) for z in summary.get("report_z") or []}

        if findings_by_z:
            # anomaly_findings is stored superior→inferior, the order the hook picked from.
            anomaly_z = sorted(findings_by_z, reverse=True)
            derived = set(_even_pick(anomaly_z, plugin_cfg["max_report_levels"]))
            if appended <= derived:
                return preview, derived
        # Nothing flagged (the hook reported a representative sample) or the derivation
        # disagrees with what was stored: every tile the hook appended is a reported level.
        return preview, appended

    @staticmethod
    def _findings_by_z(raw: Any) -> dict[int, str]:
        out: dict[int, str] = {}
        for f in raw if isinstance(raw, list) else []:
            if not isinstance(f, dict) or "z" not in f:
                continue
            try:
                z = int(f["z"])
            except (TypeError, ValueError):
                continue
            text = _WINDOW_TAG_RE.sub("", str(f.get("finding") or "")).strip()
            out[z] = f"{out[z]}; {text}" if z in out and text else (text or out.get(z, ""))
        return out


class _SeriesIndex:
    """One series' images in the pipeline volume's stacking order."""

    def __init__(self, ordered, stack_pos, n_images) -> None:
        self.ordered = ordered
        self.stack_pos = stack_pos
        self.n_images = n_images

    @classmethod
    def build(cls, instances: list[InstanceGeometry]) -> "_SeriesIndex":
        ordered = [g for _, g in order_like_volume(instances)]
        by_instance = sorted(
            instances, key=lambda g: (g.instance_number is None, g.instance_number or 0)
        )
        stack_pos = {g.sop_instance_uid: i + 1 for i, g in enumerate(by_instance)}
        return cls(ordered, stack_pos, len(instances))

    def slice_for(
        self, slice_index: Any, slice_spread: Any, n_slices: Any
    ) -> tuple[tuple[InstanceGeometry, float] | None, str | None]:
        """The native image a panel shows, or (None, reason) when it is not one image."""
        if slice_index is None:
            return None, "This result does not record which image the panel shows."
        if slice_spread is not None and float(slice_spread) >= _MAX_SLICE_SPREAD:
            return None, (
                "Reformatted from a series acquired in another plane; there is no native "
                "image at this level."
            )
        if n_slices is not None and int(n_slices) != len(self.ordered):
            return None, (
                f"The series now has {len(self.ordered)} images but the analysis read "
                f"{n_slices}; re-run the analysis to relink."
            )
        k = int(round(float(slice_index)))
        if not 0 <= k < len(self.ordered):
            return None, "This level is outside the part of the body this series covers."
        return (self.ordered[k], abs(float(slice_index) - k)), None

