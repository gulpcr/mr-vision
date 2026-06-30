"""Unit tests for the RBAC permission catalog and system-role definitions."""
from __future__ import annotations

from app.domain.permissions import (
    ALL_PERMISSIONS,
    SYSTEM_ROLE_NAMES,
    SYSTEM_ROLE_PERMISSIONS,
    validate_permissions,
)


def test_five_system_roles():
    assert SYSTEM_ROLE_NAMES == {"admin", "receptionist", "technician", "radiologist", "viewer"}


def test_admin_has_every_permission():
    assert set(SYSTEM_ROLE_PERMISSIONS["admin"]) == set(ALL_PERMISSIONS)


def test_role_permissions_are_subsets_of_catalog():
    for name, perms in SYSTEM_ROLE_PERMISSIONS.items():
        assert validate_permissions(perms) == [], f"{name} has unknown permissions"


def test_role_duties():
    assert "patient.onboard" in SYSTEM_ROLE_PERMISSIONS["receptionist"]
    assert "study.upload" in SYSTEM_ROLE_PERMISSIONS["technician"]
    assert "result.export" in SYSTEM_ROLE_PERMISSIONS["radiologist"]
    assert SYSTEM_ROLE_PERMISSIONS["viewer"] == ["study.view"]


def test_least_privilege_boundaries():
    # The doctor/viewer cannot export or onboard; receptionist cannot run AI.
    assert "result.export" not in SYSTEM_ROLE_PERMISSIONS["viewer"]
    assert "patient.onboard" not in SYSTEM_ROLE_PERMISSIONS["viewer"]
    assert "job.run" not in SYSTEM_ROLE_PERMISSIONS["receptionist"]
    # Only admin can manage users/roles and purge data.
    for name, perms in SYSTEM_ROLE_PERMISSIONS.items():
        if name != "admin":
            assert "user.manage" not in perms
            assert "data.purge" not in perms


def test_validate_permissions_flags_unknown():
    assert validate_permissions(["study.view", "nope.bad"]) == ["nope.bad"]
