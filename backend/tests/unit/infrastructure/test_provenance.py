"""AI-02: provenance identifies the exact model iteration behind a result."""
from __future__ import annotations

from types import SimpleNamespace

from app.infrastructure.provenance import collect_provenance, file_sha256, provenance_sha256

SETTINGS = SimpleNamespace(medgemma_enabled=False, medgemma_model="m", ollama_base_url="http://x")


def _collect(**post):
    base = {"model_version": "2.1", "model_checksum": "abc", "summary": {"scan_windows": ["brain"]}}
    base.update(post)
    return collect_provenance(usecase_name="brain_mri", postprocessed=base,
                              started_at="2026-10-02T10:00:00+00:00",
                              completed_at="2026-10-02T10:00:09+00:00", settings=SETTINGS)


def test_record_contents():
    rec = _collect()
    assert rec["model_name"] == "brain_mri" and rec["model_version"] == "2.1"
    cfg = [w for w in rec["weights"] if w["path"].endswith("inference_config.yaml")]
    assert cfg and len(cfg[0]["sha256"]) == 64          # the plugin's model config is hashed
    assert rec["pipeline_sha256"] and len(rec["pipeline_sha256"]) == 64
    assert "torch" in rec["frameworks"] or "numpy" in rec["frameworks"]
    assert rec["windowing"] == {"scan_windows": ["brain"]}
    assert rec["worker_host"] and rec["inference_completed_at"].endswith("09+00:00")


def test_declared_weights_and_llm_are_recorded(tmp_path):
    w = tmp_path / "net.pt"
    w.write_bytes(b"\x00weights")
    rec = _collect(provenance={"weights": [str(w)], "windowing": {"WW": 400, "WL": 40}})
    assert {"path": str(w), "size": 8, "sha256": file_sha256(str(w))} in rec["weights"]
    assert rec["windowing"] == {"WW": 400, "WL": 40}


def test_hash_is_canonical_and_changes_with_the_model(tmp_path):
    a = _collect()
    b = dict(a)
    assert provenance_sha256(a) == provenance_sha256(b)
    b["model_version"] = "2.2"
    assert provenance_sha256(a) != provenance_sha256(b)
