"""Priority review queue + electronic report signatures + report comments (alembic 049).

Same harness as test_tenant_isolation.py (skipped unless RLS_TEST_OWNER_DATABASE_URL is set).
"""
from __future__ import annotations

import json
import uuid

import pytest

from tests.rls.test_tenant_isolation import (  # noqa: F401  (fixtures)
    OWNER_URL,
    STUDY_A,
    TB,
    _auth,
    _run_isolated,
    as_b,
    as_platform,
    as_rad_b,
    client,
    seeded,
)

pytestmark = pytest.mark.skipif(not OWNER_URL, reason="RLS_TEST_OWNER_DATABASE_URL not set")

DIGITS = str(uuid.uuid4().int)[:8]
BASE = f"1.2.826.0.1.9049.{DIGITS}"
S_CRIT, S_ABN, S_NORM, S_NORES, S_ALERT, S_OTHER, S_REF = (f"{BASE}.{i}" for i in range(1, 8))
STATEMENT = "comprehensive-v1"


def _owner_exec(sql: str, params: dict | None = None):
    import sqlalchemy as sa

    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        rows = c.execute(sa.text(sql), params or {})
        out = rows.fetchall() if rows.returns_rows else None
    engine.dispose()
    return out


def _add_result(study: str, usecase: str, summary: dict) -> None:
    _owner_exec(
        "INSERT INTO results_index (id, study_instance_uid, usecase_name, summary, measurements, "
        "qa_flags, qa_details, model_version, model_checksum, artifacts, version, is_latest, "
        "tenant_id, created_at) VALUES (:id, :s, :u, CAST(:sum AS json), '{}', '[]', '{}', '1', "
        "'abc', '[]', 1, true, :t, now())",
        {"id": str(uuid.uuid4()), "s": study, "u": usecase, "sum": json.dumps(summary), "t": TB},
    )


@pytest.fixture(scope="module")
def world(seeded):
    ids = {"doc": str(uuid.uuid4()), "rad2": str(uuid.uuid4()), "noname": str(uuid.uuid4())}
    for uid, name, role, full in (
        (ids["doc"], f"doc49{DIGITS}", "doctor", "Dr Referring"),
        (ids["rad2"], f"rad49{DIGITS}", "radiologist", "Dr Second"),
        (ids["noname"], f"nn49{DIGITS}", "radiologist", ""),
    ):
        _owner_exec(
            "INSERT INTO users (id, username, email, hashed_password, full_name, role, tenant_id, "
            "is_active, status, token_version, created_at, updated_at) VALUES (:id, :n, :e, 'x', "
            ":f, :r, :t, true, 'active', 0, now(), now())",
            {"id": uid, "n": name, "e": f"{name}@example.com", "f": full, "r": role, "t": TB},
        )
    # Received in reverse priority order, so the queue must re-order them.
    for i, study in enumerate((S_NORM, S_ABN, S_CRIT, S_NORES, S_ALERT, S_OTHER, S_REF)):
        _owner_exec(
            "INSERT INTO studies (study_instance_uid, tenant_id, patient_id, patient_name, modality, "
            "reading_status, referring_user_id, created_at, updated_at) VALUES (:s, :t, 'MRN-49', "
            ":n, 'MG', 'unread', :ref, now() - make_interval(mins => :age), now())",
            {"s": study, "t": TB, "n": f"Pt {i}", "ref": ids["doc"] if study == S_REF else None,
             "age": 100 - i},
        )
    _add_result(S_CRIT, "mammography", {"birads_right": "5", "birads_left": "1"})
    _add_result(S_ABN, "abdomen_ct", {"anomaly_slices": [12, 13]})
    _add_result(S_NORM, "brain_mri", {"tumor_detected": False})
    _add_result(S_ALERT, "brain_mri", {})
    _add_result(S_OTHER, "brain_mri", {})
    _add_result(S_REF, "abdomen_ct", {"anomaly_findings": [{"organ": "liver"}]})
    _owner_exec(
        "INSERT INTO critical_alerts (id, study_instance_uid, usecase_name, result_id, finding_type, "
        "severity, title, message, status, escalation_count, tenant_id, created_at) VALUES "
        "(:id, :s, 'brain_mri', 'r', 'midline_shift', 'CRITICAL', 'Midline shift', 'm', 'pending', 0, :t, now())",
        {"id": str(uuid.uuid4()), "s": S_ALERT, "t": TB},
    )
    _owner_exec(
        "INSERT INTO mammography_reports (study_instance_uid, tenant_id, opinion, created_at, updated_at) "
        "VALUES (:s, :t, 'suspicious mass', now(), now())", {"s": S_CRIT, "t": TB},
    )
    _owner_exec(
        "UPDATE studies SET reading_status = 'in_progress', assigned_to = :r, assigned_to_username = :n "
        "WHERE study_instance_uid = :s", {"r": ids["rad2"], "n": f"rad49{DIGITS}", "s": S_OTHER},
    )
    ids["as_doc"] = _auth(ids["doc"], f"doc49{DIGITS}", "doctor", TB)
    ids["as_noname"] = _auth(ids["noname"], f"nn49{DIGITS}", "radiologist", TB)
    yield ids
    _owner_exec("DELETE FROM studies WHERE study_instance_uid LIKE :b", {"b": f"{BASE}.%"})
    _owner_exec("DELETE FROM users WHERE id IN (:a, :b, :c)", {"a": ids["doc"], "b": ids["rad2"], "c": ids["noname"]})


