# HL7 Integration — Implementation Plan

Scope agreed: **bidirectional HL7 v2.x messaging** (inbound ADT/ORM, outbound ORU) plus
completion of the existing **FHIR** export module. This document details **Phase 1 (inbound
RIS/EHR → platform)** to implementation-ready depth; Phases 0/2/3 and FHIR are summarized at the
end. The full roadmap is tracked in the session todo list.

---

## 0. Architectural placement (Clean/Hexagonal — per CLAUDE.md)

```
domain/interfaces.py            + HL7InboundHandler, HL7OutboundClient (abstract)
domain/hl7_models.py  (new)     HL7Message dataclass, HL7MessageType enum, ProcessResult
application/hl7_service.py (new) HL7IngestService — parse → map → persist → ACK (sync-session)
infrastructure/hl7/ (new)
    mllp_server.py              MLLP TCP framing + listener (separate process)
    parser.py                   raw bytes ↔ structured segments (hl7apy wrapper)
    ack.py                      MSA/MSH ACK/NAK builder
    mappers.py                  ADT→PatientRecord, ORM→OrderRecord
interface/api/hl7.py  (new)     REST monitoring (Phase 3)
```

**Layer rule reminder:** the MLLP listener is I/O → `infrastructure/`. Mapping/persistence business
logic → `application/`. The domain contracts it depends on → `domain/`. No layer violations.

**Process model:** the MLLP listener is a **standalone long-running process** (new docker-compose
service `hl7-listener`), *not* an endpoint inside FastAPI and *not* a Celery task. It owns a raw TCP
socket. It uses the **synchronous** DB path (`_get_sync_session()` pattern, like Celery workers)
because it runs outside the FastAPI event loop — do **not** reuse the async `OnboardingService`
directly from the socket handler.

---

## 1. Phase 0 prerequisites (must land before Phase 1 code)

### 0.2 — Dependency + config flags
- Add to `backend/pyproject.toml`: `hl7apy` (HL7 v2 parsing; permissive MIT-style license) — verify
  it resolves in the worker image before wiring.
- Add to `Settings` in `backend/app/config.py` (follow the existing `# FHIR (F11)` block style, next
  free feature number, e.g. `# HL7 v2 (F21)`):
  ```python
  hl7_enabled: bool = False
  hl7_mllp_host: str = "0.0.0.0"
  hl7_mllp_port: int = 2575          # NB: worklist_scp_port already uses 2575 — pick a
                                     #     distinct port (e.g. 2576) to avoid a clash
  hl7_sending_application: str = "MRCV"
  hl7_sending_facility: str = "MRCV_AI"
  hl7_accept_version: str = "2.5.1"
  hl7_default_tenant: str = "default"
  # outbound (Phase 2)
  hl7_outbound_enabled: bool = False
  hl7_outbound_host: str = ""
  hl7_outbound_port: int = 0
  ```
- Document every flag in `.env.example`.

### 0.3 — Domain contracts (`domain/`, stdlib-only imports)
- `domain/hl7_models.py`:
  - `class HL7MessageType(str, Enum)`: `ADT_A01, ADT_A04, ADT_A08, ADT_A40, ORM_O01, OMG_O19, ORU_R01, UNKNOWN`
  - `@dataclass ParsedPatient`: `mrn, name_raw, birth_date, sex, tenant_id` (name/DOB held only
    transiently for de-identification; never persisted raw)
  - `@dataclass ParsedOrder`: `mrn, placer_order_number, filler_order_number, modality,
    universal_service_id, priority, reason_for_study, referrer, study_instance_uid, scheduled_dt`
  - `@dataclass ProcessResult`: `ack_code ("AA"|"AE"|"AR"), message_control_id, error_detail`
- `domain/interfaces.py`: add `HL7InboundHandler` (abstract `handle(raw: str) -> ProcessResult`).

---

## 2. Phase 1 tasks (inbound)

### 1.1 — MLLP listener (`infrastructure/hl7/mllp_server.py`)
- Implement MLLP framing: messages wrapped `<VT>...<FS><CR>` (`\x0b` … `\x1c\x0d`). Read until
  `\x1c\x0d`, strip the vertical-tab header.
