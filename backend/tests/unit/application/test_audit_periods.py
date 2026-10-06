from __future__ import annotations

from datetime import datetime, timezone

from app.application.audit_archive_service import month_bounds, previous_month_bounds
from app.application.audit_review_service import previous_month, previous_week


def _d(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_previous_week_is_monday_to_monday():
    start, end = previous_week(_d(2026, 10, 2, 15, 30))  # a Friday
    assert (start, end) == (_d(2026, 9, 21), _d(2026, 9, 28))
    assert start.weekday() == 0 and end.weekday() == 0
    # On a Monday the closed week is the one before.
    assert previous_week(_d(2026, 9, 28, 2)) == (_d(2026, 9, 21), _d(2026, 9, 28))


def test_month_bounds_and_year_rollover():
    assert month_bounds(2026, 12) == (_d(2026, 12, 1), _d(2027, 1, 1))
    assert previous_month_bounds(_d(2027, 1, 1, 3)) == (_d(2026, 12, 1), _d(2027, 1, 1))
    assert previous_month_bounds(_d(2026, 3, 31)) == previous_month(_d(2026, 3, 31))
