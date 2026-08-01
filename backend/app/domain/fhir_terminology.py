"""Canonical FHIR/terminology **system URIs** — pure domain module, stdlib only.

Scope, deliberately narrow: this module holds *system* identifiers, not *codes*.

* A system URI ("http://loinc.org") names a code system. These are stable, published,
  and safe to hard-code — getting one wrong is an immediately visible failure.
* A code ("29463-7") asserts a clinical meaning. A wrong code is *invisibly* wrong: the
  receiving EHR files or trends the value under the wrong concept and nothing surfaces
  the error. Codes therefore live in per-plugin ``fhir_map.yaml`` files, ship as
  ``<TBD-verify>``, and stay disabled until signed off by someone qualified
  (FHIR_R4_COMPLIANCE_AUDIT.md steps 4-5).

Do not add a code constant here. If you need one, add it to the plugin's map.

An ``Identifier`` without a ``system`` is not matchable: "12345" is meaningless without
saying which numbering scheme issued it. In multi-tenant deployments it is worse than
meaningless — two hospitals' MRN "12345" become indistinguishable. Hence the
per-deployment systems in ``app.config`` (``fhir_patient_identifier_system``,
``fhir_accession_identifier_system``) rather than a constant here: only the operator
knows their own namespace.
"""
from __future__ import annotations

# ── DICOM ─────────────────────────────────────────────────────────────────────
# Identifier.system for any DICOM UID. Per the FHIR ImagingStudy guidance the *value*
# is additionally prefixed "urn:oid:" — use dicom_uid_identifier() rather than
# formatting it by hand.
DICOM_UID_SYSTEM = "urn:dicom:uid"

# DICOM Controlled Terminology (CID 29 modality codes, DCM audit message IDs, ...).
# Note: modality values already stored in studies.modality / series.modality ("MR",
# "CT", "PT") are already valid CID 29 codes — only this system wrapper was missing.
DCM_SYSTEM = "http://dicom.nema.org/resources/ontology/DCM"

# ── Clinical code systems ─────────────────────────────────────────────────────
LOINC_SYSTEM = "http://loinc.org"
SNOMED_SYSTEM = "http://snomed.info/sct"
# WHO ICD-10. The US clinical modification is a *different* system:
ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10"
ICD10_CM_SYSTEM = "http://hl7.org/fhir/sid/icd-10-cm"
RXNORM_SYSTEM = "http://www.nlm.nih.gov/research/umls/rxnorm"

# Units of measure. Safe to apply now — unlike clinical codes, UCUM unit strings
# ("mL", "g", "mm", "kg", "1", "{score}") need no clinical sign-off.
UCUM_SYSTEM = "http://unitsofmeasure.org"

# ── This platform's own code system ───────────────────────────────────────────
# For concepts with no signed-off standard binding. Some genuinely have none (metabolic
# tumour volume, total lesion glycolysis, Centiloid); others are awaiting verification.
# A local code is always honest: it names the platform's own concept and can never be
# mistaken for a standard one, whereas a plausible-but-wrong LOINC is filed by the
# receiving EHR under the wrong concept with nothing to surface the error.
# Single definition — both the per-plugin map loader and ObservationService use this.
LOCAL_OBSERVATION_SYSTEM = "urn:mrcv:observation"

# Identifier.system for a model artefact's content hash — the identifier that makes an
# AI-derived finding traceable to the exact model build that produced it (FHIR Device).
# A version string can be reused across rebuilds; a checksum cannot.
MODEL_CHECKSUM_IDENTIFIER_SYSTEM = "urn:mrcv:model-checksum"

# ── HL7 terminology (fixed, spec-defined value sets) ──────────────────────────
OBSERVATION_CATEGORY_SYSTEM = "http://terminology.hl7.org/CodeSystem/observation-category"
# v2-0074 Diagnostic Service Section ID — DiagnosticReport.category ("RAD", "NMR").
DIAGNOSTIC_SERVICE_SECTION_SYSTEM = "http://terminology.hl7.org/CodeSystem/v2-0074"
PROVENANCE_PARTICIPANT_TYPE_SYSTEM = (
    "http://terminology.hl7.org/CodeSystem/provenance-participant-type"
)
DATA_OPERATION_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-DataOperation"


def dicom_uid_identifier(uid: str) -> dict[str, str]:
    """Build a FHIR Identifier for a DICOM UID (StudyInstanceUID, SeriesInstanceUID).

    The ``urn:oid:`` prefix belongs on the *value*, not the system — a detail that is
    easy to get backwards, which is why this is a function and not a format string.

        >>> dicom_uid_identifier("1.2.840.113619.2.55")
        {'system': 'urn:dicom:uid', 'value': 'urn:oid:1.2.840.113619.2.55'}
    """
    cleaned = (uid or "").strip()
    if not cleaned:
        raise ValueError("cannot build a DICOM UID Identifier from an empty UID")
    if cleaned.startswith("urn:oid:"):
        cleaned = cleaned[len("urn:oid:"):]
    return {"system": DICOM_UID_SYSTEM, "value": f"urn:oid:{cleaned}"}


def identifier(system: str, value: str) -> dict[str, str] | None:
    """Build a FHIR Identifier, or None when either half is missing.

    Returns None rather than a partial Identifier: a value with no system cannot be
    matched against a foreign system, and emitting one invites a false match. Callers
    filter these out of ``identifier[]`` instead of publishing an unusable entry.
    """
    system = (system or "").strip()
    value = (value or "").strip()
    if not system or not value:
        return None
    return {"system": system, "value": value}