- Concurrency: `socketserver.ThreadingTCPServer` (simplest, adequate for RIS message rates) or
  asyncio `start_server`. Recommendation: **threaded server + sync DB session per message** —
  matches the Celery sync-session precedent and avoids an event loop in the listener.
- Each accepted message → hand raw string to `HL7IngestService.handle()` → write back the ACK bytes
  (re-framed with MLLP envelope).
- Entry point: `backend/app/hl7_listener.py` (module `python -m app.hl7_listener`) that reads
  `Settings`, binds host/port, and serves forever. Guard the whole thing on `hl7_enabled`.
- Structured logging via `structlog.get_logger(__name__)`; log connect/disconnect, message control
  ID, ACK code — **never log raw PHI segments at INFO** (PID/PV1 contain name/DOB). Log raw only at
  DEBUG, and only if `phi_deidentify_enabled` is False.

### 1.2 — Parser + validator + ACK (`parser.py`, `ack.py`)
- `parser.py`: wrap `hl7apy.parser.parse_message`. Extract MSH-9 (message type) → map to
  `HL7MessageType`; MSH-10 (control ID); MSH-12 (version). Provide typed field accessors that never
  raise on missing optional fields (RIS feeds vary wildly).
- Validation rules → NAK (`AE`) with reason: unparseable MSH, unsupported message type, missing
  MRN (PID-3). Malformed but recoverable → best-effort + `AA`. Downstream DB failure → `AE`.
- `ack.py`: build ACK reusing inbound MSH-10 as MSA-2, swapping sending/receiving app+facility,
  stamping our `hl7_sending_application/facility`. Support `AA`/`AE`/`AR`.

### 1.3 — ADT → PatientRecord (`mappers.py` + `application/hl7_service.py`)
- Map PID-3 (or PID-2) → `patient_ref` (MRN). PID-8 sex (`F/M/O`) → existing enum
  (`female/male/other`); anything else → `other`.
- **De-identification (critical):** PID-5 patient name and PID-7 DOB must **not** be persisted. DOB →
  compute `age_band` (`0-17|18-39|40-64|65+`) at ingest time. This matches the de-identified
  `PatientRecord` contract and `phi_deidentify_enabled` intent. Age is derived from DOB vs. message
  timestamp (MSH-7); if DOB absent, `age_band = None`.
- Upsert by `(tenant_id, patient_ref)` — mirror `OnboardingService.create_order`'s upsert
  (`ix_patients_tenant_ref` is the unique key). A08 updates sex/age_band; A40 (merge) updates the
  surviving MRN and audits the merge (do **not** hard-delete the merged patient without user sign-off
  — CLAUDE.md forbids destructive ops without confirmation).
- Tenant: from a configured mapping of receiving facility → tenant, else `hl7_default_tenant`.

### 1.4 — ORM/OMG → OrderRecord
- Map ORC/OBR: OBR-4 universal service ID → `modality` + `body_part` + `region_profile` (needs a
  lookup table; start with a small configurable dict, log unmapped codes). ORC-1/OBR-11 priority
  (`S`=stat → `stat`, else `routine`). OBR-31 / NTE reason → `indication`. ORC-12 → `referrer`.
- `orders.indication` is **NOT NULL** — if the message has no reason, default to the service
  description text so the row is valid; flag it in the audit detail.
- `orders.consent_ack` defaults `false` for HL7-originated orders (consent is captured at the
  reception desk, not in the RIS feed). Add a **new `HL7IngestService` method** rather than calling
  `OnboardingService.create_order`, whose validation hard-requires consent and runs async.
- Study linking: if the message carries a Study Instance UID (ZDS-1 or OBR-referenced), link it; else
  leave null — the existing MRN auto-match in `get_clinical_for_study` will resolve it later.
- Idempotency: key on `(tenant, placer_order_number)`; a repeat message updates rather than
  duplicates. (Requires a placer-order column — see 1.5.)

