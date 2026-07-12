"""MedGemma debug endpoints — inspect the exact inputs and outputs of the local VLM.

Nothing in the pipeline persists the prompt or the raw model response (only the
parsed ``ai_report`` is stored), so this router reconstructs the EXACT inputs a
use case would send to MedGemma for a completed study and — optionally — re-runs
the model, returning:

  * the full prompt text,
  * an image manifest (name / size / sha256, and base64 if requested),
  * the raw model output (before JSON parsing),
  * the parsed/validated result (what the pipeline would store), and
  * timing.

It is parameterised by use case (``/api/debug/medgemma/{usecase}/{study_uid}``)
with a per-use-case recipe registry, so "each case" has its own reconstruction.
Only pet_ct is wired to MedGemma today; other use cases return a clear
"not configured" response and are a one-function addition to ``_RECIPES``.

This is a debug surface: it re-runs a live model call (~20 s for medgemma:27b)
and returns PHI-bearing content (images, findings), so it inherits the same auth
as the rest of ``/api`` and should not be exposed unauthenticated.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from typing import Annotated, Any, Awaitable, Callable

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.result_service import ResultService
from app.config import get_settings
from app.domain.models import Result
from app.interface.api.dependencies import get_result_service, get_session

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/debug/medgemma", tags=["debug"])

# A recipe reconstructs (prompt, images, force_json, parser) for one use case,
# reusing that use case's real prompt builder so the debug view matches what the
# pipeline actually sends — not a separate approximation.
Recipe = Callable[
    [str, ResultService, AsyncSession, Result],
    Awaitable[tuple[str, dict[str, bytes], bool, Callable[[str], Any] | None]],
]


# ── pet_ct recipe (reuses PetCtNarrativeService's real prompt) ──────────────────

async def _recipe_pet_ct(
    study_uid: str, service: ResultService, session: AsyncSession, result: Result,
) -> tuple[str, dict[str, bytes], bool, Callable[[str], Any] | None]:
    # Build the SAME payload the production MedGemma path uses (tasks.py): the
    # Stage-3 numbered coronal roadmap + regional composite crops, plus the
    # data-matrix / Findings-First prompt from prompts.py — so the debug prompt is
    # exactly what MedGemma actually receives, not the (Gemini) narrative prompt.
    from app.application.pet_ct_narrative_service import (
        _parse_json_response,
        _validate_and_normalise,
    )
    from app.usecases.pet_ct import prompts

    summary = result.summary or {}
    measurements = result.measurements or {}

    # Images MedGemma reads: roadmap first, then composite crops (by name).
    roadmap = [a for a in (result.artifacts or []) if getattr(a, "artifact_type", "") == "lesion_roadmap_png"]
    comps = sorted(
        (a for a in (result.artifacts or []) if getattr(a, "artifact_type", "") == "composite_crop_png"),
        key=lambda a: getattr(a, "name", ""),
    )
    ordered: list[tuple[str, bytes]] = []
    for art in [*roadmap, *comps]:
        try:
            data = await service.get_artifact_data(study_uid, "pet_ct", art.name)
            ordered.append((art.name, data))
        except Exception:
            continue

    s = get_settings()
    payload = prompts.build_payload(
        summary=summary,
        measurements=measurements,
        images=ordered,
        radiopharmaceutical=summary.get("radiopharmaceutical", "FDG"),
        quantitative=bool(summary.get("quantitative", True)),
        max_images=s.medgemma_max_images,
    )
    sent_images = dict(zip(payload["image_names"], payload["images"]))

    def parser(raw: str) -> Any:
        return _validate_and_normalise(_parse_json_response(raw))

    return payload["prompt"], sent_images, True, parser


async def _recipe_abdomen_ct(
    study_uid: str, service: ResultService, session: AsyncSession, result: Result,
) -> tuple[str, dict[str, bytes], bool, Callable[[str], Any] | None]:
    # Rebuild the SAME payload the production abdomen_ct path sends (tasks.py): the
    # soft-tissue-windowed sampled slices (abdomen_ct_slice_png) + the free-text
    # prompt from the abdomen_ct plugin's prompts.py.
    from app.usecases.abdomen_ct import prompts as abdomen_ct_prompts

    summary = result.summary or {}
    slices = sorted(
        (a for a in (result.artifacts or []) if getattr(a, "artifact_type", "") == "abdomen_ct_slice_png"),
        key=lambda a: getattr(a, "name", ""),
    )
    ordered: list[tuple[str, bytes]] = []
    for art in slices:
        try:
            data = await service.get_artifact_data(study_uid, "abdomen_ct", art.name)
            ordered.append((art.name, data))
        except Exception:
            continue

    payload = abdomen_ct_prompts.build_payload(
        images=ordered,
        windows=summary.get("hu_windows") or ["soft-tissue"],
        study_description=summary.get("study_description"),
        max_images=0,
    )
    sent_images = dict(zip(payload["image_names"], payload["images"]))
    return payload["prompt"], sent_images, True, abdomen_ct_prompts.parse_report


_RECIPES: dict[str, Recipe] = {
    "pet_ct": _recipe_pet_ct,
    "abdomen_ct": _recipe_abdomen_ct,
}


# ── helpers ─────────────────────────────────────────────────────────────────────

def _build_client(force_json: bool):
    """Construct a MedGemmaClient from settings (probes Ollama in __init__)."""
    from app.infrastructure.llm.medgemma_client import MedGemmaClient

    s = get_settings()
    return MedGemmaClient(
        base_url=s.ollama_base_url,
        model_name=s.medgemma_model,
        timeout_s=s.medgemma_timeout_s,
        force_json=force_json,
    )


# ── endpoints ────────────────────────────────────────────────────────────────────

@router.get("/status")
async def medgemma_status() -> dict[str, Any]:
    """MedGemma configuration + live Ollama reachability and model inventory."""
    s = get_settings()
    info: dict[str, Any] = {
        "enabled": s.medgemma_enabled,
        "base_url": s.ollama_base_url,
        "model": s.medgemma_model,
        "max_images": s.medgemma_max_images,
        "timeout_s": s.medgemma_timeout_s,
        "supported_usecases": sorted(_RECIPES),
    }
    try:
        import httpx

        def _tags() -> list[str]:
            with httpx.Client(timeout=5.0) as c:
                r = c.get(f"{s.ollama_base_url.rstrip('/')}/api/tags")
                r.raise_for_status()
                return [str(m.get("name", "")) for m in r.json().get("models", []) or []]

        models = await asyncio.to_thread(_tags)
        base = s.medgemma_model.split(":")[0]
        info["ollama_reachable"] = True
        info["ollama_models"] = models
        info["model_present"] = any(m == s.medgemma_model or m.split(":")[0] == base for m in models)
    except Exception as exc:
        info["ollama_reachable"] = False
        info["error"] = str(exc)
    return info


async def _gather_all_image_artifacts(
    study_uid: str, usecase: str, service: ResultService, result: Result,
    already: dict[str, bytes],
) -> tuple[dict[str, bytes], dict[str, str]]:
    """Fetch EVERY image artifact for the study (mip/fused + Stage-3 roadmap/crops).

    ``already`` holds bytes already fetched by the recipe (the images actually sent
    to the model) so they are not re-downloaded. Returns ``(name->bytes, name->type)``
    for all PNG artifacts recorded on the result, so the debug view shows every image
    the pipeline produced — not just the ones fed to MedGemma.
    """
    images: dict[str, bytes] = dict(already)
    types: dict[str, str] = {}
    for art in (result.artifacts or []):
        name = getattr(art, "name", None) or ""
        ctype = (getattr(art, "content_type", "") or "").lower()
        if ctype != "image/png" and not name.lower().endswith(".png"):
            continue
        types[name] = getattr(art, "artifact_type", "") or ""
        if name not in images:
            try:
                images[name] = await service.get_artifact_data(study_uid, usecase, name)
            except Exception:
                continue  # artifact recorded but object missing — skip, still listed below
    return images, types


@router.get("/{usecase}/{study_uid}")
async def medgemma_debug(
    usecase: str,
    study_uid: str,
    service: Annotated[ResultService, Depends(get_result_service)],
    session: Annotated[AsyncSession, Depends(get_session)],
    run: bool = True,
    include_images: bool = False,
    send_all: bool = False,
) -> dict[str, Any]:
    """Reconstruct MedGemma inputs for a study and (optionally) re-run the model.

    Shows EVERY image the pipeline produced for the study — the 6 MIP/fused views
    that are actually sent to the model AND the Stage-3 images (numbered coronal
    ``lesion_roadmap.png`` + ``composite_region_N.png`` regional crops) that are
    generated but NOT sent by the production report path. Each image is flagged
    ``sent_to_model`` so it is clear what the model saw vs. what merely exists.

    Query params:
      * ``run`` (default true): call MedGemma and return its raw + parsed output.
        Set false to inspect the prompt/images only (no model call, instant).
      * ``include_images`` (default false): embed each image as base64 in the
        manifest (large). Otherwise only name/type/size/sha256 are returned.
      * ``send_all`` (default false): EXPERIMENTAL — feed every image (incl.
        Stage-3) to the model instead of just the 6 the prompt describes. Useful
        to A/B whether the composites help; note the prompt text still says "six
        images", so treat the output as exploratory.
    """
    s = get_settings()
    recipe = _RECIPES.get(usecase)
    if recipe is None:
        raise HTTPException(
            400,
            f"MedGemma debug not configured for usecase '{usecase}'. "
            f"Supported: {sorted(_RECIPES)}",
        )

    result = await service.get_result(study_uid, usecase)
    if not result:
        raise HTTPException(404, f"No result found for study '{study_uid}' / usecase '{usecase}'")

    prompt, sent_images, force_json, parser = await recipe(study_uid, service, session, result)
    sent_names = list(sent_images)  # preserve order (mip_* then fused_*)

    # Every image artifact on the result (sent + Stage-3 not-sent).
    all_images, image_types = await _gather_all_image_artifacts(
        study_uid, usecase, service, result, sent_images
    )
    # Order: sent images first (in prompt order), then the rest by name.
    ordered = sent_names + sorted(n for n in all_images if n not in sent_names)

    manifest: list[dict[str, Any]] = []
    for name in ordered:
        data = all_images[name]
        entry: dict[str, Any] = {
            "name": name,
            "artifact_type": image_types.get(name),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()[:16],
            "sent_to_model": name in sent_images,
        }
        if include_images:
            entry["base64"] = base64.b64encode(data).decode("ascii")
        manifest.append(entry)

    # Which images this run will actually send.
    send_names = ordered if send_all else sent_names
    send_bytes = [all_images[n] for n in send_names]

    resp: dict[str, Any] = {
        "usecase": usecase,
        "study_uid": study_uid,
        "result_version": getattr(result, "version", None),
        "stored_ai_report_provider": (result.summary or {}).get("ai_report_provider"),
        "medgemma": {
            "enabled": s.medgemma_enabled,
            "base_url": s.ollama_base_url,
            "model": s.medgemma_model,
            "force_json": force_json,
        },
        "inputs": {
            "prompt": prompt,
            "prompt_chars": len(prompt),
            "n_images_total": len(all_images),
            "n_images_sent": len(send_names),
            "send_all": send_all,
            "sent_image_names": send_names,
            "images": manifest,
        },
        "output": None,
    }

    if not run:
        resp["note"] = "run=false — inputs only; MedGemma was not called."
        return resp

    if not send_bytes:
        resp["note"] = "No images available for this study (artifacts missing) — MedGemma not called."
        return resp

    client = await asyncio.to_thread(_build_client, force_json)
    resp["medgemma"]["ready"] = client.ready
    if not client.ready:
        resp["note"] = (
            "MedGemma not ready (Ollama unreachable or model not pulled). "
            "See GET /api/debug/medgemma/status."
        )
        return resp

    t0 = time.monotonic()
    raw = await client.generate_from_images(prompt, send_bytes)
    elapsed = round(time.monotonic() - t0, 2)

    parsed: Any = None
    parse_error: str | None = None
    if parser and raw:
        try:
            parsed = parser(raw)
        except Exception as exc:  # surface parse failures — the whole point of debugging
            parse_error = str(exc)

    resp["output"] = {
        "raw": raw,
        "raw_chars": len(raw),
        "parsed": parsed,
        "parsed_ok": parsed is not None,
        "parse_error": parse_error,
        "elapsed_s": elapsed,
    }
    logger.info(
        "medgemma_debug_run",
        usecase=usecase, study_uid=study_uid, model=s.medgemma_model,
        n_images_sent=len(send_names), send_all=send_all,
        raw_chars=len(raw), parsed_ok=parsed is not None, elapsed_s=elapsed,
    )
    return resp