def _queue(client, headers, **params):
    r = client.get("/api/review-queue", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _ours(items):
    return [i for i in items if i["study_instance_uid"].startswith(BASE)]


def _sign(client, headers, study, **overrides):
    body = {"statement_version": STATEMENT, "agreed": True, "comment": "Agree with AI findings."}
    body.update(overrides)
    return client.post(f"/api/studies/{study}/signature", headers=headers, json=body)


def test_queue_is_priority_ordered_and_tenant_confined(client, world, as_rad_b):
    data = _queue(client, as_rad_b)
    items = _ours(data["items"])
    order = [i["study_instance_uid"] for i in items]
    assert S_NORES not in order                                  # no AI result → not in the queue
    assert STUDY_A not in {i["study_instance_uid"] for i in data["items"]}   # other tenant
    prio = {i["study_instance_uid"]: i["priority"] for i in items}
    assert prio[S_CRIT] == "critical" and prio[S_ALERT] == "critical"
    assert prio[S_ABN] == "abnormal" and prio[S_REF] == "abnormal"
    assert prio[S_NORM] == "normal"
    ranks = [{"critical": 0, "abnormal": 1, "normal": 2}[prio[u]] for u in order]
    assert ranks == sorted(ranks)
    # Within a priority, the oldest received comes first (S_CRIT is older than S_ALERT).
    assert order.index(S_CRIT) < order.index(S_ALERT)
    crit = next(i for i in items if i["study_instance_uid"] == S_CRIT)
    assert any("BI-RADS 5" in r for r in crit["priority_reasons"])
    assert data["counts"]["critical"] >= 2
    only = _queue(client, as_rad_b, priority="normal")
    assert {i["priority"] for i in only["items"]} <= {"normal"}


def test_priority_override_and_clear(client, world, as_rad_b):
    r = client.put(f"/api/studies/{S_NORM}/priority", headers=as_rad_b, json={"priority": "critical"})
    assert r.status_code == 200, r.text
    assert r.json()["priority"] == "critical" and r.json()["computed_priority"] == "normal"
    assert r.json()["priority_overridden"] is True
    item = next(i for i in _queue(client, as_rad_b)["items"] if i["study_instance_uid"] == S_NORM)
    assert item["priority"] == "critical"
    r = client.put(f"/api/studies/{S_NORM}/priority", headers=as_rad_b, json={"priority": None})
    assert r.json()["priority"] == "normal" and r.json()["priority_overridden"] is False
    assert client.put(f"/api/studies/{S_NORM}/priority", headers=world["as_doc"],
                      json={"priority": "critical"}).status_code == 403
    assert client.put(f"/api/studies/{S_NORM}/priority", headers=as_rad_b,
                      json={"priority": "urgent"}).status_code == 422


def test_signature_requires_attestation_comment_and_current_statement(client, world, as_rad_b):
    assert _sign(client, as_rad_b, S_ABN, agreed=False).status_code == 422
    assert _sign(client, as_rad_b, S_ABN, comment="   ").status_code == 422
    assert _sign(client, as_rad_b, S_ABN, statement_version="v0").status_code == 422
    assert _sign(client, as_rad_b, S_NORES).status_code == 409          # nothing to sign


def test_old_bare_sign_endpoint_is_retired(client, world, as_rad_b):
    assert client.post(f"/api/studies/{S_ABN}/sign", headers=as_rad_b).status_code == 410


def test_esignature_records_attestation_and_locks_the_report(client, world, as_rad_b, as_platform):
    r = _sign(client, as_rad_b, S_CRIT, comment="BI-RADS 5 confirmed; biopsy advised.")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reading_status"] == "signed" and body["integrity"] == "valid"
    sig = body["signatures"][0]
    assert sig["signer_full_name"] == "rls_rad_b" and sig["signer_role"] == "radiologist"
    assert sig["statement_text"].startswith("I, rls_rad_b, attest")
    assert sig["comment"] == "BI-RADS 5 confirmed; biopsy advised."
    assert sig["priority_at_signing"] == "critical" and len(sig["content_hash"]) == 64

    assert _sign(client, as_rad_b, S_CRIT).status_code == 409           # already signed
    assert S_CRIT not in {i["study_instance_uid"] for i in _queue(client, as_rad_b)["items"]}
    signed = _ours(_queue(client, as_rad_b, status="signed")["items"])
    assert any(i["study_instance_uid"] == S_CRIT and i["signed_by"] == "rls_rad_b" for i in signed)

    # The signed mammography report can no longer be edited.
    r = client.put(f"/api/studies/{S_CRIT}/mammography-report", headers=as_rad_b, json={"opinion": "changed"})
    assert r.status_code == 409, r.text

    # The signature is in the tamper-evident audit chain, and the chain is intact.
    rows = _owner_exec("SELECT seq, row_hash FROM audit_log WHERE action = 'report_esigned' AND entity_id = :s",
                       {"s": S_CRIT})
    assert rows and rows[0][0] is not None and rows[0][1]
    verify = client.get("/api/admin/audit/verify", headers=as_platform).json()
    assert verify["valid"] is True, verify

    # Changing the signed content after the fact is detected.
    _owner_exec("UPDATE results_index SET summary = CAST(:s AS json) WHERE study_instance_uid = :u",
                {"s": json.dumps({"birads_right": "2"}), "u": S_CRIT})
    assert client.get(f"/api/studies/{S_CRIT}/signoff", headers=as_rad_b).json()["integrity"] == "changed"


def test_signature_rows_are_append_only_for_the_app_role(world):
    import sqlalchemy as sa

    from app.infrastructure.database.session import engine

    async def attempt():
        async with engine.connect() as conn:
            try:
                await conn.execute(sa.text("SELECT set_config('app.platform', 'on', false)"))
                await conn.execute(sa.text("UPDATE report_signatures SET comment = 'x'"))
                return "updated"
            except Exception as exc:  # noqa: BLE001
                return str(exc)

    outcome = _run_isolated(attempt)
    assert "permission denied" in outcome, outcome


def test_study_assigned_to_someone_else(client, world, as_rad_b, as_b):
    r = _sign(client, as_rad_b, S_OTHER)
    assert r.status_code == 403 and "assigned" in r.text
    assert _sign(client, as_b, S_OTHER).status_code == 200              # a workspace admin may


def test_signer_without_a_name_must_type_one(client, world):
    r = _sign(client, world["as_noname"], S_NORM)
    assert r.status_code == 422 and "full name" in r.text
    r = _sign(client, world["as_noname"], S_NORM, full_name="Dr Nadia Noor")
    assert r.status_code == 200, r.text
    assert r.json()["signatures"][0]["statement_text"].startswith("I, Dr Nadia Noor, attest")
    (name,) = _owner_exec("SELECT full_name FROM users WHERE id = :id", {"id": world["noname"]})[0]
    assert name == "Dr Nadia Noor"


def test_doctor_sees_referred_queue_comments_but_cannot_sign(client, world, as_rad_b):
    doc = world["as_doc"]
    items = _queue(client, doc)["items"]
    assert {i["study_instance_uid"] for i in items} == {S_REF}
    assert _sign(client, doc, S_REF).status_code == 403
    r = client.post(f"/api/studies/{S_REF}/comments", headers=doc, json={"body": "Patient symptomatic, please expedite."})
    assert r.status_code == 201, r.text
    assert r.json()["author_role"] == "doctor"
    assert client.post(f"/api/studies/{S_ABN}/comments", headers=doc, json={"body": "x"}).status_code == 404
    assert client.get(f"/api/studies/{S_ABN}/signoff", headers=doc).status_code == 404
    # The radiologist sees the doctor's comment on the study.
    comments = client.get(f"/api/studies/{S_REF}/signoff", headers=as_rad_b).json()["comments"]
    assert [c["body"] for c in comments] == ["Patient symptomatic, please expedite."]
    assert client.post(f"/api/studies/{S_REF}/comments", headers=as_rad_b, json={"body": " "}).status_code == 422


def test_signed_pdf_block_renders():
    from app.reports.pdf_generator import PDFReportGenerator, _reading_status_line

    info = {
        "reading_status": "signed", "signed_at": "28/09/2026",
        "e_signature": {
            "id": "sig-1", "signer_full_name": "Dr A <B>", "signer_role": "radiologist",
            "signed_at": "2026-09-28T10:00:00+00:00", "comment": "ok & agreed",
            "statement_text": "I, Dr A, attest …", "content_hash": "f" * 64, "integrity": "changed",
        },
    }
    assert _reading_status_line(info) == ("ELECTRONICALLY SIGNED — Dr A <B> · 28/09/2026", True)
    pdf = PDFReportGenerator().generate("1.2.3", "coronary_cta", {"summary": {}, "measurements": {}},
                                        patient_info=info)
    assert pdf[:4] == b"%PDF"


@pytest.mark.skipif(not __import__("os").environ.get("MINIO_SECURE") and
                    not __import__("os").environ.get("RLS_TEST_WITH_MINIO"),
                    reason="needs a reachable MinIO (artifact store)")
def test_signed_pdf_is_frozen_served_and_tamper_evident(client, world, as_rad_b):
    """alembic 054: the first PDF after signing is frozen; later downloads return the
    same bytes; changing the stored object is detected by verify and on download."""
    S_PDF = f"{BASE}.54"
    _owner_exec(
        "INSERT INTO studies (study_instance_uid, tenant_id, patient_id, patient_name, modality, "
        "reading_status, created_at, updated_at) VALUES (:s, :t, 'MRN-54', 'Pt 54', 'MR', 'unread', "
        "now(), now())", {"s": S_PDF, "t": TB},
    )
    _add_result(S_PDF, "brain_mri", {"tumor_detected": False})
    S_NORM = S_PDF  # noqa: N806  (local alias keeps the assertions below readable)
    r = _sign(client, as_rad_b, S_NORM, comment="No acute intracranial abnormality.")
    assert r.status_code == 200, r.text

    first = client.get(f"/api/reports/{S_NORM}/brain_mri/pdf", headers=as_rad_b)
    assert first.status_code == 200, first.text[:300]
    assert first.headers["x-report-signed-copy"] == "false"
    second = client.get(f"/api/reports/{S_NORM}/brain_mri/pdf", headers=as_rad_b)
    assert second.headers["x-report-signed-copy"] == "true"
    assert second.content == first.content

    verify = client.get(f"/api/studies/{S_NORM}/signoff/verify", headers=as_rad_b).json()
    assert verify["valid"] is True and verify["documents"][0]["usecase"] == "brain_mri"

    # Append-only for the app role.
    rows = _owner_exec("SELECT object_path FROM report_signature_documents WHERE study_instance_uid = :s",
                       {"s": S_NORM})
    assert len(rows) == 1

    # Tamper with the stored copy.
    import asyncio

    from app.infrastructure.storage.client import get_artifact_store

    asyncio.new_event_loop().run_until_complete(
        get_artifact_store().put(rows[0][0], b"%PDF-1.4 forged", "application/pdf")
    )
    verify = client.get(f"/api/studies/{S_NORM}/signoff/verify", headers=as_rad_b).json()
    assert verify["valid"] is False and verify["documents"][0]["valid"] is False
    assert client.get(f"/api/reports/{S_NORM}/brain_mri/pdf", headers=as_rad_b).status_code == 409
    hits = _owner_exec("SELECT count(*) FROM audit_log WHERE action = 'signed_document_integrity_failed' "
                       "AND entity_id = :s", {"s": S_NORM})
    assert hits[0][0] >= 1


def test_ai_results_cannot_be_exported_before_sign_off(client, world, as_rad_b, as_b, monkeypatch):
    """AI-01: DICOM SR / FHIR / on-demand SR export refuse an unsigned (or since-changed) report."""
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "dicom_sr_enabled", True)
    monkeypatch.setattr(s, "fhir_enabled", True)
    S_EXP = f"{BASE}.71"
    _owner_exec(
        "INSERT INTO studies (study_instance_uid, tenant_id, patient_id, patient_name, modality, "
        "reading_status, created_at, updated_at) VALUES (:s, :t, 'MRN-71', 'Pt 71', 'MR', 'unread', "
        "now(), now())", {"s": S_EXP, "t": TB},
    )
    _add_result(S_EXP, "brain_mri", {"tumor_detected": False})
    result_id = _owner_exec("SELECT id FROM results_index WHERE study_instance_uid = :s", {"s": S_EXP})[0][0]

    refused = client.get(f"/api/reports/{S_EXP}/brain_mri/dicom-sr", headers=as_rad_b)
    assert refused.status_code == 409
    # The refusal explains itself: what blocked it, how to fix it, where to go.
    detail = refused.json()["detail"]
    assert detail["code"] == "not_signed"
    assert "not been signed" in detail["message"] and "DICOM" in detail["message"]
    assert "sign" in detail["remedy"].lower()
    assert detail["action"] == {"label": "Open report sign-off", "path": f"/study/{S_EXP}#signoff"}
    assert client.get(f"/api/reports/{S_EXP}/brain_mri/fhir", headers=as_rad_b).status_code == 409
    assert client.post(f"/api/results/{result_id}/export-dicom", headers=as_rad_b).status_code == 409
    # A portal link would show the unsigned AI findings to an outside physician.
    share = client.post(f"/api/results/{result_id}/share", headers=as_b, json={})
    assert share.status_code == 409 and share.json()["detail"]["code"] == "not_signed"
    assert "portal link" in share.json()["detail"]["message"]

    assert _sign(client, as_rad_b, S_EXP, comment="Normal study.").status_code == 200
    sr = client.get(f"/api/reports/{S_EXP}/brain_mri/dicom-sr", headers=as_rad_b)
    assert sr.status_code == 200, sr.text[:200]

    # A re-run after signing changes the signed content: exports are refused again.
    _owner_exec("UPDATE results_index SET summary = CAST(:j AS json) WHERE id = :i",
                {"j": json.dumps({"tumor_detected": True}), "i": result_id})
    again = client.get(f"/api/reports/{S_EXP}/brain_mri/dicom-sr", headers=as_rad_b)
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "changed_since_signing"
    assert "again" in again.json()["detail"]["remedy"]


