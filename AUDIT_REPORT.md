# MR Computer Vision Platform — System Architecture Audit

**Date:** 2026-08-03
**Scope:** Full-system evidence-based audit (Phase 1–2 style: system overview + architecture
assessment). Supersedes the 2026-05-24 revision of this file, which documented the
TotalSegmentator/BraTS-SegResNet MRI pipelines and PET/CT bugs that predate the July 2026 MedGemma
rewrite — none of that content is still accurate and it has been fully replaced below.
**Method:** Six parallel read-only sweeps across backend layers, use-case plugins, database/RBAC,
queue/config/LLM wiring, infra/deployment, and frontend. Every claim below is cited to a
file/line; anything that couldn't be verified is called out explicitly rather than assumed.

---

## Executive Summary

This is a clinical radiology AI platform: FastAPI backend + Celery/Redis async inference +
PostgreSQL + MinIO + Orthanc PACS, Next.js 14 frontend, 17 imaging use-case plugins covering
CT/MRI/PET-CT/mammography/coronary CTA. The intended architecture is a clean 4-layer hexagonal
design (domain/application/infrastructure/interface); in practice the **application layer breaks
its own boundary rules pervasively** — most services import SQLAlchemy and infrastructure modules
directly instead of going through the domain repository interfaces, and one service imports the
FastAPI-layer websocket manager. The use-case plugin system itself is a genuine strength: all 17
plugins are file-complete, self-contained (no cross-plugin imports), and share a well-factored
MedGemma orchestration hook for 13 of them. The most consequential findings are operational, not
architectural: a `DELETE /studies/{uid}` and an admin `/reset` endpoint that destroy data with
**no permission check and no audit trail**, an audit log that is itself an unaudited deletion
target, several security-relevant settings absent from `.env.example` with insecure hardcoded
defaults (`jwt_secret_key="changeme"`), zero CI, zero frontend tests, and ~zero test coverage on
the 17 imaging pipelines (the highest-clinical-risk code in the repo).

---

## 1. System Overview

**Domain:** Clinical radiology AI — every use case ingests a DICOM study from PACS, runs an
inference pipeline, and produces a structured + narrative result for radiologist review/sign-off.

**Core workflow:** Study arrives in Orthanc → routed to a matching use-case by body part/modality
→ Celery job runs `preprocess → infer → postprocess` → result stored (versioned) → optional LLM
report-authoring, VLM QA, CDS, longitudinal comparison, DICOM SR/Seg export, alerting, active
learning queueing, FHIR Observation/Condition derivation → radiologist claims/reads/signs via the
reading workflow → optional referring-physician portal share.

**Tech stack (backend):** Python 3.11, FastAPI 0.111, SQLAlchemy 2.0 (async, asyncpg) +
sync engine for Celery, Celery 5.4/Redis 7, MinIO, structlog, Pydantic 2.8. Imaging: SimpleITK,
MONAI 1.4, TotalSegmentator ≥2.2, nnU-Net v2, pydicom, nibabel. LLM: Gemini
(`google-generativeai`) and a local MedGemma-via-Ollama client. FHIR (`fhir.resources`) and HL7
(`hl7apy`) are dependencies but see §2.6/§4 for how much is actually wired up.

**Tech stack (frontend):** Next.js 14 (App Router) + React 18, TypeScript strict mode, Tailwind,
Cornerstone for DICOM viewing, SWR as a thin cache layer over a hand-written `fetch()` wrapper
(`ui/src/lib/api.ts`) — no axios/react-query, no Redux/Zustand (auth state is one React Context),
no charting library (hand-rolled inline SVG).

**Deployment:** Docker Compose, 9 services (postgres, redis, minio, orthanc, backend, worker,
beat, ohif, ui, nginx). GPU worker via NVIDIA runtime with a CPU-only override compose file.

---

## 2. Architecture

### 2.1 Layered architecture — as designed vs. as built

CLAUDE.md specifies a strict 4-layer hexagonal design with import rules (domain: stdlib only;
application: domain only; infrastructure: domain+application; interface: all layers). Verified
against the actual codebase:

| Layer | Compliance | Evidence |
|---|---|---|
| `domain/` | **Compliant.** Zero fastapi/sqlalchemy/infrastructure/interface imports. | grep across `domain/**`, zero matches |
| `application/` | **Non-compliant, pervasively.** | 15 files import `sqlalchemy` directly; ~85 import-sites across 22 files import `app.infrastructure.*` directly (mostly `infrastructure.database.models` — the ORM records — bypassing the `domain.interfaces.*Repository` abstraction entirely); `alerting_service.py:401,527` imports `app.interface.api.ws` (application → interface, the layer the rule explicitly forbids) |
| `infrastructure/` | **One violation.** | `infrastructure/queue/tasks.py:1608` imports `app.interface.api.ws` (infra → interface) |
| `interface/` | Compliant (may import all layers by design). | — |

**Reading of this:** the domain layer (interfaces, dataclasses, enums) is genuinely clean and
well-factored — 10 interfaces (`StudyRepository`, `SeriesRepository`, `JobRepository`,
`ResultRepository`, `UseCaseRegistryRepository`, `AuditRepository`, `ArtifactStore`, `PACSClient`,
`HL7InboundHandler`, `HL7OutboundClient`) and a `UseCasePipeline` Protocol exist, but the
application layer largely doesn't use them — it reaches past the interfaces straight into
`infrastructure.database.models` ORM classes. The abstraction exists on paper; in the running
code it's bypassed often enough that "inject a fake `StudyRepository` for testing" would not
actually decouple most services from Postgres today. This is the single largest architectural gap
in the codebase — not a few stray imports, but the dominant pattern across 22 of 41
application-layer files.

Two overlapping pipeline contracts also exist: `domain.interfaces.UseCasePipeline` (Protocol,
`preprocess` with no `event_loop` param) and `usecases/base.py:BasePipeline` (ABC, `preprocess`
*with* `event_loop`). All 17 plugins subclass `BasePipeline`; the domain Protocol appears to be
vestigial/unused by anything concrete.

### 2.2 Textual module map

```
backend/app/
├── domain/            7 files  — interfaces.py, models.py (20 dataclasses), enums.py (15 enums),
│                                 permissions.py, hl7_models.py, fhir_terminology.py
├── application/       41 files — flat; 37 business-logic services (see §2.3)
├── infrastructure/
│   ├── database/      models.py (27 tables) · repositories.py (Pg*Repository) · session.py
│   ├── orthanc/        client.py — OrthancPACSClient(PACSClient)
│   ├── storage/         client.py — MinIOArtifactStore(ArtifactStore)
│   ├── queue/          celery_app.py · tasks.py (1693 lines — pipeline lifecycle + hooks)
│   ├── llm/             gemini_client.py · medgemma_client.py
│   └── dicomweb/       client.py — QIDO/WADO-RS client
├── interface/
│   ├── api/            20 routers (see §2.6)
│   ├── middleware/      auth.py
│   └── schemas/         job/result/study/usecase Pydantic models
├── usecases/           base.py (BasePipeline ABC) + 17 self-contained plugin dirs (see §2.4)
├── fhir/               client.py (FHIRClient, outbound DiagnosticReport) · fhir_export_service.py
└── services / reports / dicom / deidentify   (additional top-level packages outside the 4-layer
                                                 set — not covered by the CLAUDE.md layer table;
                                                 not deep-audited in this pass)
```

Also present outside `backend/`: `orthanc/` (Dockerfile for the PACS container), `ohif/`
(viewer config), `nginx/`, `backend/external/` (vendored `GMIC`, `SAM-Med3D-main`).

### 2.3 Application-layer service catalog (41 files)

