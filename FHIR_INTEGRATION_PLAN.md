# HL7 FHIR Integration — Design & Implementation Plan

**Scope: FHIR only. No HL7 v2.** `HL7_INTEGRATION_PLAN.md` (v2/MLLP) is parked — nothing in this
plan depends on it, and this plan does not require the v2 dependency `hl7apy`, the MLLP listener
process, migration `027`'s `hl7_messages` table, or the `hl7_*` config block. Those can be removed
or left dormant behind their (default-off) flag.

**Deployment premise: the platform runs standalone.** It has its own UI, its own login, its own
Postgres/MinIO/Orthanc. It is *not* an EHR module. Therefore the platform is a pure **FHIR client**:
every FHIR interaction is an outbound HTTPS call to the customer's FHIR endpoint.

---

## 1. Recommendation up front

Build a **single per-tenant FHIR connection** with three capabilities, in this order:

1. **Identity resolution** (map a PACS study to the right EHR patient) — foundation, and the highest-risk
   component. Nothing else is safe until this is right.
2. **Read / clinical enrichment** — fetch demographics, weight/height, labs, conditions, medications,
   and *external prior reports*. Needs read scopes only, needs no cooperation beyond credentials,
   and it measurably improves the correctness of what the AI produces.
3. **Write / report delivery** — publish the report back, in **three selectable fidelity tiers**
   (`DocumentReference` PDF → `DiagnosticReport` → `+ coded Observations`), because write permission
   is the thing most likely to be restricted and you want the integration to degrade rather than fail.

Then, separately, **SMART-based auth** — which is two different things that must not be conflated
(§5): machine-to-machine access to the FHIR server, and clinician SSO into your app.

Read before write. It inverts the intuitive order, but read scopes get approved faster, deliver value
on day one, and de-risk the identity layer before you start writing into a patient's legal record.

---

## 2. What "standalone + FHIR-only" actually implies

These seven consequences are not obvious and each one changes the design:

1. **You are a client, never a server.** No inbound TCP port, no listener process, no firewall
   ingress rule, no new container. Compare with v2, which needed an MLLP port and a standalone
   process. This is the strongest practical argument for the FHIR-only decision — it is a
   *deployment* win as much as a data win.
2. **Therefore: polling, not push.** FHIR `Subscription` (rest-hook) requires the hospital's server
   to call *you*, which requires you to be reachable — the opposite of standalone. Design the
   trigger model as **poll-first** (Celery Beat, `_lastUpdated` watermark) with subscriptions as an
   opt-in extra for sites that will route to you.
3. **Images still do not come over FHIR.** Studies keep arriving by DICOM into Orthanc.
   `ImagingStudy` is metadata only. FHIR sits *alongside* the existing PACS path, adding context and
   delivery; it replaces nothing in the acquisition chain.
