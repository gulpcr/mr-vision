"""Per-tenant workspace settings (report header/signatories, timezone) and branding."""
from __future__ import annotations

import re
from typing import Any

from app.infrastructure.database.models import TenantBrandingRecord, TenantSettingsRecord

SETTINGS_FIELDS = (
    "institution_name", "institution_address", "report_header", "report_footer",
    "signatory_name", "signatory_title", "signatory_qualifications",
    "secondary_signatory_name", "timezone",
)
BRANDING_FIELDS = ("display_name", "logo_data_url", "primary_color", "accent_color")

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
# Raster images only: an SVG data URL can carry script.
_LOGO = re.compile(r"^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$")
MAX_LOGO_BYTES = 300_000


class SettingsValidationError(ValueError):
    pass


class TenantSettingsService:
    def __init__(self, session):
        self._session = session

    async def _settings_row(self, tenant_id: str) -> TenantSettingsRecord:
        row = await self._session.get(TenantSettingsRecord, tenant_id)
        if row is None:
            row = TenantSettingsRecord(tenant_id=tenant_id)
            self._session.add(row)
            await self._session.flush()
        return row

    async def _branding_row(self, tenant_id: str) -> TenantBrandingRecord:
        row = await self._session.get(TenantBrandingRecord, tenant_id)
        if row is None:
            row = TenantBrandingRecord(tenant_id=tenant_id)
            self._session.add(row)
            await self._session.flush()
        return row

    async def get_settings(self, tenant_id: str) -> dict[str, Any]:
        row = await self._session.get(TenantSettingsRecord, tenant_id)
        return {f: getattr(row, f, None) if row else None for f in SETTINGS_FIELDS}

    async def update_settings(self, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        row = await self._settings_row(tenant_id)
        for field in SETTINGS_FIELDS:
            if field in payload:
                value = payload[field]
                setattr(row, field, (str(value).strip() or None) if value is not None else None)
        await self._session.flush()
        return await self.get_settings(tenant_id)

    async def get_branding(self, tenant_id: str) -> dict[str, Any]:
        row = await self._session.get(TenantBrandingRecord, tenant_id)
        return {f: getattr(row, f, None) if row else None for f in BRANDING_FIELDS}

    async def update_branding(self, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        for color in ("primary_color", "accent_color"):
            value = payload.get(color)
            if value and not _HEX.match(value):
                raise SettingsValidationError(f"{color} must be a #RRGGBB colour")
        logo = payload.get("logo_data_url")
        if logo:
            if not _LOGO.match(logo):
                raise SettingsValidationError("Logo must be a PNG, JPEG or WebP data URL")
            if len(logo) > MAX_LOGO_BYTES * 4 // 3 + 64:
                raise SettingsValidationError("Logo must be smaller than 300 KB")
        row = await self._branding_row(tenant_id)
        for field in BRANDING_FIELDS:
            if field in payload:
                setattr(row, field, payload[field] or None)
        await self._session.flush()
        return await self.get_branding(tenant_id)

    async def report_profile(self, tenant_id: str) -> dict[str, str]:
        """PDF report overrides (settings.* names used by reports/pdf_generator.py)."""
        s = await self.get_settings(tenant_id)
        profile = {
            "report_institution_name": s.get("institution_name"),
            "report_signatory_primary": s.get("signatory_name"),
            "report_signatory_secondary": s.get("secondary_signatory_name"),
            "mri_report_signatory_name": s.get("signatory_name"),
            "mri_report_signatory_title": s.get("signatory_title"),
            "mri_report_signatory_qualifications": s.get("signatory_qualifications"),
        }
        return {k: v for k, v in profile.items() if v}