Grouped by concern — job lifecycle & routing (`job_orchestrator`, `routing_service`,
`usecase_registry`, `study_service`, `result_service`, `qa_service`, `ensemble_service`,
`gpu_scheduler`), RBAC/auth (`auth_service`, `role_service`), reporting/narrative
(`llm_report_service`, `report_service`, `abdomen_report_service`, `pet_ct_narrative_service`,
`mammography_narrative_service`, `mammography_radiologist_service`,
`coronary_cta_narrative_service`, `cds_service`, `longitudinal_service`), clinical-data-standard
writers (`observation_service`, `condition_service`, `dicom_demographics`, `practitioner_service`,
`fhir_terminology.py`), operational (`alerting_service`, `retention_service`,
`active_learning_service`, `audit_service`, `analytics_service`, `batch_service`,
`ab_testing_service`, `model_registry.py`, `portal_service`, `onboarding_service`, `cpt_service`,
`vlm_qa_service`), mammography-specific (`mammography_service`), and `ct_report_regions.py` (region
metadata shared by the 13-plugin MedGemma hook). Full one-line-per-file list captured during this
audit is available on request; omitted here to keep this document from ballooning.

### 2.4 Use-case plugin catalog (17 plugins — all file-complete, all self-contained)

| Plugin | Body region | Model / architecture |
|---|---|---|
| `abdomen_ct` | Abdomen/pelvis CT | MedGemma two-pass + SAM-Med3D tumour measurement + deterministic organ-size grounding (new, see below) |
| `ct_brain`, `ct_chest`, `ct_face`, `ct_lower_limb`, `ct_lumbar_spine`, `ct_neck` | CT, per-region | MedGemma two-pass (templated pipeline, `infer()` is a no-op — real inference runs in the Celery task hook) |
| `abdomen_mri`, `brain_mri`, `chest_mri`, `lumbar_spine_mri`, `lower_limb_mri`, `neck_mri` | MRI, per-region | MedGemma multiparametric two-pass (sequence classification → labeled montage → same shared hook) |
| `coronary_cta` | Cardiac CTA | Agatston calcium scoring (fully implemented) + TotalSegmentator heart ROI; DL stenosis/CAD-RADS **stubbed** — `NotImplementedError` at `coronary_cta/pipeline.py:690`, caught and gracefully degraded to calcium-only at `:1234`. Self-disclosed in its own manifest as "planned but not implemented." |
| `mammography` | Breast MG | NYU GMIC 5-model ensemble; falls back to a disclosed, non-diagnostic placeholder (`qa_flags: ["placeholder_no_model"]`) when weights aren't present |
| `pet_ct` | Whole-body FDG PET/CT | PERCIST 1.0 SUV threshold (default) or optional SwinUNETR DL segmentation; TotalSegmentator for physiologic-uptake suppression |
| `pet_ct_brain` | Brain PET/CT | Deterministic AAL3/MNI152 atlas SUVR + asymmetry index + Centiloid — no VLM, no DL |

13 of 17 (all `ct_*`, all `*_mri`, plus `abdomen_ct`) share one central orchestration hook —
`_CT_REPORT_USECASES` in `application/ct_report_regions.py:27-43`, driving the scan→flag→report
MedGemma sequence from `infrastructure/queue/tasks.py:962`. The remaining 4
(`coronary_cta`, `mammography`, `pet_ct`, `pet_ct_brain`) have fully bespoke pipeline logic. No
plugin imports another plugin's internals anywhere in the codebase — the "self-contained plugin"
rule holds without exception.

**New/untracked:** `abdomen_ct/organ_grounding.py` — a Tier-1 deterministic TotalSegmentator-based
organ-size measurement module (liver/spleen/kidneys volume + span, aortic diameter), built because
the MedGemma VLM alone "missed a 15.5 cm hepatomegaly while confabulating a mass" (module
docstring). Gated by `settings.organ_grounding_enabled` (default `True`), abdomen_ct-only, wired
into the shared hook at `tasks.py:1199-1225`. This matches project memory on the Tier-1
organ-grounding work; the mass-verification "option C" approach mentioned in that memory is
correctly absent from this file (it was disabled after failing validation).

### 2.5 Pipeline lifecycle (Celery `run_usecase_pipeline`, `tasks.py:368`)