def test_npi_is_validated_and_required_for_sign_off(client, world, as_rad_b, as_b, seeded, monkeypatch):
    """TEC-08: a valid NPI (Luhn) is required to sign when REQUIRE_NPI_FOR_SIGNING is on,
    and is recorded on the signature and in the verified SR."""
    from app.config import get_settings

    npi_system = "http://hl7.org/fhir/sid/us-npi"
    bad = client.patch(f"/api/practitioners/{seeded['rad_b']}", headers=as_b,
                       json={"identifier_system": npi_system, "identifier_value": "1234567890"})
    assert bad.status_code in (400, 422), bad.text

    monkeypatch.setattr(get_settings(), "require_npi_for_signing", True)
    S_NPI = f"{BASE}.81"
    _owner_exec(
        "INSERT INTO studies (study_instance_uid, tenant_id, patient_id, patient_name, modality, "
        "reading_status, created_at, updated_at) VALUES (:s, :t, 'MRN-81', 'Pt 81', 'MR', 'unread', "
        "now(), now())", {"s": S_NPI, "t": TB},
    )
    _add_result(S_NPI, "brain_mri", {"tumor_detected": False})
    _owner_exec("UPDATE users SET identifier_system = NULL, identifier_value = NULL WHERE id = :u",
                {"u": seeded["rad_b"]})
    refused = _sign(client, as_rad_b, S_NPI, comment="Normal.")
    assert refused.status_code == 422 and "NPI" in refused.text

    ok = client.patch(f"/api/practitioners/{seeded['rad_b']}", headers=as_b,
                      json={"identifier_system": npi_system, "identifier_value": "1234567893"})
    assert ok.status_code == 200, ok.text
    signed = _sign(client, as_rad_b, S_NPI, comment="Normal.")
    assert signed.status_code == 200, signed.text
    assert signed.json()["signatures"][0]["signer_npi"] == "1234567893"


