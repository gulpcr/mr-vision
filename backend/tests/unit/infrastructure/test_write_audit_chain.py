"""_write_audit (the Celery/sync-session counterpart to PgAuditRepository.save)
must extend the same hash chain — not silently fall outside it. Lives under
tests/unit/, not tests/integration/: that package's conftest.py mocks out the
entire app.infrastructure.queue.tasks module (so the FastAPI app can import
without Celery/heavy ML deps), which would make _write_audit a MagicMock
no-op here instead of the real function under test.
"""
from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.domain.audit_chain import compute_audit_row_hash
from app.infrastructure.database.models import AuditLogRecord, Base
from app.infrastructure.queue.tasks import _write_audit


def test_sync_write_audit_extends_the_same_chain(tmp_path):
    """Two consecutive sync writes must form a proper seq/prev_hash chain,
    matching the same scheme PgAuditRepository.save() uses for async writes —
    same domain compute_audit_row_hash function, same HMAC secret derivation.
    """
    db_path = tmp_path / "chain_test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    _write_audit(
        session, "job_completed", "job", "job-1",
        {"study_uid": "1.2.3"}, tenant_id="hospital-a",
    )
    _write_audit(
        session, "job_completed", "job", "job-2",
        {"study_uid": "1.2.4"}, tenant_id="hospital-a",
    )

    rows = session.execute(
        select(AuditLogRecord).order_by(AuditLogRecord.seq.asc())
    ).scalars().all()
    assert [r.seq for r in rows] == [1, 2]
    assert rows[0].prev_hash is None
    assert rows[1].prev_hash == rows[0].row_hash

    from app.config import derive_secret
    secret = derive_secret("audit-chain-v1")
    expected = compute_audit_row_hash(
        secret=secret, seq=rows[0].seq, action=rows[0].action,
        entity_type=rows[0].entity_type, entity_id=rows[0].entity_id,
        actor=rows[0].actor, details=rows[0].details,
        tenant_id=rows[0].tenant_id, prev_hash=rows[0].prev_hash,
    )
    assert rows[0].row_hash == expected

    session.close()
    engine.dispose()


def test_write_audit_defaults_tenant_id_when_none_given(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'chain_test2.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    _write_audit(session, "job_completed", "job", "job-1", {})

    row = session.execute(select(AuditLogRecord)).scalar_one()
    assert row.tenant_id == "default"

    session.close()
    engine.dispose()
