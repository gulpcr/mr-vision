from __future__ import annotations

"""Inference provenance (NIST AI RMF; HIPAA 164.312(c)(1) integrity): which exact model
iteration produced an AI result, on which hardware, with which inputs settings.

Collected by the Celery task after every pipeline run and stored in
``ai_inference_metadata`` (alembic 057), one row per result. The row's SHA-256 is part of
the content an e-signature covers, so a signed report is bound to the model that produced
it.

Weights: every file under the plugin's ``model/`` directory (incl. downloaded bundles) and
any path a pipeline declares in ``postprocessed["provenance"]["weights"]`` is hashed once
per worker process (cache keyed by path, size and mtime). Frameworks whose weights live in
shared caches (TotalSegmentator, nnU-Net, MONAI, Hugging Face) are identified by their
exact package version. A local LLM is identified by its Ollama model digest.
"""

import hashlib
import json
import os
import socket
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

WEIGHT_SUFFIXES = (".pt", ".pth", ".ckpt", ".safetensors", ".onnx", ".bin", ".pkl", ".h5",
                   ".tar", ".zip", ".npz", ".model", ".json", ".yaml", ".yml")
FRAMEWORKS = ("torch", "monai", "TotalSegmentator", "nnunetv2", "SimpleITK", "transformers",
              "pydicom", "numpy")
LLM_SUMMARY_KEYS = ("ai_report", "medgemma_findings", "anomaly_findings", "anomaly_slices",
                    "consolidated_report")
_hash_cache: dict[tuple[str, int, float], str] = {}


def file_sha256(path: str) -> str:
    st = os.stat(path)
    key = (os.path.realpath(path), st.st_size, st.st_mtime)
    if key not in _hash_cache:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _hash_cache[key] = h.hexdigest()
    return _hash_cache[key]


def _weight_files(paths: list[str]) -> list[dict[str, Any]]:
    out = []
    for root in paths:
        p = Path(root)
        files = [p] if p.is_file() else (sorted(q for q in p.rglob("*") if q.is_file()) if p.is_dir() else [])
        for f in files:
            if f.suffix.lower() in WEIGHT_SUFFIXES and "__pycache__" not in f.parts:
                try:
                    out.append({"path": str(f), "size": f.stat().st_size, "sha256": file_sha256(str(f))})
                except OSError as exc:
                    logger.warning("provenance_hash_failed", path=str(f), error=str(exc))
    return out


@lru_cache(maxsize=1)
def framework_versions() -> dict[str, str]:
    from importlib import metadata

    versions = {}
    for name in FRAMEWORKS:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return versions


@lru_cache(maxsize=1)
def gpu_inventory() -> list[dict[str, str]]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,uuid,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            gpus.append({"index": parts[0], "name": parts[1], "uuid": parts[2], "driver": parts[3]})
    return gpus


def gpus_used() -> list[dict[str, str]]:
    """The GPUs this process may use (CUDA_VISIBLE_DEVICES: indexes or UUIDs)."""
    visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    inventory = gpu_inventory()
    if visible is None:
        return inventory
    wanted = {v.strip() for v in visible.split(",") if v.strip()}
    return [g for g in inventory if g["index"] in wanted or g["uuid"] in wanted]


_llm_digest_cache: dict[str, str | None] = {}


def ollama_digest(base_url: str, model: str) -> str | None:
    key = f"{base_url}|{model}"
    if key not in _llm_digest_cache:
        digest = None
        try:
            import httpx

            tags = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=5).json().get("models", [])
            digest = next((m.get("digest") for m in tags if m.get("name") == model or m.get("model") == model), None)
        except Exception as exc:
            logger.warning("provenance_llm_digest_unavailable", error=str(exc))
        _llm_digest_cache[key] = digest
    return _llm_digest_cache[key]


def _source_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _windowing(summary: dict[str, Any], declared: Any) -> Any:
    if declared:
        return declared
    found = {k: v for k, v in (summary or {}).items() if "window" in k.lower() and k != "tile_geometry"}
    return found or None


def collect_provenance(
    *, usecase_name: str, postprocessed: dict[str, Any], started_at: str | None,
    completed_at: str | None, settings,
) -> dict[str, Any]:
    """Everything recorded about the model iteration that produced this result."""
    plugin_dir = Path(__file__).resolve().parents[1] / "usecases" / usecase_name
    declared = postprocessed.get("provenance") or {}
    summary = postprocessed.get("summary") or {}

    weights = _weight_files([str(plugin_dir / "model"), *declared.get("weights", [])])
    prompts = plugin_dir / "prompts.py"
    uses_llm = bool(declared.get("llm")) or any(k in summary for k in LLM_SUMMARY_KEYS)
    llm = None
    if uses_llm and getattr(settings, "medgemma_enabled", False):
        model = declared.get("llm") or settings.medgemma_model
        llm = {"engine": "ollama", "model": model,
               "digest": ollama_digest(settings.ollama_base_url, model)}

    return {
        "model_name": declared.get("model_name") or usecase_name,
        "model_version": postprocessed.get("model_version", "unknown"),
        "model_checksum": postprocessed.get("model_checksum", ""),
        "weights": weights,
        "frameworks": framework_versions(),
        "llm": llm,
        "prompt_sha256": _source_sha256(prompts) if prompts.exists() else None,
        "pipeline_sha256": _source_sha256(plugin_dir / "pipeline.py"),
        "inference_started_at": started_at,
        "inference_completed_at": completed_at,
        "worker_host": socket.gethostname(),
        "gpus": gpus_used(),
        "windowing": _windowing(summary, declared.get("windowing")),
        "platform_version": os.environ.get("MRCV_GIT_SHA") or os.environ.get("GIT_SHA"),
    }


def provenance_sha256(record: dict[str, Any]) -> str:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()