def test_result_provenance_is_recorded_and_bound_to_the_signature(client, world, as_rad_b):
    """AI-02: provenance row per result, readable via the API, part of the signed content."""
    from sqlalchemy.orm import Session

    import sqlalchemy as sa
    from app.infrastructure.queue.tasks import _save_provenance

    S_PROV = f"{BASE}.91"
    _owner_exec(
        "INSERT INTO studies (study_instance_uid, tenant_id, patient_id, patient_name, modality, "
        "reading_status, created_at, updated_at) VALUES (:s, :t, 'MRN-91', 'Pt 91', 'MR', 'unread', "
        "now(), now())", {"s": S_PROV, "t": TB},
    )
    _add_result(S_PROV, "brain_mri", {"tumor_detected": False})
    rid = _owner_exec("SELECT id FROM results_index WHERE study_instance_uid = :s", {"s": S_PROV})[0][0]
    engine = sa.create_engine(OWNER_URL)
    with Session(engine) as s:
        _save_provenance(s, rid, None, "brain_mri", {"model_version": "1", "model_checksum": "abc",
                                                      "summary": {}}, TB,
                         "2026-10-02T10:00:00+00:00", "2026-10-02T10:00:05+00:00")
    engine.dispose()
    prov = client.get(f"/api/provenance/{rid}", headers=as_rad_b)
    assert prov.status_code == 200, prov.text
    body = prov.json()
    assert len(body["record_sha256"]) == 64 and body["model_version"] == "1"
    assert any(w["path"].endswith("inference_config.yaml") for w in body["weights"])

    r = _sign(client, as_rad_b, S_PROV, comment="Normal.")
    assert r.status_code == 200, r.text
    snap = _owner_exec("SELECT content_snapshot FROM report_signatures WHERE study_instance_uid = :s",
                       {"s": S_PROV})[0][0]
    assert snap["results"][0]["provenance_sha256"] == body["record_sha256"]


