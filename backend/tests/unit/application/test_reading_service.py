"""Unit tests for ReadingService pure logic (status constants + TAT derivation)."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.application.reading_service import (
    IN_PROGRESS,
    REPORTED,
    SIGNED,
    UNREAD,
    VALID_STATUSES,
    ReadingService,
)


def test_status_constants():
    assert VALID_STATUSES == (UNREAD, IN_PROGRESS, REPORTED, SIGNED)


def _rec(**kw):
    base = dict(
        study_instance_uid="1.2.3",
        reading_status=UNREAD,
        assigned_to=None,
        assigned_to_username=None,
        assigned_at=None,
        reported_at=None,
        signed_at=None,
        created_at=datetime(2026, 1, 1, 12, 0, 0),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_reading_dict_unread_has_no_tat():
    d = ReadingService.reading_dict(_rec())
    assert d["reading_status"] == UNREAD
    assert d["tat_report_minutes"] is None
    assert d["tat_signoff_minutes"] is None


def test_reading_dict_computes_turnaround_minutes():
    created = datetime(2026, 1, 1, 12, 0, 0)
    rec = _rec(
        reading_status=SIGNED,
        assigned_to="u1",
        assigned_to_username="rad1",
        reported_at=created + timedelta(minutes=30),
        signed_at=created + timedelta(minutes=90),
    )
    d = ReadingService.reading_dict(rec)
    assert d["tat_report_minutes"] == 30.0
    assert d["tat_signoff_minutes"] == 90.0
    assert d["assigned_to_username"] == "rad1"
