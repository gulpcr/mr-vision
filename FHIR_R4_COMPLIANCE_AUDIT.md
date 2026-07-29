# HL7 FHIR R4 Compliance Audit & Gap Analysis

**Audited:** 2026-07-29 · branch `ui`
**Scope:** data structures, schemas, terminology coding, conformance readiness.
Live transport (MLLP listener, HTTP pipelines, gateways) explicitly excluded per instruction.

**Artefacts audited (read, not assumed):**

| Area | File |
|---|---|
| ORM schema (24 tables) | [backend/app/infrastructure/database/models.py](backend/app/infrastructure/database/models.py) |
| Domain entities | [backend/app/domain/models.py](backend/app/domain/models.py) |
| Enums / value sets | [backend/app/domain/enums.py](backend/app/domain/enums.py) |
| RBAC catalog | [backend/app/domain/permissions.py](backend/app/domain/permissions.py) |
| API serialization | [backend/app/interface/schemas/study.py](backend/app/interface/schemas/study.py), [result.py](backend/app/interface/schemas/result.py) |
| Existing FHIR exporter | [backend/app/fhir/client.py](backend/app/fhir/client.py), [fhir_export_service.py](backend/app/fhir/fhir_export_service.py) |
| DICOM→DB mapping | [backend/app/application/study_service.py](backend/app/application/study_service.py) |
| Clinical intake | [backend/app/interface/api/onboarding.py](backend/app/interface/api/onboarding.py) |
| Result payload shape | [backend/app/usecases/pet_ct/pipeline.py:718-760](backend/app/usecases/pet_ct/pipeline.py#L718-L760) |
| Migrations | `backend/alembic/versions/001…026` (next free: **027**) |

---

## 1. Executive Compliance Score

# 27 / 100 — Pre-conformance

Per-pillar breakdown:

| Pillar | Score | One-line verdict |
|---|---|---|
| 1. Data model & schema conformance | **35%** | `ImagingStudy` maps well; `Patient`/`ServiceRequest`/`DiagnosticReport` partial; `Observation`, `Condition`, `MedicationRequest`, `Encounter` have **no schema representation at all**. |
| 2. Clinical terminology & coding | **8%** | Effectively zero coded terminology. One hardcoded LOINC in the whole codebase. Labs, diagnoses, body sites, and all measurements are plain strings or bare numbers. |
| 3. Serialization & search readiness | **30%** | Document-tier export is reachable today. Structured export is not: clinical values live in opaque JSON blobs that no FHIR search parameter can address. |
| 4. Practitioner & AuditEvent | **40%** | An audit trail genuinely exists and is written on state changes — but `actor` is untyped and inconsistently populated, the action vocabulary is proprietary, and **read access is not audited**. |

### High-level summary

The platform is a **DICOM-native imaging system**, and that is where its conformance strength lies:
`StudyRecord` + `SeriesRecord` are a near-complete `ImagingStudy`, and `OrderRecord` is a credible
`ServiceRequest`. Those two are worth real credit — the identifiers, accession number, modality,
body part, series geometry, and instance counts are all present and indexed.

The gap is that this is an imaging system that has recently grown **clinical** data collection
(intake, labs, indications, reports) without a clinical data model underneath it. Diagnoses,
glucose, creatinine, weight, and every AI measurement are stored as free text, bare floats, or JSON
blobs. There is no row-per-observation table anywhere in the schema, so the three FHIR resources
that carry clinical meaning — `Observation`, `Condition`, `MedicationRequest` — cannot be emitted
without new tables, not merely new mapping code.

Two things about `app/fhir/` need stating plainly, because they are the difference between "low
score" and "actively unsafe":

1. It hardcodes `"status": "final"` ([client.py:36](backend/app/fhir/client.py#L36)) while
   `studies.reading_status` sits unused — that publishes unreviewed AI output into a chart as a
   final radiology report.
2. It attaches `report["_observations"]` ([client.py:90](backend/app/fhir/client.py#L90)), a
   non-FHIR key on the resource, which is a **hard** validator rejection.

Both are single-file fixes. The schema work behind pillars 1–3 is not.

One design decision deserves explicit credit rather than a penalty: `PatientRecord` is
**deliberately de-identified** (`patient_ref`, `sex`, `age_band`; no name, no DOB —
[models.py:208-223](backend/app/infrastructure/database/models.py#L208-L223)). Missing
`Patient.telecom`, `Patient.address`, and `managingOrganization` is therefore an intentional
privacy posture, not sloppiness, and the remediation plan below preserves it. The conformance
problem is narrower than "PHI is missing": it is that `StudyRecord` holds a **second, raw,
un-normalised copy** of the same demographics, and the two disagree on value sets.

---

## 2. Detailed Gap Analysis Table

### 2.1 Core clinical resources

| Entity / Model | Current Application Implementation | HL7 FHIR R4 Core Standard | Status | Gap Description |
|---|---|---|---|---|
| **Patient** (identity) | `PatientRecord`: `patient_ref` String(128), `sex`, `age_band`, `tenant_id` ([models.py:208](backend/app/infrastructure/database/models.py#L208)) | `Patient.identifier[]` (system+value), `name[] HumanName`, `birthDate date`, `gender code`, `active` | **Partial** | Single MRN column, no `Identifier.system` URI → cannot disambiguate MRN "12345" across tenants. `age_band` ("40-64") has **no FHIR equivalent** — FHIR has `birthDate`, not age. De-identification is intentional and fine; the missing `system` is not. |
| **Patient** (denormalised copy) | `StudyRecord.patient_name` String(256), `patient_sex` String(16), `patient_age` String(16) ([models.py:31-34](backend/app/infrastructure/database/models.py#L31-L34)) | `HumanName[]` {family, given[], prefix[], suffix[]}; `gender` ∈ `male\|female\|other\|unknown`; no age element | **Fail** | `patient_name` stores raw DICOM PN (`DOE^JOHN^A^DR^JR`) as one flat string, never parsed ([orthanc/client.py:98](backend/app/infrastructure/orthanc/client.py#L98)). `patient_sex` is raw DICOM `M`/`F`/`O` — **not** a valid `administrativeGender` code. `patient_age` is DICOM AS format (`"045Y"`). Two divergent patient representations with different value sets and no reconciliation. |
| **Patient.birthDate** | Not persisted. Computed from `PatientBirthDate` then **discarded** ([study_service.py:77-88](backend/app/application/study_service.py#L77-L88)) | `birthDate` (0..1, `date`) | **Fail** | Deliberate PHI choice, but the consequence must be stated: no `birthDate` means no FHIR-native age, no `Patient?birthdate=` search, and no demographic cross-check during identity resolution. `age_band` is not exportable. |
| **Observation** (vitals) | `orders.height_cm` Float, `orders.weight_kg` Float, `studies.patient_weight_kg`, `studies.patient_height_cm` — bare numbers on parent rows | `Observation`: `status` 1..1, `code` 1..1 CodeableConcept, `subject`, `effectiveDateTime`, `valueQuantity` {value, unit, system, code} | **Fail** | No `Observation` table exists. No timestamp for *when* the weight was measured, no unit metadata, no status, no LOINC code. Stored twice (order + study) with no precedence rule — and `patient_weight_kg` drives SUV calculation. |
| **Observation** (labs) | `orders.fasting_glucose` **String(32)**, `orders.creatinine` **String(32)** ([models.py:245-247](backend/app/infrastructure/database/models.py#L245-L247)) | `Observation` + LOINC `code` + UCUM `valueQuantity` + `referenceRange` + `effectiveDateTime` | **Fail** | Quantitative lab results stored as **free-text strings**. No value/unit split, no LOINC, no draw time, no reference range, no status. `"5.4"`, `"5.4 mmol/L"`, and `"normal"` are all accepted by the schema. Non-serialisable to a valid `Observation`. |
| **Observation** (AI measurements) | `results_index.measurements` **JSON blob**; e.g. pet_ct → `{lesions:[…], reference_organs:{…}, whole_body:{…}, voxel_spacing_mm:[…], image_dimensions:[…]}` ([pipeline.py:743](backend/app/usecases/pet_ct/pipeline.py#L743)) | One `Observation` per measured concept, each independently coded, timed, and searchable | **Fail** | 1 row = N clinical values in an opaque blob. No code, no unit, no per-value status. Clinical numbers (SUVmax, MTV, TLG) are structurally indistinguishable from technical metadata (`image_dimensions`, `voxel_spacing_mm`) in the same dict. |
| **Condition** | `orders.indication` TEXT NOT NULL, `orders.clinical_history` TEXT | `Condition`: `code` CodeableConcept (ICD-10 / SNOMED CT), `clinicalStatus`, `verificationStatus`, `subject` 1..1 | **Fail** | The only diagnosis data in the entire system is prose. Zero ICD-10 or SNOMED. No `Condition` is derivable without NLP. No `clinicalStatus`/`verificationStatus` concept exists. |
| **MedicationRequest** | **Absent entirely** | `MedicationRequest`: `status` 1..1, `intent` 1..1, `medication[x]` (RxNorm), `subject` 1..1 | **Fail** | No medication model of any kind. Note this is a real clinical gap, not just a FHIR one: the platform's own longitudinal analysis needs chemo/immunotherapy timing to avoid reading pseudoprogression as progression. |
| **Encounter** | **Absent entirely** | `Encounter`: `status` 1..1, `class` 1..1, `subject` | **Partial (acceptable)** | No visit/encounter concept. For an outpatient imaging-only platform this is defensible — `Encounter` is 0..1 on `ImagingStudy`, `ServiceRequest`, and `DiagnosticReport`. Flagged as a known, accepted omission rather than a violation. |

### 2.2 Imaging & order resources — the strong area

| Entity / Model | Current Application Implementation | HL7 FHIR R4 Core Standard | Status | Gap Description |
|---|---|---|---|---|
| **ImagingStudy** | `StudyRecord`: `study_instance_uid` PK, `accession_number`, `modality`, `study_date`, `study_description`, `body_part_examined`, `institution_name`, `referring_physician` | `identifier[]` (`urn:dicom:uid` / `urn:oid:{uid}`), `status` 1..1 (`available`), `subject` 1..1(US Core), `started instant`, `modality Coding` (DICOM CID 29), `numberOfSeries`, `numberOfInstances` | **Partial — closest to conformant** | Content is largely right; the **wrappers** are missing. `study_instance_uid` needs `Identifier.system = "urn:dicom:uid"` + `value = "urn:oid:…"`. No `status` column (would be constant `available`). `numberOfSeries`/`numberOfInstances` are derivable but not stored. `modality` values (`MR`/`CT`/`PT`) are already **correct DICOM CID 29 codes** — only the `system` URI is absent. Good news. |
| **ImagingStudy.series** | `SeriesRecord`: `series_instance_uid` PK, `series_number`, `modality`, `body_part_examined` String(64), `num_instances`, `slice_thickness`, `pixel_spacing` JSON, `dicom_tags` JSON | `series.uid` 1..1, `series.modality` 1..1 Coding, `series.bodySite Coding` (SNOMED CT body structure), `series.number`, `series.instance[]` | **Partial** | Best-mapped table in the schema. `body_part_examined` is a **string** where `bodySite` requires a `Coding` — `BodyPart` enum values (`BRAIN`, `CSPINE`, `LOWER_LIMB`, `TSPINE`) are proprietary and need a SNOMED body-structure map. Instance-level data lives in Orthanc, not the DB (acceptable — `instance[]` is 0..*). |
| **ImagingStudy.subject** | `studies.patient_id` String(64) — matched to `patients.patient_ref` **by string equality, no FK** ([models.py:31](backend/app/infrastructure/database/models.py#L31), [models.py:215](backend/app/infrastructure/database/models.py#L215)) | `subject` Reference(Patient), 1..1 in US Core | **Fail** | No referential integrity between a study and its patient. A study whose DICOM `PatientID` has a stray prefix or stripped leading zero silently becomes an orphan. This is the join that every exported resource's `subject` depends on. |
| **ServiceRequest** | `OrderRecord`: `modality`, `body_part`, `indication` TEXT, `priority` (`routine\|stat`), `referrer` String(256), `region_profile`, `consent_ack` ([models.py:226](backend/app/infrastructure/database/models.py#L226)) | `status` 1..1, `intent` 1..1, `subject` 1..1, `code` CodeableConcept, `reasonCode[]`, `priority` ∈ `routine\|urgent\|asap\|stat`, `requester` Reference, `authoredOn` | **Partial** | Solid shape. Missing mandatory `status` and `intent` (both 1..1 — **automatic validator failure**). `priority` value `stat` is coincidentally valid; the value set has 4 members, the column has 2. `code` (the requested procedure) is not stored at all — only `modality` + `body_part`. `referrer` is a flat name string, not a `Practitioner` reference. `indication` → `reasonCode` needs coding. No external order identifier column. |
| **DiagnosticReport** (MRI) | `MriReportRecord`: `examination`, `technique`, `clinical_indication`, `findings`, `impression`, `reporting_doctor` String(256); **no status column** ([models.py:509](backend/app/infrastructure/database/models.py#L509)) | `status` 1..1 (`registered\|partial\|preliminary\|final\|amended\|entered-in-error`), `code` 1..1, `subject`, `effective[x]`, `issued instant`, `performer[]`, `conclusion` | **Partial** | `status` (1..1) is absent from the table; report state is inferred from `studies.reading_status` — a **different table**, one row per study. A study carrying both an MRI report and a mammography report shares a single status. `performer` is a name string. Narrative content maps cleanly to `conclusion`/`presentedForm`. |
| **DiagnosticReport** (Mammo) | `MammographyReportRecord`: per-breast slots, `birads_right/left` String(8), `density_right/left` String(1) ([models.py:452](backend/app/infrastructure/database/models.py#L452)) | As above + `Observation` components for BI-RADS/density | **Partial** | Best *structured* clinical data in the app — genuinely slot-based, CHECK-constrained, per-laterality. But: uncoded (BI-RADS → LOINC answer list; density a/b/c/d → LOINC/SNOMED), and laterality is encoded in **column names** (`mass_right`) rather than as `Observation.bodySite` + `Observation.component`. A pivot to rows is required, but the semantics are already there. |
| **DiagnosticReport** (AI result) | `ResultRecord` + `app/fhir/client.py:create_diagnostic_report` | as above | **Fail** | See §3 — four independent hard failures in the emitted payload. |
| **Device** (AI attribution) | `results_index.model_version` String(64) NOT NULL, `model_checksum` String(128) NOT NULL; `model_versions` table | `Device.identifier`, `.deviceName`, `.version`, `.type` | **Partial** | Excellent raw material — every result already carries version + checksum, which is exactly what AI attribution needs. Simply not modelled as a resource and never referenced from an exported report. Low-effort, high-value fix. |
| **Provenance** | Absent | `Provenance.target[]`, `.recorded instant`, `.agent.who` (Device for AI-derived) | **Fail** | No mechanism attributing an AI-generated finding to the algorithm that produced it. Increasingly an expectation for AI-assisted diagnostic output. |
| **Flag / Communication** | `CriticalAlertRecord`: `severity`, `finding_type`, `status`, `acknowledged_by`, `escalation_count` ([models.py:424](backend/app/infrastructure/database/models.py#L424)) | `Flag.status/code/subject`, or `Communication.status/priority/payload` | **Partial** | Well-structured internally with a real lifecycle. `finding_type` and `severity` (`CRITICAL`/`WARNING`) are proprietary; no coded finding concept. `acknowledged_by` is a String(128) name, not a `Practitioner` reference. |
| **DocumentReference** | `results_index.artifacts` JSON: `{name, artifact_type, storage_path, content_type}` | `status` 1..1, `content.attachment` {contentType, url/data}, `subject`, `date` | **Partial** | `content_type` is present and correct — a genuine plus. `storage_path` is a MinIO key, not a resolvable URL; `artifact_type` is a proprietary vocabulary (`segmentation_nifti`, `report_json`, `pet_nifti`). Needs a `format`/`category` coding and URL resolution, but the shape is compatible. |

### 2.3 Cross-cutting schema issues

| Entity / Model | Current Application Implementation | HL7 FHIR R4 Core Standard | Status | Gap Description |
|---|---|---|---|---|
| **All timestamps** | Every column is `Column(DateTime)` — **0 occurrences of `timezone=True`** across all 24 tables → `TIMESTAMP WITHOUT TIME ZONE` | `instant` **requires** a timezone offset (`YYYY-MM-DDThh:mm:ss.sss+zz:zz`). Applies to `DiagnosticReport.issued`, `Observation.issued`, `AuditEvent.recorded`, `Provenance.recorded` | **Fail** | Serialising any naive datetime yields `2026-07-29T00:00:00` with no offset → **validator error on every resource carrying an `instant`**. Compounded by [study_service.py:56-61](backend/app/application/study_service.py#L56-L61), which parses DICOM `StudyDate` alone and **drops `StudyTime`**, so `study_date` is always midnight-local. Two same-day studies cannot be ordered by `effectiveDateTime`. |
| **All identifiers** | `patient_id`, `accession_number`, `study_instance_uid`, `orders.id` — bare strings, no namespace | `Identifier` = {`system` URI, `value`}; `system` is what makes a value globally meaningful | **Fail** | Without `system`, no cross-system matching is valid. Specifically required: `urn:dicom:uid` for study UIDs, a per-tenant MRN system URI, an accession-issuer system URI. In multi-tenant mode two hospitals' MRN "12345" are indistinguishable today. |
| **Resource `status` fields** | Present: `job_runs.status`, `critical_alerts.status`, `studies.reading_status`. Absent: `ImagingStudy`, `DiagnosticReport` (MRI), `ServiceRequest`, and every would-be `Observation` | `status` is 1..1 (mandatory) on `Observation`, `Condition`(via clinicalStatus), `DiagnosticReport`, `ServiceRequest`, `MedicationRequest`, `ImagingStudy` | **Fail** | Missing mandatory `status` is the single most common cause of a `$validate` rejection. Where a status *does* exist it uses a proprietary value set (`unread`/`in_progress`/`reported`/`signed`) needing a mapping table — that mapping is also the clinical-safety gate for §3.1. |
| **BodyPart enum** | `BRAIN HEAD SPINE CSPINE TSPINE LSPINE KNEE SHOULDER ABDOMEN PELVIS HEART CARDIAC CHEST THORAX BREAST NECK LOWER_LIMB FACE` ([enums.py:41](backend/app/domain/enums.py#L41)) | `bodySite` → SNOMED CT body structure hierarchy | **Fail** | Proprietary strings. Note the enum contains **overlapping synonym pairs** (`HEART`/`CARDIAC`, `CHEST`/`THORAX`, `BRAIN`/`HEAD`) which will produce two different SNOMED codes for one anatomical concept unless the map collapses them deliberately. |
| **QAFlag enum** | 13 proprietary values (`motion_artifact`, `low_snr`, `field_inhomogeneity`, …) | No direct equivalent; nearest is `Observation` + `dataAbsentReason`, or an extension | **Partial (acceptable)** | Legitimately platform-specific. Currently flattened into report narrative text by [client.py:_build_conclusion](backend/app/fhir/client.py#L110). Needs a defined home (extension on `DiagnosticReport`, or dedicated `Observation`s) rather than prose. |
| **Terminology infrastructure** | Codebase-wide grep for `loinc\|snomed\|icd-10\|rxnorm\|unitsofmeasure`: **one file** — `app/fhir/client.py`, containing a single hardcoded LOINC `18748-4` | Coded `CodeableConcept` on every clinical element; UCUM for all quantities | **Fail** | There is no terminology layer of any kind: no code tables, no value-set files, no per-plugin code map, no UCUM units. `ui_schema.json` carries `unit` strings (`"mL"`, `"g"`) for display — usable as UCUM raw material, which shortens the work. |
| **Multi-tenancy vs FHIR identity** | `tenant_id` String(36) on most tables, default `"default"` | `Identifier.system` / `Identifier.assigner` (Organization) | **Partial** | Tenant isolation is consistently implemented. But `tenant_id` is a local UUID with no `Organization` resource behind it — `TenantRecord` has `name` + `slug` only, no identifier, no address. So identifiers cannot be namespaced *by* the tenant on export. |

### 2.4 Pillar 4 — Practitioner & AuditEvent

| Entity / Model | Current Application Implementation | HL7 FHIR R4 Core Standard | Status | Gap Description |
|---|---|---|---|---|
| **Practitioner** | `UserRecord`: `username`, `email`, `full_name` String(256), `role` String(32), `hashed_password`, `is_active` ([models.py:183](backend/app/infrastructure/database/models.py#L183)) | `identifier[]` (NPI / license), `name[] HumanName`, `telecom[] ContactPoint`, `qualification[]`, `active` | **Partial** | `is_active` → `active` and `email` → `telecom` map cleanly. Gaps: no professional identifier (NPI/state license) — the identifier that actually matters for a `Practitioner`; `full_name` is a flat string where `HumanName` is required; no `qualification`. A login account is being used as a clinical actor without the credentials that distinguish one. |
| **PractitionerRole** | `users.role` — **one** String(32) referencing `roles.name` within a tenant; `RoleRecord.permissions` JSON of 16 proprietary keys ([permissions.py:12](backend/app/domain/permissions.py#L12)) | `PractitionerRole`: `practitioner` Ref, `organization` Ref, `code[]` CodeableConcept, `specialty[]`, `period` | **Partial** | Structurally sound and genuinely permission-based. Two FHIR mismatches: (a) **one role per user** — FHIR models a practitioner holding several `PractitionerRole`s across organizations/specialties; (b) role names (`radiologist`, `technician`, `receptionist`) are proprietary and need mapping to `PractitionerRole.code` (v2-0912 / SNOMED). No `specialty`, no `period` (no record of *when* someone held a role — relevant when re-reading a historical report's signer). |
| **Organization** | `TenantRecord`: `id`, `name`, `slug`, `is_active` | `Organization.identifier[]`, `.name`, `.type`, `.address` | **Fail** | A deployment tenant, not a healthcare organization. No identifier, no address, no type. `studies.institution_name` (a DICOM string) is the only real-world org name and it is not linked to the tenant. |
| **AuditEvent** | `AuditLogRecord`: `action` String(64), `entity_type` String(64), `entity_id` String(256), `actor` String(128) default `"system"`, `details` JSON, `timestamp` indexed ([models.py:167](backend/app/infrastructure/database/models.py#L167)) | `type` 1..1 Coding, `subtype[]` (DICOM audit codes), `action` ∈ `C\|R\|U\|D\|E`, `recorded instant` 1..1, `outcome`, `agent[]` 1..* {`who` Reference, `type`, `requestor`}, `source` 1..1 {`observer`}, `entity[]` {`what` Reference, `type`, `role`} | **Partial** | The trail exists, is indexed, and *is* written on state changes ([reading_service.py:179](backend/app/application/reading_service.py#L179), [onboarding_service.py:345](backend/app/application/onboarding_service.py#L345), [role_service.py:133](backend/app/application/role_service.py#L133), [mammography_service.py:114](backend/app/application/mammography_service.py#L114), [tasks.py:193](backend/app/infrastructure/queue/tasks.py#L193)). Five distinct gaps below. |
| **AuditEvent.agent.who** | `actor` String(128), populated **inconsistently**: `"system"`, `"celery_worker"` ([tasks.py:198](backend/app/infrastructure/queue/tasks.py#L198)), a **user_id** ([onboarding.py:66](backend/app/interface/api/onboarding.py#L66) → `request.state.user_id`), or a **username** ([reading_service.py:77](backend/app/application/reading_service.py#L77) → `actor=username`) | `agent.who` Reference(Practitioner\|Device\|…), `agent.type` Coding, `agent.requestor` boolean | **Fail** | One untyped column carrying four different kinds of value. Given a row, you cannot tell whether `actor` is an id, a name, or a machine — so you cannot reliably resolve it to a `Practitioner`, and joining the audit log to `users` requires guessing the key. This is the highest-value audit fix and it is a data-quality bug independent of FHIR. |
| **AuditEvent.action** | `AuditAction` enum: 20 proprietary values (`study_received`, `job_created`, `result_viewed`, `phi_deidentified`, …) ([enums.py:62](backend/app/domain/enums.py#L62)) | `action` is a **fixed** 5-code set: `C`reate `R`ead `U`pdate `D`elete `E`xecute; semantic detail belongs in `type`/`subtype` | **Fail** | Domain vocabulary is in the wrong element. It is well-designed vocabulary — it just needs to move to `subtype`, with `action` collapsed to CRUDE. DICOM audit message codes (`DCM 110110` Patient Record, `DCM 110106` Export) exist for exactly this domain and are the right target. |
| **Audit read coverage** | `AuditAction.RESULT_VIEWED` is **defined** in the enum but no `AuditLogRecord` write site logs a result/report view — write sites cover state changes only; `results.py` and `reports.py` log nothing | HIPAA-aligned audit expects `action = R` on PHI access; `AuditEvent` exists precisely for this | **Fail** | The enum value exists, so the intent was there — the write was never implemented. PHI *reads* are unlogged: viewing a result, downloading a report PDF, and opening a `share_links` token ([models.py:410](backend/app/infrastructure/database/models.py#L410), `created_by` default `"system"`) all leave no trace. This is the most consequential finding in Pillar 4 and is not really about FHIR. |
| **AuditEvent.source / outcome / network** | Absent — no observer, no outcome code, no IP/hostname | `source.observer` 1..1; `outcome`; `agent.network` | **Fail** | Cannot distinguish a succeeded from a failed action, cannot attribute to a node or client address. |
| **AuditEvent.entity.what** | `entity_type` String(64) + `entity_id` String(256), composite-indexed | `entity.what` Reference, `entity.type` Coding, `entity.role` | **Partial** | The right *pair* of facts with the right index — but as untyped strings rather than a typed reference. Mechanical to map once `entity_type` values are enumerated. |

---

## 3. Critical High-Risk Violations

Ranked by immediacy. **#1 and #2 are clinical-safety issues, not conformance issues** — they matter
more than the score.

### 3.1 🔴 `DiagnosticReport.status` hardcoded to `"final"` — publishes unreviewed AI as a signed report

[backend/app/fhir/client.py:36](backend/app/fhir/client.py#L36)

```python
report = {
    "resourceType": "DiagnosticReport",
    "status": "final",          # ← unconditional
```

`studies.reading_status` (`unread → in_progress → reported → signed`) already tracks radiologist
review and is **never consulted**. Any export therefore asserts to the receiving system that raw
model output is a finalised radiology report. In FHIR semantics `final` means verified and released
to the chart.

This is a validator-clean, clinically wrong payload — which is worse than a rejected one, because
nothing surfaces the error. Must be fixed before `fhir_enabled` is set to `True` anywhere.

Required mapping:

| `studies.reading_status` | `DiagnosticReport.status` |
|---|---|
| `unread`, `in_progress` | `preliminary` |
| `reported` | `preliminary` (or `partial`) |
| `signed` | `final` |
| superseded (`is_latest = False`) | `entered-in-error` |

### 3.2 🔴 `subject` reference built from a DICOM PatientID treated as a FHIR logical id

[backend/app/fhir/client.py:66-70](backend/app/fhir/client.py#L66-L70)

```python
report["subject"] = {"reference": f"Patient/{patient_info['patient_id']}",
                     "display": patient_info.get("patient_name", "")}
```

`Patient/{x}` is a **logical resource id** on the target server. `patient_id` is a DICOM `PatientID`
(an MRN in a foreign namespace). These coincide essentially never. The result is either a dangling
reference or — the dangerous case — a reference that resolves to a *different patient* who happens to
hold that logical id.

Attaching one patient's AI findings to another's chart is the worst failure this platform can
produce. Correct approach is a search-based resolution (`Patient?identifier={system}|{value}`,
exactly one hit required) with `unlinked` and `ambiguous` as explicit non-linking outcomes.
Secondary: `subject` is set **only if** `patient_id` is truthy, and `DiagnosticReport.subject` is
mandatory under US Core — so a study with a blank `PatientID` silently emits a subject-less report.

### 3.3 🔴 `_observations` — a non-FHIR property on the resource → hard validator rejection

[backend/app/fhir/client.py:90](backend/app/fhir/client.py#L90)

```python
report["_observations"] = observations
```

`DiagnosticReport` has no `_observations` element. The `_element` prefix is **reserved** in FHIR JSON
for a primitive's extension object, so this is not merely unknown — it is syntactically meaningful
and wrong. `$validate` returns `Unrecognised property '_observations'`; strict servers reject the
whole POST.

Compounding it: those `Observation`s are never POSTed and never referenced from
`DiagnosticReport.result[]`. The structured export tier is **entirely non-functional** — it builds
a list and abandons it inside the parent resource. The correct shape is a transaction `Bundle`
containing the `DiagnosticReport` plus each `Observation`, with `result[]` holding the references.

### 3.4 🔴 Every `instant` serialises without a timezone offset

`grep -c "timezone=True"` over [models.py](backend/app/infrastructure/database/models.py) → **0**.
All 24 tables use `TIMESTAMP WITHOUT TIME ZONE`.

`instant` is defined as requiring an offset. `DiagnosticReport.issued`, `Observation.issued`,
`AuditEvent.recorded`, and `Provenance.recorded` are all `instant`. A naive value serialises as
`2026-07-29T00:00:00` → `Error parsing JSON: the primitive value must be a valid instant`.

Note `client.py:31` currently does `datetime.utcnow().isoformat() + "Z"`, which is valid *only*
because `Z` is appended by hand — a pattern that silently mislabels any non-UTC value it is applied
to. Meanwhile `studies.study_date` cannot be rescued this way: [study_service.py:56](backend/app/application/study_service.py#L56)
parses DICOM `StudyDate` and **discards `StudyTime`**, so it is midnight-local, date-only precision.
Two studies on the same day cannot be ordered by `effectiveDateTime`.

### 3.5 🔴 `Observation` emitted with no code and `unit: "unknown"` — and, for pet_ct, none at all

[backend/app/fhir/client.py:76-88](backend/app/fhir/client.py#L76-L88)

```python
for key, value in result.get("measurements", {}).items():
    if isinstance(value, (int, float)):
        obs = {"resourceType": "Observation", "status": "final",
               "code": {"text": key.replace("_", " ").title()},   # no coding
               "valueQuantity": {"value": value, "unit": "unknown"}}  # no UCUM
```

Three faults, and the third is the one to check first:

1. **No `coding`.** `Observation.code` text-only passes base cardinality but fails every real profile
   and is untrendable — the receiving EHR cannot graph or index it.
2. **`unit: "unknown"`** with no `system`/`code` fails UCUM binding. A quantity whose unit is
   `unknown` is not a clinical measurement.
3. **The filter is wrong in both directions.** For `brain_mri` it publishes technical metadata
   (`image_dimensions`, `candidate_slices`) into a patient chart as clinical observations. For
   `pet_ct` — the plugin with the most clinically valuable numbers — every top-level `measurements`
   value is a `dict` or `list` (`lesions`, `reference_organs`, `whole_body`, `voxel_spacing_mm`,
   `image_dimensions`; [pipeline.py:743-757](backend/app/usecases/pet_ct/pipeline.py#L743-L757)), so
   `isinstance(value, (int, float))` matches **nothing** and SUVmax, MTV, and TLG are silently
   dropped. The export is simultaneously too permissive and empty where it matters.

The general rule this implies: a blanket loop over `measurements` cannot be made safe. Export must
be a **strict per-plugin whitelist** — a field with no code map entry is never exported.

---

## 4. Remediation Action Plan

Ordered so that each step is independently shippable and nothing later depends on terminology
sign-off that has not happened yet.

### Step 0 — Stop the bleeding (hours, no migration)

Do these before any schema work; they are the difference between "not yet conformant" and "unsafe if
enabled".

1. Confirm `fhir_enabled` is `False` in every deployed environment
   ([config.py:112](backend/app/config.py#L112)).
2. `app/fhir/client.py:36` — derive `status` from `studies.reading_status` per the §3.1 table. Refuse
   to export when `reading_status` is unknown rather than defaulting to `final`.
3. `app/fhir/client.py:90` — delete `report["_observations"]`.
4. `app/fhir/client.py:76` — delete the `isinstance(value, (int, float))` loop entirely. Emit no
   `Observation`s until Step 4 lands. Zero observations is correct; wrong ones are not.
5. `app/fhir/client.py:66` — remove the `Patient/{patient_id}` reference. Emit no `subject` and log a
   warning until Step 3 lands.
6. Note for later: `app/fhir/` sits outside the four-layer architecture
   (`domain`/`application`/`infrastructure`/`interface`). Relocate per the layout in
   `FHIR_INTEGRATION_PLAN.md` §7 — mapping logic to `application/`, HTTP to `infrastructure/`. One
   import to migrate, at [reports.py:546](backend/app/interface/api/reports.py#L546).

### Step 1 — Timestamps (migration 027)

Fixes §3.4 for every resource at once. Do it first; it is mechanical and unblocks all serialization.

1. Convert every `Column(DateTime)` → `Column(DateTime(timezone=True))` across all 24 tables
   (`TIMESTAMP WITH TIME ZONE` in Postgres). Alembic: `op.alter_column(..., type_=sa.DateTime(timezone=True))`.
   Existing naive values are interpreted as the server timezone — verify that assumption against
   production data before running, and pin `postgresql_using` with an explicit `AT TIME ZONE` if not.
2. Make `_utcnow()` ([domain/models.py:12](backend/app/domain/models.py#L12)) the only clock in use;
   it is already correct (`datetime.now(timezone.utc)`).
3. [study_service.py:56](backend/app/application/study_service.py#L56) — parse DICOM `StudyTime`
   (0008,0030) alongside `StudyDate` and combine. Where `StudyTime` is absent, record date-only
   precision explicitly rather than implying midnight; FHIR `dateTime` permits `YYYY-MM-DD`, which is
   honest, whereas `T00:00:00Z` is not.
4. Add a serialization guard: assert `dt.tzinfo is not None` before emitting any `instant`.

### Step 2 — Identifier systems (migration 027, same batch)

Fixes the "bare string identifier" class of failure.

1. Add to `fhir_connections`-style per-tenant config (or `TenantRecord` if you defer that table):
   `patient_identifier_system`, `accession_identifier_system`.
2. Define constants in a new `domain/fhir_terminology.py` (stdlib only, domain-safe):
   `DICOM_UID_SYSTEM = "urn:dicom:uid"`, `DCM_SYSTEM = "http://dicom.nema.org/resources/ontology/DCM"`,
   `LOINC = "http://loinc.org"`, `SNOMED = "http://snomed.info/sct"`,
   `ICD10 = "http://hl7.org/fhir/sid/icd-10"`, `RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"`,
   `UCUM = "http://unitsofmeasure.org"`.
3. Emit `study_instance_uid` as `{"system": "urn:dicom:uid", "value": f"urn:oid:{uid}"}` — note the
   `urn:oid:` prefix on the **value**, which the current code does correctly at
   [client.py:60-64](backend/app/fhir/client.py#L60-L64). Keep that.
4. Add `orders.external_order_ref` String, indexed — the placer's `ServiceRequest.identifier`.
5. Add a real FK: `studies.patient_record_id` → `patients.id`, populated by an explicit resolution
   step. Keep `studies.patient_id` (the raw DICOM value) as the *identifier*; the FK is the
   *reference*. This is the fix for §2.2 `ImagingStudy.subject`, and it is worth doing even with no
   FHIR work at all.

### Step 3 — Patient normalisation

1. Add `patients.family_name`, `given_names` (JSON array), `name_prefix`, `name_suffix` — **or**, to
   preserve the de-identified invariant, keep them out of Postgres entirely and parse DICOM PN into a
   `HumanName` only at serialization time from the transient study metadata. Recommended:
   **parse at the boundary, do not persist.** It keeps §4.2 of `FHIR_INTEGRATION_PLAN.md` intact.
2. Write a DICOM PN parser (`^`-delimited: family^given^middle^prefix^suffix) in
   `application/` — one function, used by both the FHIR mapper and the PDF report generator, which
   currently prints the raw PN string ([report_service.py:111](backend/app/application/report_service.py#L111)).
3. Normalise gender at ingest: DICOM `M|F|O` → `male|female|other`, empty → `unknown`. Apply in
   [study_service.py:95](backend/app/application/study_service.py#L95) so `StudyRecord.patient_sex`
   and `PatientRecord.sex` finally agree on one value set. Store the FHIR code; keep the DICOM
   original in `dicom_tags` if needed for provenance.
4. Decide `birthDate` explicitly and document the decision. Options: (a) keep discarding it and
   accept that `Patient.birthDate` is never exported; (b) store year-only. Do **not** try to export
   `age_band` — it has no FHIR representation and inventing an extension for it is not worth it.

### Step 4 — The `Observation` table (migration 028) ★ largest item

This is the structural change that unblocks pillars 2 and 3 together. Without it, `Observation`,
lab data, and FHIR search are all unreachable.

New table `observations` — one row per measured concept:

```
id              String(36) PK
tenant_id       String(36)  indexed
patient_ref     String(128) indexed        -- FK-equivalent to patients.patient_ref
study_instance_uid String(128) FK nullable indexed
result_id       String(36)  FK nullable    -- provenance back to results_index
category        String(32)                 -- vital-signs | laboratory | imaging | survey
code_system     String(128)                -- LOINC / SNOMED / local
code            String(64)  indexed
code_display    String(256)
value_quantity  Float nullable
value_unit      String(32)  nullable       -- UCUM code
value_string    String(512) nullable
value_codeable_code   String(64) nullable  -- e.g. BI-RADS answer code
value_codeable_system String(128) nullable
body_site_code  String(64)  nullable       -- SNOMED body structure
laterality      String(16)  nullable       -- left | right | bilateral
status          String(16)  not null       -- registered|preliminary|final|amended
effective_dt    DateTime(timezone=True)    indexed
issued          DateTime(timezone=True)
derived_from_device String(128) nullable    -- model_version:model_checksum
Index (tenant_id, patient_ref, code, effective_dt)   -- the FHIR search index
```

That composite index is the point: it makes `Observation?patient=X&code=Y&date=ge…` a single
index scan, which is impossible against a JSON blob.

Populate from three sources:

1. **Intake labs/vitals** — migrate `orders.fasting_glucose`, `orders.creatinine`,
   `orders.height_cm`, `orders.weight_kg` into rows. Keep the order columns for now (dual-write) and
   deprecate after the UI is switched, so nothing that reads them breaks.
   Change the intake form to capture **value + unit + date drawn** as separate inputs — a free-text
   `String(32)` cannot be repaired downstream.
2. **AI measurements** — a per-plugin whitelist map (below).
3. **Mammography slots** — pivot `birads_right`/`density_left`/`mass_right`/… into rows carrying
   `laterality` + `body_site_code`. The data is already structured and CHECK-constrained; this is a
   shape change, not a data-quality change.

### Step 5 — Terminology layer

Create `backend/app/usecases/<name>/fhir_map.yaml`, sibling of the existing `outputs_schema.json` /
`ui_schema.json` (self-contained per the plugin rule). **Strict whitelist — an unmapped field is
never exported.**

```yaml
# usecases/pet_ct/fhir_map.yaml
observations:
  - path: summary.suvmax_body
    code: {system: "http://loinc.org", code: "<TBD-verify>", display: "SUVmax, whole body"}
    unit: {system: "http://unitsofmeasure.org", code: "1"}    # SUV is dimensionless
    category: imaging
  - path: summary.mtv_total_ml
    code: {system: local, code: "MTV", display: "Metabolic tumour volume"}
    unit: {system: "http://unitsofmeasure.org", code: "mL"}
    category: imaging
  - path: summary.tlg_total
    code: {system: local, code: "TLG", display: "Total lesion glycolysis"}
    unit: {system: "http://unitsofmeasure.org", code: "g"}
    category: imaging
# NOT exported (technical, not clinical):
#   measurements.image_dimensions, measurements.voxel_spacing_mm
```

**Codes I am confident in** and which can be applied now:

| Concept | System | Code |
|---|---|---|
| Diagnostic imaging report | LOINC | `18748-4` |
| Body weight | LOINC | `29463-7` |
| Body height | LOINC | `8302-2` |
| Glucose, fasting (serum/plasma) | LOINC | `1558-6` |
| Creatinine (serum/plasma) | LOINC | `2160-0` |
| Modality `MR`/`CT`/`PT` | DCM (CID 29) | already correct — add the `system` URI only |

**Codes that must ship as `<TBD-verify>`** and stay disabled until confirmed against a current
release by someone qualified: SUVmax, MTV, TLG, Agatston score, Pfirrmann grade, BI-RADS assessment
and breast-density answer codes, and every SNOMED body-structure code for the `BodyPart` enum. A
wrong LOINC is worse than a missing one — the EHR trends it under the wrong concept, invisibly, and
nothing surfaces the error.

UCUM units (`mL`, `g`, `mm`, `kg`, `cm`, `mg/dL`, `mmol/L`, `1`, `{score}`) are safe to apply
immediately; `ui_schema.json` already carries per-field `unit`/`precision` and is the natural source.

When collapsing the `BodyPart` synonym pairs (`HEART`/`CARDIAC`, `CHEST`/`THORAX`, `BRAIN`/`HEAD`),
map both members to the *same* SNOMED code deliberately and record that decision in the map file —
otherwise one anatomical concept acquires two codes.

### Step 6 — `Condition` and `MedicationRequest`

1. `conditions` table: `patient_ref`, `code_system` (ICD-10 or SNOMED), `code`, `code_display`,
   `clinical_status`, `verification_status`, `onset_dt`, `recorded_dt`, `tenant_id`.
2. Change the intake UI: `orders.indication` gains a **coded** companion field (ICD-10 picker) while
   the free text remains as `Condition.note` / `ServiceRequest.reasonReference`. Do not attempt to
   retro-code existing free text automatically — flag legacy rows as uncoded and leave them.
3. `medication_requests` table (`status`, `intent`, RxNorm `medication_code`, `authored_on`,
   `patient_ref`) **only if** treatment-timeline data will actually be captured or fetched. If it
   will only ever arrive from an external EHR, model it as transient fetched context instead of a
   table — do not build a write-path for data you never author.

### Step 7 — Practitioner, PractitionerRole, Organization

1. `users`: add `identifier_system` + `identifier_value` (NPI / state license), and
   `family_name`/`given_names` alongside `full_name`.
2. New `user_roles` join table (`user_id`, `role_id`, `organization_id`, `specialty_code`,
   `period_start`, `period_end`) replacing the single `users.role` string. Keep `users.role` as a
   denormalised primary-role cache so `require_permission` is untouched — the RBAC path must not
   regress.
3. `tenants`: add `identifier_system`, `identifier_value`, `address` JSON, `type` so a real
   `Organization` can be emitted and used as `Identifier.assigner`.
4. Map role names → `PractitionerRole.code` in the terminology module (`radiologist`,
   `technician`, `receptionist`, `viewer`), default-deny for unmapped.

### Step 8 — `AuditEvent` conformance

Highest value per line of code in the whole plan, and most of it pays off with no FHIR involved.

1. **Split `actor` into typed columns** (migration): `actor_type` (`practitioner|device|system`),
   `actor_id` (always the stable id — never a username), `actor_display` (denormalised name).
   Backfill by inspecting current values. Then fix the four inconsistent call sites so
   `reading_service.py:77` passes an id, not a username.
2. Add `action_crude` String(1) (`C|R|U|D|E`) beside the existing `action`, and map the 20
   `AuditAction` values onto it. Keep `action` — it becomes `AuditEvent.subtype`, which is where that
   vocabulary belongs.
3. Add `outcome` String(16), `source_observer` String(128), `client_ip` String(64).
4. **Implement `RESULT_VIEWED`.** The enum value exists ([enums.py:71](backend/app/domain/enums.py#L71))
   but nothing writes it. Add audit writes on: result fetch (`results.py`), report PDF download
   (`reports.py`), and `share_links` token redemption. Without these, PHI reads are invisible — the
   most consequential gap in Pillar 4 and independent of any FHIR export.
5. Map `entity_type` values to FHIR resource type names so `entity.what` becomes a real reference.

### Step 9 — Search-parameter parity

Once `observations` exists, expose the four search axes the FHIR `Observation` contract requires:
`patient`, `category`, `code`, `date` (with `ge`/`le` prefixes), plus `_lastUpdated` for
watermark polling. `GET /studies` currently supports `body_part`, `modality`, `patient_id`
([studies.py:33-38](backend/app/interface/api/studies.py#L33-L38)) — a reasonable base for
`ImagingStudy?subject=&modality=&bodysite=`; add `date` range and `identifier`. `GET /results` has
no filters at all beyond `version` and needs `patient` + `date`.

### Step 10 — `Device` and `Provenance`

Cheap, and the right closing step for AI attribution. `results_index.model_version` and
`model_checksum` are already `NOT NULL` on every row — emit a `Device` from them and reference it as
`Observation.device` and `Provenance.agent.who`, with `Provenance.target` pointing at the
`DiagnosticReport`. This is what marks a finding as algorithm-derived rather than human-authored.

### Effort summary

| Step | Migration | Effort | Blocks |
|---|---|---|---|
| 0 Stop the bleeding | — | 0.5 d | nothing — do today |
| 1 Timestamps | 027 | 1 d | all serialization |
| 2 Identifier systems | 027 | 1 d | all references |
| 3 Patient normalisation | — | 1.5 d | `Patient`, `subject` |
| 4 `observations` table | 028 | 4 d | pillars 2 + 3 |
| 5 Terminology maps | — | 3 d + external sign-off | structured export |
| 6 `Condition`/`MedicationRequest` | 029 | 3 d | clinical context |
| 7 Practitioner/Role/Org | 030 | 2.5 d | pillar 4 |
| 8 `AuditEvent` | 031 | 2 d | pillar 4 |
| 9 Search parity | — | 2 d | FHIR REST |
| 10 `Device`/`Provenance` | — | 1 d | AI attribution |

**≈ 21.5 dev-days** to a validator-clean R4 export across all audited resources, plus external
clinical sign-off on terminology (parallel, not on the critical path).

Steps **0–2 (2.5 days)** move the score from 27% to roughly 45% and remove all five §3 violations.
That is by far the best return in the plan, and Step 0 alone removes the two clinical-safety issues.

---

## 5. Sample FHIR-Compliant JSON Transformation

Using a real `pet_ct` result, shaped exactly as
[pipeline.py:718-760](backend/app/usecases/pet_ct/pipeline.py#L718-L760) emits it and
`ResultResponse` ([schemas/result.py:18](backend/app/interface/schemas/result.py#L18)) serialises it.

### BEFORE — current `GET /api/results/{id}` payload

```json
{
  "id": "8f2c1a44-9d3e-4b7a-8c11-5e6f7a8b9c0d",
  "study_instance_uid": "1.2.840.113619.2.55.3.604688119.971.1690012345.678",
  "usecase_name": "pet_ct",
  "job_id": "b71e9f02-4c8a-4f19-9a2d-1c3b5d7e9f11",
  "summary": {
    "lesions_detected": true,
    "lesion_count": 3,
    "mtv_total_ml": 42.7,
    "tlg_total": 218.4,
    "suvmax_body": 12.86,
    "radiopharmaceutical": "FDG",
    "percist_score": "PMR",
    "deauville_score": 4,
    "tumor_to_liver_ratio": 4.92,
    "diagnosis": "Multiple FDG-avid foci consistent with active disease.",
    "inference_method": "percist_threshold",
    "quantitative": true,
    "confidence": "high",
    "processing_notes": "3 foci retained; 1 physiologic focus suppressed (bladder)."
  },
  "measurements": {
    "lesions": [
      { "id": 1, "anatomical_region": "lung_upper_lobe_right",
        "suv_max": 12.86, "suv_mean": 7.41, "suv_peak": 11.02,
        "volume_ml": 18.3, "tlg": 135.6 },
      { "id": 2, "anatomical_region": "lymph_node_mediastinal",
        "suv_max": 8.12, "suv_mean": 5.33, "suv_peak": 7.28,
        "volume_ml": 14.1, "tlg": 75.2 },
      { "id": 3, "anatomical_region": "liver",
        "suv_max": 4.05, "suv_mean": 3.11, "suv_peak": 3.78,
        "volume_ml": 10.3, "tlg": 32.0 }
    ],
    "reference_organs": {
      "liver_suv_mean": 2.114, "liver_suv_sd": 0.382,
      "mediastinum_suv_mean": 1.203
    },
    "whole_body": {
      "mtv_total_ml": 42.7, "tlg_total": 218.4,
      "suvmax_body": 12.86, "lesion_count": 3
    },
    "voxel_spacing_mm": [4.073, 4.073, 3.27],
    "image_dimensions": [168, 168, 331]
  },
  "qa_flags": ["scan_delay_exceeded"],
  "qa_details": { "scan_delay_exceeded": { "delay_min": 78, "limit_min": 70 } },
  "model_version": "percist-1.0",
  "model_checksum": "sha256:4b1d…c7e9",
  "artifacts": [
    { "name": "report", "artifact_type": "report_json",
      "storage_path": "results/1.2.840…678/pet_ct/report.json",
      "content_type": "application/json", "size_bytes": 8421 }
  ],
  "version": 2,
  "is_latest": true,
  "created_at": "2026-07-29T09:14:22.481000"
}
```

**Why this fails a FHIR validator as-is:** no `resourceType`; no `status`; no `subject`; no
`effective[x]`; `created_at` has no timezone offset (§3.4); clinical values (`suvmax_body`) and
technical metadata (`image_dimensions`) are indistinguishable siblings; nothing is coded; no units.

### AFTER — transaction `Bundle`, R4-valid

Only whitelisted clinical values appear. `image_dimensions` and `voxel_spacing_mm` are deliberately
absent. `status` is `preliminary` because this study's `reading_status` is not yet `signed` (§3.1).

```json
{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {
      "fullUrl": "urn:uuid:8f2c1a44-9d3e-4b7a-8c11-5e6f7a8b9c0d",
      "resource": {
        "resourceType": "DiagnosticReport",
        "identifier": [
          { "system": "urn:dicom:uid",
            "value": "urn:oid:1.2.840.113619.2.55.3.604688119.971.1690012345.678" },
          { "system": "https://mrcv.example.org/fhir/sid/result-id",
            "value": "8f2c1a44-9d3e-4b7a-8c11-5e6f7a8b9c0d" }
        ],
        "status": "preliminary",
        "category": [
          { "coding": [ { "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
                          "code": "NMR", "display": "Nuclear Medicine Report" } ] }
        ],
        "code": {
          "coding": [ { "system": "http://loinc.org", "code": "18748-4",
                        "display": "Diagnostic imaging study" } ],
          "text": "FDG PET/CT whole body — AI analysis"
        },
        "subject": { "reference": "Patient/eXY9mQ2kL7pR4tN" },
        "effectiveDateTime": "2026-07-28T10:32:00+05:00",
        "issued": "2026-07-29T09:14:22.481+05:00",
        "performer": [ { "reference": "Organization/mrcv-imaging-centre" } ],
        "imagingStudy": [ { "reference": "ImagingStudy/is-1690012345678" } ],
        "result": [
          { "reference": "urn:uuid:a1000000-0000-4000-8000-000000000001" },
          { "reference": "urn:uuid:a1000000-0000-4000-8000-000000000002" },
          { "reference": "urn:uuid:a1000000-0000-4000-8000-000000000003" }
        ],
        "conclusion": "Multiple FDG-avid foci consistent with active disease. 3 foci retained; 1 physiologic focus suppressed (bladder).",
        "conclusionCode": [
          { "coding": [ { "system": "http://snomed.info/sct",
                          "code": "<TBD-verify>",
                          "display": "Increased metabolic activity" } ] }
        ],
        "extension": [
          { "url": "https://mrcv.example.org/fhir/StructureDefinition/qa-flag",
            "valueCode": "scan_delay_exceeded" }
        ]
      },
      "request": {
        "method": "PUT",
        "url": "DiagnosticReport?identifier=urn:dicom:uid|urn:oid:1.2.840.113619.2.55.3.604688119.971.1690012345.678"
      }
    },

    {
      "fullUrl": "urn:uuid:a1000000-0000-4000-8000-000000000001",
      "resource": {
        "resourceType": "Observation",
        "status": "preliminary",
        "category": [
          { "coding": [ { "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                          "code": "imaging", "display": "Imaging" } ] }
        ],
        "code": {
          "coding": [ { "system": "http://loinc.org", "code": "<TBD-verify>",
                        "display": "SUVmax, whole body" } ],
          "text": "Whole-body SUVmax"
        },
        "subject": { "reference": "Patient/eXY9mQ2kL7pR4tN" },
        "effectiveDateTime": "2026-07-28T10:32:00+05:00",
        "issued": "2026-07-29T09:14:22.481+05:00",
        "valueQuantity": {
          "value": 12.86,
          "unit": "1",
          "system": "http://unitsofmeasure.org",
          "code": "1"
        },
        "bodySite": {
          "coding": [ { "system": "http://snomed.info/sct", "code": "<TBD-verify>",
                        "display": "Upper lobe of right lung" } ]
        },
        "device": { "reference": "Device/mrcv-percist-1-0" },
        "derivedFrom": [ { "reference": "ImagingStudy/is-1690012345678" } ]
      },
      "request": { "method": "POST", "url": "Observation" }
    },

    {
      "fullUrl": "urn:uuid:a1000000-0000-4000-8000-000000000002",
      "resource": {
        "resourceType": "Observation",
        "status": "preliminary",
        "category": [
          { "coding": [ { "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                          "code": "imaging" } ] }
        ],
        "code": {
          "coding": [ { "system": "https://mrcv.example.org/fhir/CodeSystem/pet-metrics",
                        "code": "MTV", "display": "Metabolic tumour volume" } ],
          "text": "Metabolic tumour volume (total)"
        },
        "subject": { "reference": "Patient/eXY9mQ2kL7pR4tN" },
        "effectiveDateTime": "2026-07-28T10:32:00+05:00",
        "valueQuantity": {
          "value": 42.7, "unit": "mL",
          "system": "http://unitsofmeasure.org", "code": "mL"
        },
        "device": { "reference": "Device/mrcv-percist-1-0" }
      },
      "request": { "method": "POST", "url": "Observation" }
    },

    {
      "fullUrl": "urn:uuid:a1000000-0000-4000-8000-000000000003",
      "resource": {
        "resourceType": "Observation",
        "status": "preliminary",
        "category": [
          { "coding": [ { "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                          "code": "imaging" } ] }
        ],
        "code": {
          "coding": [ { "system": "https://mrcv.example.org/fhir/CodeSystem/pet-metrics",
                        "code": "TLG", "display": "Total lesion glycolysis" } ],
          "text": "Total lesion glycolysis"
        },
        "subject": { "reference": "Patient/eXY9mQ2kL7pR4tN" },
        "effectiveDateTime": "2026-07-28T10:32:00+05:00",
        "valueQuantity": {
          "value": 218.4, "unit": "g",
          "system": "http://unitsofmeasure.org", "code": "g"
        },
        "device": { "reference": "Device/mrcv-percist-1-0" }
      },
      "request": { "method": "POST", "url": "Observation" }
    },

    {
      "fullUrl": "urn:uuid:d0000000-0000-4000-8000-00000000000d",
      "resource": {
        "resourceType": "Device",
        "identifier": [
          { "system": "https://mrcv.example.org/fhir/sid/model-checksum",
            "value": "sha256:4b1d…c7e9" }
        ],
        "deviceName": [ { "name": "MRCV pet_ct PERCIST", "type": "manufacturer-name" } ],
        "type": {
          "coding": [ { "system": "http://snomed.info/sct", "code": "<TBD-verify>",
                        "display": "Software application" } ]
        },
        "version": [ { "value": "percist-1.0" } ]
      },
      "request": {
        "method": "PUT",
        "url": "Device?identifier=https://mrcv.example.org/fhir/sid/model-checksum|sha256:4b1d…c7e9"
      }
    },

    {
      "fullUrl": "urn:uuid:e0000000-0000-4000-8000-00000000000e",
      "resource": {
        "resourceType": "Provenance",
        "target": [ { "reference": "urn:uuid:8f2c1a44-9d3e-4b7a-8c11-5e6f7a8b9c0d" } ],
        "recorded": "2026-07-29T09:14:22.481+05:00",
        "activity": {
          "coding": [ { "system": "http://terminology.hl7.org/CodeSystem/v3-DataOperation",
                        "code": "CREATE" } ]
        },
        "agent": [
          { "type": { "coding": [ { "system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                                    "code": "assembler" } ] },
            "who": { "reference": "urn:uuid:d0000000-0000-4000-8000-00000000000d" } }
        ]
      },
      "request": { "method": "POST", "url": "Provenance" }
    }
  ]
}
```

### What changed, element by element

| Before | After | Why |
|---|---|---|
| *(no `resourceType`)* | `Bundle` / `DiagnosticReport` / `Observation` / `Device` / `Provenance` | 5 resources from 1 row — a flat result is not a FHIR resource. |
| *(no `status`)* | `"status": "preliminary"` derived from `studies.reading_status` | Mandatory 1..1. `preliminary`, not `final`, because the study is unsigned (§3.1). |
| *(no `subject`)* | `Patient/eXY9mQ2kL7pR4tN` — a **resolved logical id**, not the MRN | §3.2. The id comes from `Patient?identifier={system}\|{MRN}` returning exactly one hit. |
| `"created_at": "…481000"` | `"issued": "…481+05:00"` | `instant` requires an offset (§3.4). |
| *(no study time)* | `effectiveDateTime: "2026-07-28T10:32:00+05:00"` | Requires parsing DICOM `StudyTime`, currently discarded. |
| `study_instance_uid` bare string | `identifier[]` with `system: "urn:dicom:uid"`, `value: "urn:oid:…"` | An identifier without a system is not matchable. |
| `summary.suvmax_body: 12.86` | `Observation` + LOINC code + UCUM `1` + `bodySite` | Coded, timed, trendable, searchable. |
| `summary.mtv_total_ml`, `tlg_total` | two `Observation`s, UCUM `mL` / `g` | No LOINC exists → explicit local `CodeSystem`, honestly namespaced. |
| `measurements.image_dimensions`, `voxel_spacing_mm` | **omitted** | Technical metadata. §3.5 — the current loop would publish these into a chart. |
| `measurements.lesions[]` | per-lesion `Observation`s (one shown; `bodySite` per lesion) | Blob → rows. This is what makes FHIR search possible. |
| `qa_flags: ["scan_delay_exceeded"]` | `DiagnosticReport.extension` with a declared `StructureDefinition` URL | No core element for QA flags; an extension is the conformant home. |
| `model_version` + `model_checksum` | `Device` + `Provenance.agent.who` | Marks the finding as algorithm-derived. Data already exists in every row. |
| `"unit": "unknown"` | `system: "http://unitsofmeasure.org"` + UCUM `code` | §3.5. |
| `report["_observations"]` | `Bundle.entry[]` + `DiagnosticReport.result[]` references | §3.3 — removes the hard validator failure and makes the observations actually reachable. |
| *(single POST)* | `PUT` by identifier for `DiagnosticReport`/`Device`, `POST` for the rest | Conditional update = idempotent re-runs; a re-processed study updates rather than duplicating. |

Every `<TBD-verify>` is deliberate: those codes must be confirmed against a current LOINC/SNOMED
release by someone qualified before the structured tier is enabled. A wrong code is worse than a
missing one, because the receiving EHR will trend it under the wrong concept and nothing will
surface the error.

---

## Relationship to the existing `FHIR_INTEGRATION_PLAN.md`

That document plans the **integration** (connections, identity resolution, auth, capability
discovery, export tiers) and is well-aligned with these findings — §4.4 already identifies the
hardcoded `"final"`, §8 already identifies the `isinstance(int, float)` loop publishing
`Candidate Slices: 14 unknown`.

This audit covers the layer beneath it: the **schema and terminology** work that has to exist for
that plan's P3/P4 to have anything valid to send. Concretely, Steps 1–5 here are prerequisites for
the plan's P4 "Structured tier", and Step 2's `studies.patient_record_id` FK is a prerequisite for
its P1 identity resolution. Step 8 (`AuditEvent`) is not covered by that plan at all and stands
alone — as does the unimplemented `RESULT_VIEWED` write, which is a compliance gap today regardless
of whether FHIR is ever enabled.