def test_signing_records_ai_quality_metrics(client, world, as_rad_b, as_b):
    """AI-04: one ai_report_reviews row per signed AI result; the dashboard aggregates them."""
    S_QA = f"{BASE}.95"
    _owner_exec(
        "INSERT INTO studies (study_instance_uid, tenant_id, patient_id, patient_name, modality, "
        "reading_status, created_at, updated_at) VALUES (:s, :t, 'MRN-95', 'Pt 95', 'MR', 'unread', "
        "now(), now())", {"s": S_QA, "t": TB},
    )
    _add_result(S_QA, "brain_mri", {"ai_report": {"impression": "No acute intracranial abnormality."}})
    assert _sign(client, as_rad_b, S_QA, comment="Agree.").status_code == 200
    rows = _owner_exec(
        "SELECT r.outcome, r.similarity, r.usecase_name FROM ai_report_reviews r "
        "JOIN report_signatures s ON s.id = r.signature_id WHERE s.study_instance_uid = :s", {"s": S_QA})
    assert rows == [("unchanged", 1.0, "brain_mri")]
    q = client.get("/api/admin/metrics/ai-quality", headers=as_b)
    assert q.status_code == 200, q.text
    assert any(r["usecase_name"] == "brain_mri" and r["signed_results"] >= 1 for r in q.json()["series"])
