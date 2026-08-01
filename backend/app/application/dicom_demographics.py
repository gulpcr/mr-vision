"""DICOM → FHIR demographic normalisation: person names and administrative gender.

One implementation, shared by every consumer (ingest, PDF reports, and the FHIR mapper)
so the platform cannot drift into several disagreeing interpretations of the same DICOM
tag — which is exactly what had happened before this module existed:

* ``PatientName`` was persisted raw and printed raw into PDFs ("DOE^JOHN^A^DR^JR"),
  while the UI separately replaced carets with spaces (ui/src/lib/format.ts).
* ``PatientSex`` was persisted raw as "M"/"F"/"O", while the intake table used
  "female"/"male"/"other" and tasks.py re-derived the mapping inline for LLM prompts.

**PHI note.** Nothing here persists anything. Structured name parts are produced on
demand from study metadata that is already held, and are deliberately *not* stored:
``PatientRecord`` is de-identified by design (sex + age band, no name, no DOB) and this
module exists to keep that invariant intact while still emitting a conformant
FHIR HumanName at the serialisation boundary. See FHIR_INTEGRATION_PLAN.md §4.2.

On ``birthDate``: the platform deliberately does **not** persist PatientBirthDate. It is
read at ingest solely to derive an age when the scanner omitted PatientAge, then
discarded (app/application/study_service.py). The consequence, accepted knowingly, is
that ``Patient.birthDate`` can never be exported and ``Patient?birthdate=`` search is
unavailable. ``PatientRecord.age_band`` is NOT a substitute — FHIR has no age element,
and inventing an extension for a coarse band is not worth the conformance debt.
"""
from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# DICOM PatientSex (0010,0040) is defined as M | F | O. Real-world scanners also emit
# "U" and other junk; those map to no gender at all rather than a guessed one.
# Target value set is FHIR administrativeGender, which is also what the intake table
# already uses (onboarding_service.SEX_VALUES) — so this unifies the two.
_GENDER_BY_DICOM: dict[str, str] = {
    "m": "male",
    "f": "female",
    "o": "other",
    # Accept the normalised forms too, so the function is idempotent and can be applied
    # to already-migrated rows without corrupting them.
    "male": "male",
    "female": "female",
    "other": "other",
    "unknown": "unknown",
}

FHIR_ADMINISTRATIVE_GENDERS = frozenset({"male", "female", "other", "unknown"})


def normalize_administrative_gender(raw: Any) -> str | None:
    """DICOM PatientSex → FHIR ``administrativeGender``, or None if not determinable.

    Returns None rather than "unknown" for an absent value: the column is nullable, and
    NULL ("we were not told") is the honest representation, which also lets reports keep
    rendering an em-dash instead of the word "unknown". A serialiser that needs the
    element populated maps None → "unknown" at the boundary, since FHIR's value set has
    no null member.

    Unrecognised values are dropped with a warning, deliberately: a scanner sending "U"
    or "1" must not become a confident gender assertion on a patient record.

        >>> normalize_administrative_gender("M")
        'male'
        >>> normalize_administrative_gender("female")   # idempotent
        'female'
        >>> normalize_administrative_gender("") is None
        True
    """
    if raw is None:
        return None
    key = str(raw).strip().lower()
    if not key:
        return None
    gender = _GENDER_BY_DICOM.get(key)
    if gender is None:
        logger.warning("patient_sex_unrecognised", value=str(raw)[:32])
        return None
    return gender


def parse_person_name(raw: Any) -> dict[str, Any] | None:
    """DICOM PN → FHIR ``HumanName``, or None when there is no name.

    DICOM PN is ``Family^Given^Middle^Prefix^Suffix``, optionally with up to three
    component groups separated by "=" (alphabetic=ideographic=phonetic) — only the
    alphabetic group is used. A PN carrying no caret is, per the standard, a family name
    alone; that is honoured rather than guessed at, and ``text`` always preserves the
    original so nothing is silently lost.

        >>> parse_person_name("DOE^JOHN^A^DR^JR")
        {'family': 'DOE', 'given': ['JOHN', 'A'], 'prefix': ['DR'], 'suffix': ['JR'], \
'text': 'DOE JOHN A DR JR'}
    """
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None

    # Alphabetic component group only.
    alphabetic = value.split("=")[0].strip()
    if not alphabetic:
        return None

    parts = [p.strip() for p in alphabetic.split("^")]
    # Pad to five so indexing is uniform; DICOM allows trailing components to be absent.
    parts += [""] * (5 - len(parts)) if len(parts) < 5 else []

    name: dict[str, Any] = {}
    if parts[0]:
        name["family"] = parts[0]
    given = [p for p in (parts[1], parts[2]) if p]
    if given:
        name["given"] = given
    if parts[3]:
        name["prefix"] = [parts[3]]
    if parts[4]:
        name["suffix"] = [parts[4]]

    name["text"] = format_person_name(alphabetic)
    return name or None


def format_person_name(raw: Any, fallback: str = "") -> str:
    """DICOM PN → a human-readable single line, for report headers and display.

    Intentionally identical to what the UI already renders (ui/src/lib/format.ts
    ``formatPatientName``): carets become spaces and whitespace is collapsed. PDFs
    previously printed the raw "DOE^JOHN^A^DR^JR", so this makes the PDF agree with the
    screen rather than introducing a third convention.
    """
    if raw is None:
        return fallback
    value = str(raw).split("=")[0].replace("^", " ")
    return " ".join(value.split()) or fallback
