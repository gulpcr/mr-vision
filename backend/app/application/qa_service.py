from __future__ import annotations

from typing import Any

import structlog

from app.domain.models import Series, Study

logger = structlog.get_logger(__name__)


class QACheck:
    """A single QA check result."""
    def __init__(self, name: str, passed: bool, severity: str = "warning", message: str = ""):
        self.name = name
        self.passed = passed
        self.severity = severity  # "blocking", "warning", "info"
        self.message = message


class QAService:
    """Pre-flight quality assurance checks before inference."""

    def run_checks(
        self, study: Study, series: list[Series], required_sequences: list[str] | None = None
    ) -> list[QACheck]:
        checks = []
        checks.append(self._check_has_series(series))
        checks.append(self._check_modality(study))
        if required_sequences:
            checks.append(self._check_required_sequences(series, required_sequences))
        checks.append(self._check_slice_thickness(series))
        checks.append(self._check_instance_count(series))
        return checks

    def has_blocking_issues(self, checks: list[QACheck]) -> bool:
        return any(not c.passed and c.severity == "blocking" for c in checks)

    def to_dict_list(self, checks: list[QACheck]) -> list[dict[str, Any]]:
        return [
            {
                "name": c.name,
                "passed": c.passed,
                "severity": c.severity,
                "message": c.message,
            }
            for c in checks
        ]

    def _check_has_series(self, series: list[Series]) -> QACheck:
        if not series:
            return QACheck("has_series", False, "blocking", "No series found in study")
        return QACheck("has_series", True, "info", f"{len(series)} series found")

    def _check_modality(self, study: Study) -> QACheck:
        if study.modality and study.modality.upper() not in ("MR", "CT"):
            return QACheck(
                "supported_modality", False, "blocking",
                f"Unsupported modality: {study.modality}"
            )
        return QACheck("supported_modality", True, "info", f"Modality: {study.modality or 'MR'}")

    def _check_required_sequences(
        self, series: list[Series], required: list[str]
    ) -> QACheck:
        descriptions = [
            (s.series_description or "").upper() for s in series
        ]
        missing = []
        for seq in required:
            seq_upper = seq.upper()
            if not any(seq_upper in d for d in descriptions):
                missing.append(seq)
        if missing:
            return QACheck(
                "required_sequences", False, "warning",
                f"Missing sequences: {', '.join(missing)}"
            )
        return QACheck("required_sequences", True, "info", "All required sequences present")

    def _check_slice_thickness(self, series: list[Series]) -> QACheck:
        for s in series:
            if s.slice_thickness and s.slice_thickness > 10.0:
                return QACheck(
                    "slice_thickness", False, "warning",
                    f"Series {s.series_description}: slice thickness {s.slice_thickness}mm > 10mm"
                )
        return QACheck("slice_thickness", True, "info", "Slice thickness within normal range")

    def _check_instance_count(self, series: list[Series]) -> QACheck:
        for s in series:
            if s.num_instances < 5:
                return QACheck(
                    "instance_count", False, "warning",
                    f"Series {s.series_description}: only {s.num_instances} instances"
                )
        return QACheck("instance_count", True, "info", "Instance counts adequate")