4. **The join key is MRN + Accession Number, and both already exist in your schema.**
   `studies.patient_id` (DICOM PatientID) and `studies.accession_number` are both present and indexed
   ([models.py:31,39](backend/app/infrastructure/database/models.py#L31)). These are your only links
   to the EHR. §4.1 is about doing that join safely.
5. **Multi-site means connection config must live in the database, not `.env`.** One standalone
   product instance serving several hospitals (`multi_tenant_enabled`) needs per-tenant base URL,
   client id, key, scopes, and identifier system. Env-var-only FHIR config silently locks you to
   single-tenant. Build `fhir_connections` from the start (§7).
6. **Every site will support a different subset of FHIR.** Some expose `Observation` but not
   `MedicationRequest`; some allow `DocumentReference.write` but not `DiagnosticReport.write`; some
   are R4, some R4B/R5. A standalone product cannot hand-configure this per install — it must
   **discover** capability from `/metadata` and degrade features automatically (§6).
7. **Fetched data becomes an input to patient-affecting computation.** If EHR weight drives SUV, the
   AI output now depends on external, mutable data. That forces input provenance (§4.3) — snapshot
   *what* was used, with resource id + `versionId` + fetch time, into the result.

---

## 3. Feature catalog, ranked by clinical value

### Tier 1 — features that change the *correctness* of AI output

| Feature | FHIR source | Why it matters here |
|---|---|---|
| **Patient weight & height auto-fill** | `Observation` (body weight / height), `Patient` | SUV<sub>bw</sub> is *computed from weight*. Today it comes from the DICOM header (`studies.patient_weight_kg`) or is typed at reception into `orders.weight_kg` ([models.py:243](backend/app/infrastructure/database/models.py#L243)). A stale or mistyped weight makes every SUV in a PET report wrong. |
| **DICOM-vs-EHR weight cross-check → new QA flag** | same | You have *two* independent sources once FHIR is connected. Disagreement beyond a threshold is a detectable data-integrity fault, surfaced through the existing `qa_flags` mechanism instead of going unnoticed. This is a feature you cannot build without FHIR. |
| **Fasting glucose** | `Observation` | Elevated glucose invalidates FDG uptake interpretation. Currently a free-text field typed by hand. |
| **Creatinine / eGFR → contrast-safety pre-check** | `Observation` | For contrast CT: a low-eGFR QA flag *before* the study is reported. Existing field `orders.creatinine` is a hand-typed string. |
| **Pregnancy status** | `Observation` / `Condition` | Hard safety gate for CT/PET. Not captured anywhere today. |

That group is the honest headline: FHIR is not just plumbing here — it removes a class of
quantitative and safety error from PET/CT and contrast studies.

### Tier 2 — features that change AI *interpretation*

| Feature | FHIR source | Why it matters here |
|---|---|---|
| **External prior reports for longitudinal comparison** | `DiagnosticReport` (+`Observation`) history | Your longitudinal service (LLM Phase 4, RANO/PERCIST/Pfirrmann) can only compare against results **the platform itself produced**. In a standalone deployment the baseline scan was very often read elsewhere — so the most valuable comparison is exactly the one currently impossible. This is the single biggest capability unlock in the whole plan. |
| **Known primary / staging** | `Condition` | "Known lung primary" vs "screening" changes how a PET focus is read. Feeds the CDS (Phase 3) prompt as real coded history instead of a typed indication string. |
| **Treatment timeline** | `MedicationRequest`, `Procedure` | Chemo/immunotherapy → pseudoprogression; recent surgery/radiation → expected post-treatment change. Prevents a confident-but-wrong "progression" read. |
| **Referral context** | `ServiceRequest` | `reasonCode` → `orders.indication` (a NOT NULL column), `requester` → `referrer`, `priority` → routine/stat. |

### Tier 3 — workflow and delivery

| Feature | FHIR resource | Notes |
|---|---|---|
| **Report delivered to the chart** | `DocumentReference` (PDF) → `DiagnosticReport` → `Observation` | Three fidelity tiers, §4.4. Delivery status + retry visible in the admin UI. |
| **Coded, trendable numbers** | `Observation` | SUVmax/MTV/TLG, Agatston, BI-RADS, Pfirrmann graphable in the EHR over years. Requires terminology sign-off (§8). |
| **Algorithm attribution** | `Device` | `model_version` + `model_checksum` from `ResultRecord` — traceability for AI-assisted findings. |
| **Critical finding → clinician's inbox** | `Communication` / `Flag` | Pushes `CriticalAlertRecord` out of your UI and into the referrer's EHR worklist; acknowledgement can be read back to close the loop the Beat escalation task currently closes internally. |
| **Auto-filled intake** | `Patient` + `ServiceRequest` | The receptionist confirms instead of transcribing. Consent stays a local action. |
| **Study↔EHR link state in the worklist** | resolver output | A "linked / not in EHR / ambiguous" badge — an operational feature that makes the integration's failures visible rather than silent. |
| **Clinician SSO** | SMART/OIDC | §5. |
| **Deep link back to the chart** | `Patient.id` | Open the EHR patient from the study view. |

### Explicitly *not* worth building for a standalone product
- **A FHIR façade (platform as FHIR server).** It requires the hospital to build against you.
  Realistic value is procurement-checkbox only. Deferred to optional Phase 6, labelled honestly.
- **Bulk `$export`.** Interesting for active-learning cohorts, not for clinical operation.
- **FHIRcast viewer sync.** Only meaningful when embedded in an EHR — which you are not.

---

## 4. The four hard problems

### 4.1 Patient identity resolution — the highest-risk component ★
A study arrives from PACS with a DICOM PatientID and an AccessionNumber. You must find the right
`Patient` in a foreign system. Failure modes are real: DICOM PatientID ≠ MRN; leading zeros stripped
by a modality; site prefixes (`AB-12345` vs `12345`); several identifier systems per hospital;
merged/duplicate MRNs.

**A wrong patient match is the worst failure this platform can produce** — worse than no integration
at all, because it attaches one patient's AI findings to another's chart.

Design: `application/fhir_patient_resolver.py`, ordered strategies, each recorded:
1. **Accession** → `ServiceRequest?identifier={accessionSystem}|{accession}` or
   `ImagingStudy?identifier=urn:dicom:uid|urn:oid:{studyUID}` → follow `subject`. Strongest, because
   the accession was issued by the ordering system itself.
2. **MRN** → `Patient?identifier={fhir_patient_identifier_system}|{normalized(patientID)}`, where
   the normalizer is per-connection configurable (strip/pad zeros, strip prefix).
3. **Nothing else.** No demographic fuzzy matching, ever. No name/DOB search fallback.

Rules: exactly one hit = linked; zero hits = `unlinked`; more than one = `ambiguous` and the study is
**never** auto-linked — both non-linked states are surfaced in the worklist for a human. Persist the
outcome and *how it was reached* in `fhir_patient_links` (§7) so every link is auditable and
re-resolvable. Cache the link; invalidate on `Patient` merge (`link.type = replaced-by`).

### 4.2 The PHI persistence boundary ★
`PatientRecord` is deliberately de-identified: sex + age band, no name, no DOB
([models.py:208](backend/app/infrastructure/database/models.py#L208)). "Fetching patient data" puts
identity and clinical history in your hands and directly pressures that invariant.

**Recommendation: keep the invariant. Fetch transiently, cache in Redis, never persist raw resources
in Postgres.**
- A `FetchedContext` object lives in Redis under the study/patient key with a short TTL
  (`fhir_context_cache_ttl_s`, default 3600), used to render reports and build LLM prompts.
- Postgres receives only (a) **derived, non-identifying values** the computation needs — e.g.
  `weight_kg` as a number on the order, an `age_band` — and (b) **references**: resource type + id +
  `versionId` + fetch timestamp. Never the resource body, never name/DOB, never lab narrative.
- Reports rendered *with* identity are generated on demand and streamed; the PDF is not archived
  with PHI unless a retention policy says so — mirroring the existing `hl7_store_raw_messages`
  reasoning already accepted for v2.
- Structured logging must never log resource bodies. Log resource type + id + status only.

This is the choice that keeps a defensible de-identified core in a product that now touches PHI, and
it costs almost nothing — the data is re-fetchable by definition.

### 4.3 Input provenance for AI results ★
Once EHR data influences output, a result is only reproducible if you record what it consumed.
Add to `postprocessed["summary"]["ehr_context"]` (namespaced, per the existing LLM merge convention):

```json
{"fetched_at": "...", "connection_id": "...", "patient_ref_link_id": "...",
 "used": [{"type": "Observation", "id": "...", "versionId": "3", "concept": "body_weight",
           "value": 78.4, "unit": "kg"}],
 "unavailable": ["fasting_glucose"]}
```

Purpose: a reader can see the SUV was computed from an EHR weight of 78.4 kg fetched at a given
instant, and a later chart correction does not silently invalidate a signed report. Also feeds
`Provenance` on export.

### 4.4 Write permission reality → tiered export profiles ★
Most production EHRs restrict third-party writes. Structured `DiagnosticReport`/`Observation` write
is commonly refused; **document write is commonly allowed**. So do not build one write path and hope:

| Profile | Resources written | When |
|---|---|---|
| `document_only` (**default**) | `DocumentReference` + binary PDF | Works almost everywhere. Clinician sees the real report. |
| `report` | `DiagnosticReport` (conclusion + `presentedForm`) [+ the above] | Site permits report write. |
| `report_plus_observations` | `+ Observation` (coded), `Device`, `Provenance`, as a transaction `Bundle` | Site permits it *and* terminology is signed off. Full fidelity. |

Selected per connection, and **auto-downgraded** if capability discovery (§6) says the higher tier is
unsupported — logged, surfaced in the admin UI, never silently dropped.

Clinical-safety gate, unchanged from the earlier analysis and non-negotiable: `DiagnosticReport.status`
derives from `studies.reading_status` (`unread`/`in_progress` → `preliminary`, `signed` → `final`;
superseded → `entered-in-error`), with `fhir_export_on` defaulting to **`signed`**. The current code
hardcodes `"final"` at [client.py:36](backend/app/fhir/client.py#L36) — that publishes unreviewed AI
as a final radiology report and must be fixed before anything is enabled.

---

## 5. Auth — three separate concerns

"Authing through FHIR" is worth doing, with one clarification: **FHIR does not define
authentication. SMART on FHIR (OAuth2 + OIDC) does.** Three distinct concerns:

### (i) Platform → FHIR server (machine identity)
**SMART Backend Services**: asymmetric JWT client assertion (RS384/ES384), registered JWKS,
`system/*.read` + `system/DocumentReference.write` scopes, cached bearer tokens. This drives
background enrichment and report delivery with no human present. `python-jose` is already a
dependency, so signing needs no new package. Also support `basic` and static `bearer` for
open-source/test servers (HAPI), and `none` for a local dev server.

### (ii) Clinician → platform (SSO / identity)
**SMART standalone launch + OIDC** (`openid fhirUser` scope): the user clicks "Sign in with
hospital account", is redirected to the hospital's authorization server (PKCE), and returns with an
id_token whose `fhirUser` claim points at a `Practitioner`. Map that to a local `UserRecord` and RBAC
role.

Critical constraint: **this must be an additional login provider, never a replacement.** A standalone
product must keep working with no EHR configured, and admins must retain local login when the
hospital IdP is down. So extend the login page and `auth_service` with a provider, keeping
`auth_mode="jwt"` as-is rather than adding `auth_mode="smart"` — issue your own platform JWT after a
successful SMART login so all downstream RBAC (`require_permission`) is untouched. Role mapping
(`Practitioner` → radiologist/viewer) is config, with a default-deny for unmapped users.

### (iii) Which token reads the data — governance, not plumbing
A service account reading whole patient charts is frequently a governance blocker; the hospital's
audit log shows only "the AI platform" accessed the record. Reading with the **logged-in clinician's**
token (`user/*.read`) attributes access to a human and is materially easier to get approved.
Recommendation: support both — prefer the user token for interactive reads, fall back to backend
services for background/batch. Make it per-connection policy: `user_token | system_token | prefer_user`.

---

## 6. Capability-driven feature degradation ★

A standalone product installed at many sites cannot rely on someone hand-configuring what each FHIR
server supports. On connection save and then on a schedule:

1. `GET /metadata` → parse `CapabilityStatement`: FHIR version, resources present, interactions
   (`read`/`search-type`/`create`/`update`), search params, `security.extension` (SMART token/auth
   URLs).
2. `GET /.well-known/smart-configuration` → scopes and grant types actually offered.
3. Store the parsed capability profile on `fhir_connections`, plus a computed **feature matrix**:
   `can_read_patient`, `can_search_by_identifier`, `can_read_labs`, `can_read_prior_reports`,
   `can_write_document`, `can_write_report`, `can_write_observation`, `supports_subscription`.
4. Every feature checks the matrix, not a config boolean. Unsupported → feature hidden in the UI and
   skipped (logged once), not attempted-and-failed on every job.
5. Admin UI shows a green/amber/red capability table per connection — this doubles as the
   "connection test" wizard and as the artefact you hand a hospital's integration team.

This is what makes the integration deployable by someone other than you.

---

## 7. Architecture, placement, and data model

```
domain/fhir_models.py             (new)  FetchedContext, PatientLink, ExportProfile, ExportOutcome,
                                         CapabilityProfile, ObservationSpec — dataclasses/enums, stdlib only
domain/interfaces.py              (edit) + FHIRGateway (abstract: read/search/create/update)
application/fhir_connection_service.py   connection CRUD, capability discovery, feature matrix
application/fhir_patient_resolver.py     §4.1 ordered strategies + link persistence
application/fhir_context_service.py      §3 Tier 1/2 fetch → FetchedContext (Redis) + derived fields
application/fhir_export_service.py       result → resources per export profile (§4.4)
application/fhir_terminology.py          per-plugin fhir_map.yaml loader, LOINC/UCUM resolution
infrastructure/fhir/gateway.py           httpx transport, paging, retry/backoff, circuit breaker
infrastructure/fhir/auth.py              backend-services JWT, token cache, basic/bearer/none
infrastructure/fhir/smart_oidc.py        (ii) standalone launch + PKCE + id_token verification
infrastructure/queue/tasks.py     (edit) + fhir_resolve_and_enrich, fhir_export_result Celery tasks
interface/api/fhir_admin.py       (new)  connections, capability table, export log, retry (RBAC)
interface/api/auth.py             (edit) + SMART login provider endpoints
app/fhir/                         DELETE — migrate the one import at reports.py:546
```

Layer compliance: mapping/terminology/resolution = business logic → `application/`; HTTP, OAuth,
paging = I/O → `infrastructure/`; shared shapes → `domain/`; routers → `interface/`. The existing
`app/fhir/` package sits outside the four layers and should go.

Process model: **no new container.** Enrichment and export are Celery tasks; polling is Beat; SMART
login and admin are FastAPI routers. Outbound export is a *separate* Celery task (not an inline
`loop.run_until_complete` like the DICOM export at
[tasks.py:1203](backend/app/infrastructure/queue/tasks.py#L1203)) so retries are real and a FHIR
outage cannot extend pipeline wall-clock. The post-result hook only enqueues, wrapped
`try/except → logger.warning`.

### Migration `027` (additive only; v2's `hl7_messages` is not needed)

```
fhir_connections    id, tenant_id (unique), name, base_url, fhir_version, auth_mode,
                    client_id, token_url, client_secret_ref, jwks_ref, scopes,
                    patient_identifier_system, accession_identifier_system,
                    mrn_normalization (json), export_profile, read_token_policy,
                    capability (json), feature_matrix (json), capability_checked_at,
                    status, last_error, enabled, created_at, updated_at

fhir_patient_links  id, tenant_id, local_patient_ref (MRN), study_instance_uid (nullable),
                    fhir_patient_id, resolved_via ('accession'|'mrn'), state
                    ('linked'|'unlinked'|'ambiguous'), candidate_count, resolved_at,
                    invalidated_at, detail (json)   — unique (tenant_id, local_patient_ref)

fhir_export_log     id, tenant_id, connection_id, study_instance_uid, usecase_name, result_id,
                    profile, resource_type, target_resource_id, target_version,
                    status ('pending'|'sent'|'failed'|'skipped'), attempts, last_error,
                    payload_sha256, created_at, updated_at

orders              + external_order_ref (String, indexed)   -- ServiceRequest identifier
studies             + ehr_link_state (String, default 'unknown')  -- worklist badge
```

No raw resource bodies, no PHI columns anywhere in the above — consistent with §4.2. Secrets are
*references* (env key name / mounted path), never literal values in the DB, per the
no-hardcoded-credentials rule. Every state change writes an `AuditLogRecord` with
`entity_type="fhir"`.

---

## 8. Terminology (only needed for the top export tier)

Per-plugin `fhir_map.yaml`, sibling of the existing `outputs_schema.json` / `ui_schema.json`
(self-contained per the plugin rule). **Strict whitelist**: a field with no map entry is never
exported.

This matters because `measurements` is *not* clinical for most plugins — brain_mri emits
`image_dimensions`, `candidate_slices`, `scan_images_rendered`
([brain_mri/pipeline.py:401](backend/app/usecases/brain_mri/pipeline.py#L401)). The current
`isinstance(value, (int, float))` loop at [client.py:76](backend/app/fhir/client.py#L76) would publish
*"Candidate Slices: 14 unknown"* into a patient chart. `ui_schema.json` already carries `label`,
`unit`, `precision` per field (pet_ct: `"unit": "mL"` for MTV, `"g"` for TLG) — reuse it for
display/units; the map adds only codes.

Priority: `pet_ct`, `pet_ct_brain`, `coronary_cta` (Agatston), `mammography` (BI-RADS, density),
`lumbar_spine_mri` (Pfirrmann). The MedGemma narrative plugins (brain/neck/chest/abdomen/CT-\*)
correctly export **no Observations** — narrative + PDF only.

All LOINC/SNOMED codes ship as `<TBD-verify>` and the tier stays disabled until confirmed against
current releases by someone qualified. A wrong LOINC is worse than a missing one: the EHR will trend
it under the wrong concept, invisibly. UCUM units (`g/mL`, `mL`, `g`, `mm`, `{score}`) are safe now.

---

## 9. Phasing

| Phase | Deliverable | Gate | Days |
|---|---|---|---|
| **P0** Foundation | `fhir_connections` + migration 027, `infrastructure/fhir/gateway.py` (paging/retry/circuit breaker), `auth.py` (backend-services JWT, basic, bearer, none), capability discovery + feature matrix, admin connection page with the capability table | Connect to a local HAPI **and** one real sandbox; capability table renders; token refresh works | 5 |
| **P1** Identity resolution | `fhir_patient_resolver.py`, `fhir_patient_links`, MRN normalization, ambiguity handling, worklist link badge, resolve-on-ingest Celery task | Zero auto-links on ambiguous/zero hits; every link auditable with `resolved_via`; a merged MRN invalidates correctly | 4 |
| **P2** Read / enrichment | `fhir_context_service.py`: weight/height/glucose/creatinine/pregnancy → derived order fields + QA flags (incl. DICOM-vs-EHR weight mismatch); Redis transient cache; input provenance in `summary["ehr_context"]`; intake pre-fill in the onboarding UI | A PET/CT job auto-obtains weight and glucose; DB contains no name/DOB/lab bodies; a weight mismatch raises a QA flag | 6 |
| **P3** Write / delivery | Rewrite the export path with `fhir.resources` (already a declared dependency, currently unused — pin R4B and verify the module namespace in the image); three export profiles with auto-downgrade; `reading_status` → `status`; idempotent conditional update; `fhir_export_log` + retry; Celery task fired from the post-result hook and from `ReadingService.sign()` ([reading_service.py:155](backend/app/application/reading_service.py#L155)); delivery status in the admin UI | Signed study → PDF `DocumentReference` in HAPI; re-run updates rather than duplicates; server down → failed row that retries green, pipeline unaffected | 6 |
| **P4** Structured tier | `fhir_terminology.py` + `fhir_map.yaml` for the five priority plugins; transaction `Bundle` with `DiagnosticReport` + `Observation` + `Device` + `Provenance`; `$validate` clean | Zero `unknown` units, zero technical metrics exported; validator clean; tier stays off until codes are signed off | 5 |
| **P5** SMART SSO | Standalone launch + PKCE + OIDC `fhirUser`, `Practitioner` → role mapping, additive login provider (local login preserved), per-connection read-token policy | Clinician logs in via sandbox IdP, lands with correct RBAC; local admin login still works with the IdP unreachable | 5 |
| **P6** Prior reports & alerts | External `DiagnosticReport`/`Observation` history into the longitudinal (Phase 4) and CDS (Phase 3) prompts; `Communication`/`Flag` for `CriticalAlertRecord` + acknowledgement read-back | A study with only an *external* baseline produces a real longitudinal comparison | 5 |
| **P7** Optional | `Subscription` rest-hook (sites that will route to you) · read-only FHIR façade (procurement value only) · bulk `$export` for active-learning cohorts | — | 5 |
| — | Terminology curation + clinical sign-off (parallel, external) | — | 2–4 |

**Total ≈ 36–43 dev-days** for P0–P6; **≈ 15 days (P0–P2) is the first genuinely valuable slice** —
patients correctly linked and AI inputs auto-populated from the chart, with no write scopes needed
from the customer at all. P3 (+6 d) adds report delivery, which is what the hospital will ask for
first even though it should be built second.

---

## 10. Config

Replace the two-line `# FHIR (F11)` block. Note the deliberate split: **connection details live in
`fhir_connections` (DB, per tenant)**; only global behaviour and secret *references* live here. Both
existing flags are currently undocumented in `.env.example` — fix that as part of P0.

```python
# FHIR (F11) — client-only integration. Per-tenant connections live in fhir_connections.
fhir_enabled: bool = False
fhir_default_base_url: str = ""              # bootstrap/single-tenant convenience
fhir_default_version: str = "R4B"            # R4B | R5 — pinned, no negotiation
fhir_client_id: str = ""
fhir_client_secret: str = ""                 # value from env only, never in DB
fhir_client_private_key_path: str = ""       # backend-services JWT signing key
fhir_request_timeout_s: int = 30
fhir_max_retries: int = 5
fhir_page_size: int = 50
# Identity resolution
fhir_patient_identifier_system: str = ""     # required — no resolution without it
fhir_accession_identifier_system: str = ""
fhir_resolve_on_ingest: bool = True
# Read / enrichment
fhir_context_enabled: bool = False
fhir_context_cache_ttl_s: int = 3600
fhir_context_weight_mismatch_pct: float = 5.0   # → QA flag threshold
fhir_poll_enabled: bool = False
fhir_poll_interval_s: int = 300
# Write / delivery
fhir_export_enabled: bool = False
fhir_export_profile: str = "document_only"   # document_only|report|report_plus_observations
fhir_export_on: str = "signed"               # signed | result
fhir_export_include_pdf: bool = True
# SMART SSO (additive login provider; local login always remains)
fhir_smart_sso_enabled: bool = False
fhir_smart_redirect_uri: str = ""
fhir_smart_default_role: str = ""            # empty = deny unmapped users
```

---

## 11. Testing

- **Servers:** `hapiproject/hapi` in a `docker-compose.fhir.yml` *overlay* (not the default stack) for
  functional work, plus at least one vendor sandbox (SMART Health IT launcher / Epic or Cerner sandbox)
  for auth and capability realism. HAPI alone will make the integration look easier than it is.
- **Resolver (highest-value tests):** exactly-one/zero/multiple-candidate cases; leading-zero and
  prefix normalization; wrong-identifier-system → unlinked, never a demographic fallback; merged-MRN
  invalidation. A test that asserts **no auto-link on ambiguity** is the most important test in the suite.
- **PHI:** assert no fetched name/DOB/lab body reaches Postgres; assert logs contain no resource bodies.
- **Provenance:** the `ehr_context` snapshot records id + `versionId`; a later chart change does not
  mutate a signed result.
- **Export:** profile auto-downgrade when capability says no; idempotent conditional update (re-run →
  same logical id, new version); `reading_status` → `status` table; `$validate` clean.
- **Capability:** synthetic `CapabilityStatement`s (missing resource, missing write interaction,
  R5 server) → correct feature-matrix degradation.
- **Auth:** token expiry/refresh mid-batch; 401 → single re-auth, not a retry storm; SSO with the IdP
  down still permits local admin login.

---

## 12. Risks

1. **Write refusal.** Structured writes may simply not be granted. Mitigated by design (tiered
   profiles, `document_only` default) rather than by hope — but confirm scopes with each site before
   promising anything above the document tier.
2. **Identity mismatch.** Covered in §4.1; treat as the project's principal safety risk, not an
   integration detail.
3. **Terminology binding** requires qualified sign-off; ship codes disabled until then.
4. **Vendor variance.** Every site differs; §6 is the mitigation and it is not optional for a
   standalone product.
5. **PHI surface expansion.** §4.2 keeps it bounded and re-fetchable; TLS, RBAC, tenant scoping and
   audit apply to every FHIR path.
6. **Read-scope governance.** A service account reading charts may be refused; §5(iii) (user-token
   reads) is the fallback and should be built, not retrofitted.
7. **Scope creep.** P0–P2 is a shippable increment. P5 (SSO) and P6 are separate milestones — do not
   bundle them.

---

## 13. Three decisions that shape the phasing

1. **Target server(s):** a real vendor EHR (Epic/Cerner — expect restricted writes, mandatory app
   registration) or an open/self-hosted FHIR server you control (all tiers viable)? This sets how much
   of P3/P4 is realistically reachable.
2. **Single site or multi-site?** Multi-site makes `fhir_connections` + capability discovery
   mandatory rather than merely advisable. Recommendation: build it that way regardless — retrofitting
   per-tenant connections later is expensive.
3. **SSO posture:** additive provider (recommended) or the primary login? Additive keeps the product
   standalone-viable and is strictly safer operationally.