Verified against code, not assumed from CLAUDE.md: PREPROCESSING 0.05 → 0.15 → [VLM QA 0.30,
gated] → INFERRING 0.40 → POSTPROCESSING 0.75 → [CDS 0.80, gated] → [Longitudinal 0.83, gated] →
**one of four mutually-exclusive report-authoring branches at 0.84** (mammography / PET-CT /
coronary-CTA / CT-report-family MedGemma — CLAUDE.md's summary collapses these into one step) →
Storing artifacts 0.85 → [DICOM SR/Seg export 0.90, gated] → post-result hooks (critical alerting,
active-learning queueing, prior-comparison audit entry, FHIR Observation derivation) → COMPLETED
1.0. Non-happy-path states: PENDING 0.0 (retry), CANCELLED 0.0, FAILED 0.0. DICOM export runs
*outside* the `_run_post_result_hooks` function despite being conceptually a post-result hook —
a naming/grouping nuance worth knowing before touching either.

### 2.6 API surface

20 routers under `interface/api/`: `auth`, `studies` (+ an `orthanc_router` sub-router), `jobs`,
`results`, `reports`, `reading` (claim/assign/sign workflow), `roles`, `critical_alerts`,
`clinical` (FHIR Observation/Condition read), `dicomweb` (QIDO/WADO proxy — includes the
series-filter used to hide non-diagnostic series), `mammography`, `onboarding`, `practitioners`,
`admin`, `usecases`, `health`, `landing`, `ws` (websocket), `medgemma_debug`. Two helper modules
(`dependencies.py`, `validators.py`) carry no routes.

### 2.7 Database (27 tables, 33 linear migrations, no branches)

Logical groups: studies/series/jobs/results (versioned via `is_latest`), FHIR-shaped clinical data
(`observations`, `conditions` — migrations 030/031), RBAC (`users`, `tenants`, `roles`,
`user_roles` — 16-permission catalog, 5 system roles), patient intake (`patients`, `orders`),
radiologist-authored reports (`mammography_reports`, `mri_reports`), critical alerting
(`critical_alerts`, `alert_rules`, `alert_history`), audit (`audit_log`, with a typed
`actor_type`/`action_crude` FHIR-AuditEvent-coded pair added in migration 032 alongside the
original free-text columns), portal shares, retention policy config. The result-versioning
invariant ("previous latest set False before inserting new latest") is implemented **twice,
independently** — `tasks.py:181-213` (`_save_result`, the sync/Celery path, which is the function
CLAUDE.md names) and `repositories.py:344-391` (`PgResultRepository.save`, the async API path,
under a different name). A maintainer following CLAUDE.md's pointer literally would find only one
of the two copies.

### 2.8 Deployment topology

9 Compose services: postgres, redis, minio, orthanc (DICOM :4242, admin REST bound to
`127.0.0.1:8042` only — the unauthenticated admin UI is deliberately not exposed), backend (API,
live source mount), worker (GPU, `NVIDIA_VISIBLE_DEVICES=all` + a health-probing entrypoint script
that reassigns `CUDA_VISIBLE_DEVICES` around a known-bad GPU), beat, ohif, ui, nginx (single public
entrypoint; explicitly `return 404`s `/orthanc/` to keep the PACS admin UI off the public path; has
a custom regex route that proxies only the QIDO series-list query through the backend for
non-diagnostic-series filtering, while all other DICOMweb traffic goes straight to Orthanc). A
`docker-compose.cpu.yml` overlay swaps the worker to the CPU `api` build target, forces
`runtime: runc`, and clears the GPU device reservation. `ui`'s `docker-compose.yml` bakes a literal
production IP (`103.93.216.37`) into `NEXT_PUBLIC_API_URL`/`NEXT_PUBLIC_DICOMWEB_URL` rather than
templating it — worth knowing before spinning up a second environment from this compose file
as-is.

---

## 3. Strengths (evidence-backed)

- **Plugin isolation is real, not aspirational.** All 17 use cases are file-complete and
  genuinely self-contained; zero cross-plugin imports found anywhere.
- **Domain layer is clean.** Interfaces, dataclasses, and enums have zero framework leakage — the
  one part of the hexagonal design that's fully honored.
- **Graceful degradation is a consistent pattern, not an afterthought.** `coronary_cta`'s stenosis
  stub, `mammography`'s placeholder model, and `pet_ct`'s PERCIST fallback all disclose their
  degraded state via `qa_flags` rather than failing silently or crashing.
- **Deliberate incident-driven fixes are documented in the code itself.** `celery_app.py:26-33`'s
  comment about a beat task stuck since 2026-06-18, and the Orthanc-admin-UI/localhost-binding +
  nginx 404 block, both read as real production lessons encoded directly at the point of the fix.
- **Logging convention adopted almost universally** — 113/114 files use `structlog`; a single
  stdlib-`logging` holdout (`longitudinal_service.py`).
- **RBAC is coherent and enforced end-to-end for the workflows it covers** — 16-permission catalog,
  5 system roles, consistently audit-logged for role mutations and reading-workflow transitions
  (claim/assign/report/sign).
- **FHIR export, while self-rated only 27/100 conformant, is a real substantive implementation**
  (419-line `FHIRClient`, `ObservationService`, `ConditionService`, per-plugin `fhir_map.yaml`) —
  not vaporware, unlike the HL7 side (§4).

---

## 4. Weaknesses & Technical Debt

Ordered roughly by severity/blast-radius, each rated **Low/Medium/High**.

### HIGH

1. **Destructive, unauthenticated, unaudited data-deletion endpoints.**
   `DELETE /studies/{study_uid}` (`interface/api/studies.py:144-192`) and
   `POST /api/admin/reset` (`interface/api/admin.py:692-774`) both cascade-delete studies,
   series, jobs, results, and (for `/reset`) the entire alert/review-queue/audit-log/share-link
   tables plus every MinIO object — with **no `require_permission` dependency on either route**
   and **no `AuditLogRecord` written for either operation**. `/reset` deletes `audit_log` itself,
   so even a retroactive forensic reconstruction is impossible. In a clinical-data system this is
   the single highest-risk finding in this audit — patient imaging/results can be permanently
   destroyed by any caller who can reach the endpoint, with zero trace.
   *Mitigation:* add `require_permission(Permission.DATA_PURGE)` / `STUDY_DELETE` to both routes
   and an explicit audit write **before** the destructive operation executes (not after, given
   `/reset` deletes its own audit trail).

2. **Application layer does not actually honor the repository-interface abstraction.**
   22 of 41 application-layer files import `infrastructure.database.models` (ORM) or other
   infrastructure modules directly rather than the injected `domain.interfaces.*Repository`
   contracts; 15 import `sqlalchemy` directly. The abstraction CLAUDE.md documents as a hard rule
   is the exception, not the norm, in the actual codebase. Practical consequence: most
   application-layer unit tests that exist today (`tests/unit/application/`) are necessarily
   coupled to SQLAlchemy/Postgres rather than testable against a fake repository, and any future
   swap of the persistence layer would touch the majority of `application/`, not just
   `infrastructure/`.

3. **Zero test coverage on the highest-clinical-risk code.** 14 of 17 imaging pipelines have no
   tests at all (only `coronary_cta` and a `pet_ct` classification sub-function are covered, and
   narrowly). No CI pipeline exists at all (no `.github/workflows`, confirmed absent). The frontend
   has zero automated tests of any kind (no jest/vitest/playwright config, no `*.test.tsx` files).
   For a platform whose outputs are "potentially patient-affecting" per CLAUDE.md's own framing,
   this is a large gap between stated intent and actual verification.

4. **Insecure defaults for security-relevant settings, several undocumented.**
   `jwt_secret_key: str = "changeme"`, `phi_hash_salt: str = "changeme"`, `secret_key`,
   `orthanc_password="orthanc"`, `postgres_password`/`minio_secret_key = "changeme_in_production"`
   are all real shipped defaults in `config.py`. Worse: `auth_mode`, `jwt_secret_key`,
   `multi_tenant_enabled`, `phi_deidentify_enabled`/`phi_hash_salt`, `alerting_enabled`,
   `retention_enabled`, `active_learning_enabled`, `model_registry_enabled`, `worklist_enabled`,
   and several report-layout strings exist as real `Settings` fields but are **absent from
   `.env.example`** — an operator following the example file alone would never learn these need
   overriding, and several would silently run with insecure defaults in production.

### MEDIUM

5. **`application → interface` and `infrastructure → interface` layer violations.**
   `alerting_service.py:401,527` and `infrastructure/queue/tasks.py:1608` all import
   `app.interface.api.ws` (the FastAPI websocket manager) from lower layers, inverting the
   dependency direction CLAUDE.md's table forbids outright (interface may depend on all layers;
   nothing may depend on interface).