### 1.5 — Message log + migration `027`
- New table `hl7_messages` (Alembic `027`, `down_revision = "026"`, additive only):
  ```
  id (uuid pk), direction ('inbound'|'outbound'), message_type, message_control_id,
  sending_facility, raw_hash (sha256 — not raw PHI at rest unless retention permits),
  raw_stored (Text, nullable — populated only if PHI retention allows),
  status ('processed'|'error'|'ignored'), ack_code, error_detail (Text),
  patient_ref, order_id (fk orders.id SET NULL), study_instance_uid,
  tenant_id, created_at
  ```
- Add `orders.placer_order_number` (String, nullable, indexed) for HL7 idempotency — additive column,
  same migration.
- Every state-changing ingest writes an `AuditLogRecord` (`entity_type="hl7"`), reusing the
  `_audit` pattern from `OnboardingService`.
- **Do not** store raw messages containing PHI at rest when `phi_deidentify_enabled` is True — store
  the sha256 hash + de-identified parsed fields only.

---

## 3. Testing (Phase 1 slice of 3.4)
- Unit: MLLP framing round-trip; parser against canned ADT^A01/A08, ORM^O01 fixtures (place in
  `backend/tests/fixtures/hl7/`); ACK builder AA/AE/AR; DOB→age_band boundaries.
- Integration: raw ADT string → `HL7IngestService.handle()` → assert `PatientRecord` upserted,
  de-identified, correct ACK returned; raw ORM → `OrderRecord` created with defaults.
- Negative: missing MRN → `AE` NAK; unsupported type → `AE`; duplicate placer order → single row.

---

## 4. Phases 2–3 + FHIR (summary — detailed on Phase 1 sign-off)

- **Phase 2 (outbound ORU):** `application/hl7_oru_builder.py` builds ORU^R01 from a completed
  `Result` (report text + measurements → OBX). `infrastructure/hl7/mllp_client.py` sends over MLLP,
  waits for ACK, retries with back-off — run as a **Celery task** fired from the post-result hooks in
  `infrastructure/queue/tasks.py`, gated on `hl7_outbound_enabled`, wrapped `try/except →
  logger.warning` so a hook failure never breaks the pipeline (CLAUDE.md rule).
- **Phase 3:** `interface/api/hl7.py` (list/view/reprocess/status, RBAC-gated) + Next.js `/admin`
  page (via `ui/src/lib/api.ts`) + `hl7-listener` docker-compose service (+ CPU override) +
  conformance/soak testing.
- **FHIR expansion:** finish `backend/app/fhir/client.py` — real LOINC/UCUM codes + units on
  `Observation` (currently `"unit": "unknown"`), post nested observations correctly (currently only
  the DiagnosticReport is POSTed; `_observations` is dropped), and optionally add inbound FHIR
  ingest. Complementary to v2, not a replacement.

---

## 5. Estimate

| Phase | Days |
|-------|------|
| 0 (foundation) | 3.5 |
| 1 (inbound) | ~9.5 |
| 2 (outbound) | ~5 |
| 3 (ops/UI/test/soak) | ~13 |
| FHIR expansion | ~3 |
| **Total** | **~34–40 dev-days** |

Smallest useful shippable slice: **Phase 0 + Phase 1 = ~13 days** (a live inbound RIS feed
populating patients/orders).

---

## 6. Day-by-date execution schedule (executed through Claude)

Each row is one implement → verify → merge cycle with a gate. The bottleneck is human
review/merge of each day's change, not Claude's coding speed. Weekends skipped.
**Phase 0 completed 2026-07-24.**

