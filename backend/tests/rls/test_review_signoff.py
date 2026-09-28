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