6. **`is_latest` result-versioning invariant is duplicated under two different names in two
   different modules** (`tasks.py:_save_result` sync path vs. `repositories.py:PgResultRepository.save`
   async path) — CLAUDE.md's "read `_save_result()` first" guidance only surfaces one of the two
   copies to a reader following the docs literally.

7. **HL7 is scaffolding-only despite substantial planning documents.** `HL7_INTEGRATION_PLAN.md`
   describes a full MLLP listener/parser/ACK/mapper stack; only the domain dataclasses
   (`domain/hl7_models.py`, 114 lines, no logic) exist. No `infrastructure/hl7/` directory, no
   MLLP server, no listener entry point exist on disk. Anyone reading the plan doc without
   checking code would reasonably believe more is built than actually is — this is exactly the
   kind of doc/code mismatch CLAUDE.md says to flag.

8. **Config/`.env.example` drift.** `gemini_model` code default is `gemini-1.5-flash`;
   `.env.example` documents `gemini-2.5-flash`. Small, but a real example of the config surface
   not being kept in lockstep with documentation.

9. **Frontend has 3 independent copies of the same auth-header-fetch logic**
   (`lib/api.ts`'s `fetchAPI`/`fetchBlob`, `components/FusedViewer.tsx:51`,
   `lib/useAuthenticatedImage.ts:74`) — two of which are raw `fetch()` calls that bypass the
   central API module CLAUDE.md explicitly says not to bypass.