| Date | Task | Deliverable | Gate (done when…) |
|------|------|-------------|-------------------|
| 07-24 ✅ | 0.2 / 0.3 | `hl7apy` dep, `hl7_*` flags, `domain/hl7_models.py`, HL7 interfaces | compiles + imports + flags load ✅ |
| 07-27 | 1.1a | `infrastructure/hl7/mllp_server.py` — MLLP framing + threaded TCP server | framing round-trip unit test passes |
| 07-28 | 1.1b | `app/hl7_listener.py` entrypoint + config wiring + graceful start/stop | `nc` smoke test: connect, send, get bytes back |
| 07-29 | 1.2a | `parser.py` — hl7apy wrapper, MSH-9/10/12 extraction, safe field accessors | parses canned ADT/ORM fixtures, no raise on missing optionals |
| 07-30 | 1.2b | `ack.py` — AA/AE/AR builder + validator (missing MRN, bad type → AE) | ACK unit tests pass; NAK on invalid |
| 07-31 | 1.3a | ADT→PatientRecord mapper — DOB→age_band, sex normalise, name dropped | mapper unit tests incl. age-band boundaries + de-id assertion |
| 08-03 | 1.3b | `application/hl7_service.py` — sync-session upsert by (tenant,MRN); A08/A40 | integration: ADT string → patient row upserted, de-identified |
| 08-04 | 1.4a | OBR-4 service-code → modality/body_part/region_profile lookup + ORM mapper | unmapped codes logged; mapper unit tests |
| 08-05 | 1.4b | HL7 order intake (consent default, indication default, placer idempotency) | integration: ORM → single order row; repeat = update not dup |
| 08-06 | 1.5a | Alembic `027`: `hl7_messages` table + `orders.placer_order_number` + ORM models | `alembic upgrade`/`downgrade` clean on a scratch DB |
| 08-07 | 1.5b | Message-log persistence + audit wiring; PHI hash-only when de-id on | integration: log row + AuditLogRecord written per message |
| 08-10 | 1.x | Phase 1 end-to-end suite + fixes | **GATE: inbound RIS feed green end-to-end** |
| 08-11 | 2.1a | `application/hl7_oru_builder.py` — ORU^R01 from Result, OBX from measurements | builder unit tests vs sample Result |
| 08-12 | 2.1b | Report text + QA-flag → OBX mapping, edge cases | round-trip parse of generated ORU |
| 08-13 | 2.2a | `infrastructure/hl7/mllp_client.py` — send + ACK wait + retry/back-off | unit test against a mock MLLP server |
| 08-14 | 2.2b | Celery task wrapper for outbound send | task enqueues + runs in worker |
| 08-17 | 2.3 | Wire into post-result hooks in `tasks.py` (gated, try/except→warning) | **GATE: full round-trip (ADT/ORM in → ORU out)** |
| 08-18 | 3.1a | `interface/api/hl7.py` — list/view messages | endpoints return log data |
| 08-19 | 3.1b | Reprocess-failed + connection-status endpoints + RBAC permission | API tests pass; permission-gated |
| 08-20 | 3.2a | `ui/src/lib/api.ts` methods + `/admin` HL7 page scaffold | page loads message list |
| 08-21 | 3.2b | Message-log table UI + interface-status panel + reprocess action | manual UI walkthrough |
| 08-24 | 3.3 | `hl7-listener` docker-compose service (+ CPU override) + healthcheck | container starts, port reachable |
| 08-25 | 3.4a | Expand unit coverage (encodings, segment variants, edge cases) | coverage target met |
| 08-26 | 3.4b | Full round-trip integration test in CI | CI green |
| 08-27 | 3.5a | Conformance testing vs partner RIS/EHR harness *(needs partner access)* | test messages exchanged |
| 08-28 | 3.5b | Fix conformance issues found | partner interface validated |
| 08-31 | 3.5c | Soak run + sign-off | **GATE: production-ready** |
| 09-01 | FHIR | Real LOINC/UCUM codes + units on Observations in `fhir/client.py` | valid FHIR bundle |
| 09-02 | FHIR | POST nested Observations correctly (currently dropped) | observations persisted on server |
| 09-03 | FHIR | Optional inbound FHIR ingest + docs | end-to-end FHIR round-trip |

### External dependencies that can shift dates
- **08-04 (1.4a)** needs the partner RIS's **OBR-4 service-code dictionary** — request it now.
- **08-27–08-31 (3.5)** needs **partner RIS/EHR test-harness access** — schedule with the integration
  team ahead of time; without it these dates slip.

