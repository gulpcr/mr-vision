from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.interface.api.validators import validate_dicom_uid


class TestValidateDicomUID:
    def test_valid_short(self):
        assert validate_dicom_uid("1.2") == "1.2"

    def test_valid_long(self):
        uid = "1.2.840.113619.2.55.3.123456789.12345.1234567890"
        assert validate_dicom_uid(uid) == uid

    def test_valid_all_digits(self):
        assert validate_dicom_uid("12") == "12"

    def test_invalid_starts_with_dot(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_dicom_uid(".1.2.3")
        assert exc_info.value.status_code == 422

    def test_invalid_ends_with_dot(self):
        with pytest.raises(HTTPException):
            validate_dicom_uid("1.2.3.")

    def test_invalid_contains_letters(self):
        with pytest.raises(HTTPException):
            validate_dicom_uid("1.2.abc.3")

    def test_invalid_single_char(self):
        with pytest.raises(HTTPException):
            validate_dicom_uid("1")

    def test_invalid_too_long(self):
        uid = "1." + "0" * 63  # 65 chars total, exceeds 64
        with pytest.raises(HTTPException):
            validate_dicom_uid(uid)

    def test_invalid_special_chars(self):
        with pytest.raises(HTTPException):
            validate_dicom_uid("1.2.3-4")

    def test_invalid_empty(self):
        with pytest.raises(HTTPException):
            validate_dicom_uid("")

    def test_invalid_spaces(self):
        with pytest.raises(HTTPException):
            validate_dicom_uid("1.2 .3")