10. **`ui/tsconfig.tsbuildinfo` is tracked in git** (committed in `90078c6c`), a TypeScript
    incremental-compile cache that's environment-specific and conventionally gitignored — explains
    why it shows as modified on nearly every commit touching TypeScript, with no product value in
    version control.

### LOW

11. Vestigial/dead abstraction: `domain.interfaces.UseCasePipeline` Protocol appears unused by any
    concrete pipeline (all 17 subclass `usecases/base.py:BasePipeline` instead, which has a
    slightly different signature).
12. `celery_app.py`'s multi-GPU queue routing (`gpu_worker_queues` config, routes for
    `run_usecase_pipeline_gpu_<queue>`) is speculative scaffolding — no such task is actually
    defined in `tasks.py`.
13. One remaining stdlib-`logging` holdout (`longitudinal_service.py`) against an otherwise
    universal `structlog` convention.
14. `AUDIT_REPORT.md` (this file, prior revision) and other planning docs
    (`FHIR_R4_COMPLIANCE_AUDIT.md` self-rates 27/100 conformant) had drifted significantly out of
    date relative to the shipped system — a general note that architecture/planning docs in this
    repo need an explicit "supersedes" trail when a major rewrite (like the July MedGemma
    migration) happens, or they actively mislead the next reader.

---

## 5. Open Questions (not derivable from the code — needs a human answer)

- Is the application-layer's bypass of the repository-interface abstraction (finding #2) an
  accepted, deliberate tradeoff (speed of delivery over strict layering) or unintentional drift
  that should be scheduled for cleanup? This determines whether it's worth a remediation plan at
  all.
- Are the two destructive unauthenticated endpoints (finding #1) actually reachable in the current
  deployment (e.g. behind a network boundary / internal-only), or exposed on the public nginx
  entrypoint? This audit did not find nginx blocking either path the way `/orthanc/` is blocked —
  worth an explicit confirmation before treating severity as "theoretical."
- Is HL7 v2 (finding #7) still a live roadmap item, or has `FHIR_INTEGRATION_PLAN.md`'s framing
  ("HL7 v2 ... parked") fully superseded it? If parked, the domain-stub files and plan doc could be
  either removed or explicitly marked archival to avoid confusing future contributors.
- Should `backend/app/services`, `app/reports`, `app/dicom`, `app/deidentify`, `app/fhir` (found
  to exist alongside the 4 canonical layers, §2.2) be formally folded into the layer model in
  CLAUDE.md, or are they intentionally exempt? They weren't deep-audited against the layer rules
  in this pass.
