"""Unit tests for the RBAC permission catalog, system roles and grant matching."""
from __future__ import annotations

from app.domain.permissions import (
    STUDY_READ,
    SYSTEM_ROLE_NAMES,
    SYSTEM_ROLE_PERMISSIONS,
    has_permission,
    is_referral_scoped,
    validate_permissions,
)


def test_system_roles():
    assert SYSTEM_ROLE_NAMES == {
        "admin", "radiologist", "doctor", "technician", "receptionist", "viewer",
    }


def test_admin_has_every_permission():
    assert SYSTEM_ROLE_PERMISSIONS["admin"] == ["*"]
    assert has_permission(SYSTEM_ROLE_PERMISSIONS["admin"], "data.purge")
    assert has_permission(SYSTEM_ROLE_PERMISSIONS["admin"], "anything.at.all")


def test_role_permissions_are_subsets_of_catalog():
    for name, perms in SYSTEM_ROLE_PERMISSIONS.items():
        assert validate_permissions(perms) == [], f"{name} has unknown permissions"


def test_role_duties():
    assert has_permission(SYSTEM_ROLE_PERMISSIONS["receptionist"], "patient.onboard")
    assert has_permission(SYSTEM_ROLE_PERMISSIONS["technician"], "study.upload")
    assert has_permission(SYSTEM_ROLE_PERMISSIONS["radiologist"], "result.approve")
    assert has_permission(SYSTEM_ROLE_PERMISSIONS["doctor"], "result.export")
    assert set(SYSTEM_ROLE_PERMISSIONS["viewer"]) == {"study.view", "dashboard.view"}


def test_least_privilege_boundaries():
    assert not has_permission(SYSTEM_ROLE_PERMISSIONS["viewer"], "result.export")
    assert not has_permission(SYSTEM_ROLE_PERMISSIONS["viewer"], "patient.onboard")
    assert not has_permission(SYSTEM_ROLE_PERMISSIONS["receptionist"], "job.run")
    # Only admin can manage users/roles/settings and purge data.
    for name, perms in SYSTEM_ROLE_PERMISSIONS.items():
        if name != "admin":
            for key in ("user.manage", "role.manage", "settings.manage", "data.purge"):
                assert not has_permission(perms, key), f"{name} must not have {key}"


def test_doctor_is_referral_scoped():
    doctor = SYSTEM_ROLE_PERMISSIONS["doctor"]
    assert is_referral_scoped(doctor)
    assert has_permission(doctor, STUDY_READ)
    assert not has_permission(doctor, "study.view")
    assert not is_referral_scoped(SYSTEM_ROLE_PERMISSIONS["radiologist"])


def test_grant_matching_grammar():
    assert has_permission(["study.*"], "study.delete")
    assert not has_permission(["study.*"], "job.run")
    assert has_permission(["study.view"], "study.view.referred")  # broader implies scoped
    assert not has_permission(["study.view.referred"], "study.view")
    assert has_permission(["alert.view"], "study.view||alert.view")
    assert not has_permission([], "study.view")


def test_validate_permissions_flags_unknown():
    assert validate_permissions(["study.view", "nope.bad"]) == ["nope.bad"]
    assert validate_permissions(["*", "study.*"]) == []
