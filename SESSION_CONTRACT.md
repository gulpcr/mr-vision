# MR Computer Vision Platform — Session Contract & Implementation Guide

> **Paste this entire document at the start of every new implementation session.**
> It is a binding contract. Every rule here reflects the real, verified state of the codebase.
> Violating any rule risks breaking a production clinical AI pipeline.

---

## 0. Self-Research Mandate (Run Before Writing Any Code)

Before touching a single file, execute this research sequence in order:

```
1. Glob the area you intend to change to confirm file paths exist.
2. Read every file you plan to modify — not skim, READ.
3. Grep for the class/function/pattern you need — it probably already exists.
4. Read the base class or interface that defines the contract.
5. Read backend/app/config.py — check if a feature flag already exists.
6. Read backend/alembic/versions/ — know the last migration number (currently 014).
7. Read backend/app/infrastructure/queue/tasks.py — if your change touches the pipeline.
8. Confirm every import path exists on disk before writing it.
```

**If you are not certain something exists, STOP and read the code first.**

---

## 1. Project Identity

Production-grade clinical AI medical imaging platform.

| Component | Technology |
|-----------|-----------|
| Backend API | FastAPI (async), Python 3.11+ |
| Frontend | Next.js 14, TypeScript, App Router |
| Inference workers | Celery + Redis |
| Database | PostgreSQL 16 + SQLAlchemy (async) |
| Artifact storage | MinIO (S3-compatible) |
| PACS | Orthanc (HTTP :8042, DICOM :4242) |
| Queue broker | Redis db/0 |
| Queue results | Redis db/1 |

**Domain rule**: Every output can affect a patient diagnosis. Correctness is non-negotiable.

---

## 2. Verified Directory Structure

All paths confirmed by Glob. Never invent a path — verify first.

```
backend/
  app/
    config.py                          ← ALL feature flags / settings
    main.py                            ← FastAPI app, router registration
    domain/
      enums.py                         ← JobStatus, QAFlag, BodyPart, AuditAction, UserRole…
      models.py                        ← Pure Python dataclasses (Study, Series, JobRun, Result…)
      interfaces.py                    ← Abstract ABC contracts (PACSClient, repositories…)
    application/
      usecase_registry.py
      job_orchestrator.py
      study_service.py
      routing_service.py
      auth_service.py
      qa_service.py
      result_service.py
      report_service.py
      alerting_service.py              ← CriticalAlert alerting + escalation
      active_learning_service.py       ← Confidence-gated review queue
      retention_service.py
      ensemble_service.py
      ab_testing_service.py
      model_registry.py
      gpu_scheduler.py
      batch_service.py
      cpt_service.py
      portal_service.py
      analytics_service.py
      llm_report_service.py            ← Phase 1: Gemini narrative impression
      vlm_qa_service.py                ← Phase 2: VLM image quality assessment
      cds_service.py                   ← Phase 3: Clinical Decision Support
      longitudinal_service.py          ← Phase 4: Longitudinal trend analysis
    infrastructure/
      database/
        models.py                      ← ALL ORM table definitions (SQLAlchemy)
        session.py                     ← async_session_factory
      orthanc/
        client.py                      ← OrthancPACSClient (implements PACSClient)
      storage/
        client.py                      ← MinIOArtifactStore (implements ArtifactStore)
      dicomweb/
        client.py                      ← DICOMwebClient
      queue/
        celery_app.py                  ← Celery config + Beat schedule (3 tasks)
        tasks.py                       ← run_usecase_pipeline + 3 Beat tasks
      llm/
        gemini_client.py               ← GeminiClient (all LLM/VLM calls)
      metrics.py                       ← Prometheus counters/histograms
    interface/
      api/
        studies.py, jobs.py, results.py, usecases.py
        auth.py, admin.py, health.py
        critical_alerts.py             ← CriticalAlert REST endpoints
        reports.py, ws.py              ← WebSocket
        dependencies.py                ← FastAPI DI helpers
        validators.py
      middleware/
        auth.py                        ← RBACMiddleware
      schemas/
        usecase.py, study.py, job.py   ← Pydantic schemas
    usecases/
      base.py                          ← BasePipeline (MUST inherit)
      brain_mri/pipeline.py            ← Reference implementation (read this first)
      spine_mri/pipeline.py
      chest_mri/pipeline.py
      abdomen_mri/pipeline.py
      pet_ct/pipeline.py               ← _generate_fused_petct_pngs delegates to fused_image_service
      pet_ct_brain/pipeline.py
    services/
      dicom_export_service.py          ← Phase 7: DICOM SR/Seg export
      preview_generator.py             ← MRI overlay PNG generator (non-PET use cases)
      fused_image_service.py           ← PET/CT fused PNG generator; generate_fused_png_bytes()
    dicom/
      sr_generator.py                  ← DICOM Structured Report builder
      seg_generator.py                 ← DICOM Segmentation builder
      worklist_client.py
    fhir/
      client.py                        ← FHIR R4 client
  alembic/
    versions/
      001_initial_schema.py … 013_features.py
      014_critical_alerts.py           ← LATEST migration (down_revision="013")
ui/
  src/
    app/
      study/[uid]/page.tsx             ← OHIF for PET/CT, Stone WebViewer for others
      worklist/page.tsx                ← latestPerUsecase logic; stale badge + cancel button
    components/
      ReportView.tsx                   ← isPetCt → fused endpoint; else → preview endpoint
    lib/
      api.ts                           ← getFusedUrl, getArtifactUrl, getPreviewUrl, api.jobs.cancel
      format.ts
```

---

## 3. Architecture: Four Hard Layers

```
domain/          ← Pure Python dataclasses + enums + abstract ABCs.
                   ZERO framework imports (no FastAPI, SQLAlchemy, Redis).
application/     ← Business logic services. Depend ONLY on domain interfaces.
                   Never import from infrastructure directly.
infrastructure/  ← I/O adapters (PostgreSQL, Redis, MinIO, Orthanc, Gemini).
                   Implements domain interfaces.
interface/       ← FastAPI routers + Pydantic schemas + WebSocket.
                   May import from all layers.
```

### Import Rules (hard constraints — breaking these corrupts the dependency graph)

| Layer | May import from | Must NOT import from |
|-------|----------------|----------------------|
| `domain/` | Python stdlib only | application, infrastructure, interface, FastAPI, SQLAlchemy |
| `application/` | domain | infrastructure (use injected interfaces), interface |
| `infrastructure/` | domain, application | interface |
| `interface/` | all layers | — |

**`app/services/` is shared utility — importable from both `interface/` and `usecases/`.**

---

## 4. Already-Implemented Features (Do Not Re-Implement)

| Phase / Feature | Status | Key Files |
|----------------|--------|-----------|
| Phase 1 — LLM Narrative Report | Done | `application/llm_report_service.py`, `interface/api/reports.py` |
| Phase 2 — VLM Image QA | Done | `application/vlm_qa_service.py`, injected in `tasks.py:0.30` |
| Phase 3 — Clinical Decision Support | Done | `application/cds_service.py`, injected in `tasks.py:0.80` |
| Phase 4 — Longitudinal Analysis | Done | `application/longitudinal_service.py`, injected in `tasks.py:0.83` |
| Phase 5 — DL PET Lesion (SwinUNETR) | Done | `usecases/pet_ct/pipeline.py` |
| Phase 6 — DL Segmentation MRI | Done | All 4 MRI pipelines have `_load_swin_unetr` + `_run_swin_unetr` |
| Phase 7 — DICOM SR/Seg Export | Done | `services/dicom_export_service.py`, injected in `tasks.py:0.90` |
| PET/CT Fused Viewer | Done | `services/fused_image_service.py`, `GET /api/fused/{uid}/{usecase}/{view}`, OHIF viewer |
| Worklist Stale Job Cleanup | Done | `run_stale_job_cleanup` Beat task (10 min interval, 30 min threshold) |
| Job Dispatch Error Handling | Done | `job_orchestrator.py` guards `apply_async`; `jobs.py` surfaces all errors; worklist shows error banner |
| Critical Alerting | Done | `application/alerting_service.py`, `interface/api/critical_alerts.py` |
| RBAC / JWT Auth | Done | `application/auth_service.py`, `interface/middleware/auth.py` |
| Multi-Tenant | Done | `tenant_id` on all ORM tables |
| Active Learning / Review Queue | Done | `application/active_learning_service.py` |
| Retention Policies | Done | `application/retention_service.py` |
| Model Registry | Done | `application/model_registry.py` |
| A/B Testing | Done | `application/ab_testing_service.py` |
| Batch Upload | Done | `application/batch_service.py` |
| FHIR R4 | Done | `fhir/client.py` |
| DICOM Worklist | Done | `dicom/worklist_client.py` |
| Prometheus Metrics | Done | `infrastructure/metrics.py` |
| WebSocket Job Updates | Done | `interface/api/ws.py` |

---

## 5. Database State

**Latest migration: `014_critical_alerts.py` (`down_revision="013"`)**

The next new migration must be:
- File: `backend/alembic/versions/015_<descriptive_name>.py`
- `revision = "015"`, `down_revision = "014"`

### ORM Tables (all in `backend/app/infrastructure/database/models.py`)

| ORM Class | Table Name | Key Columns |
|-----------|-----------|-------------|
| `StudyRecord` | `studies` | `study_instance_uid` (PK), `patient_id`, `tenant_id` |
| `SeriesRecord` | `series` | `series_instance_uid` (PK), `study_instance_uid` (FK) |
| `JobRunRecord` | `job_runs` | `id`, `status`, `progress`, `retry_count`, `tenant_id` |
| `ResultRecord` | `results_index` | `id`, `version`, `is_latest`, `summary`, `measurements`, `qa_flags` |
| `UseCaseRegistryRecord` | `usecase_registry` | `name` (PK), `ensemble_config` |
| `AuditLogRecord` | `audit_log` | `id`, `action`, `entity_type`, `entity_id` |
| `UserRecord` | `users` | `id`, `username`, `email`, `role`, `tenant_id` |
| `TenantRecord` | `tenants` | `id`, `slug` |
| `ModelVersionRecord` | `model_versions` | `id`, `usecase_name`, `version`, `is_active` |
| `ABExperimentRecord` | `ab_experiments` | `id`, `traffic_split` |
| `ABAssignmentRecord` | `ab_assignments` | `experiment_id`, `study_instance_uid` |
| `BatchUploadRecord` | `batch_uploads` | `id`, `status`, `total_items` |
| `BatchUploadItemRecord` | `batch_upload_items` | `id`, `batch_id` |
| `ReviewQueueRecord` | `review_queue` | `id`, `confidence_score`, `status` |
| `AlertRuleRecord` | `alert_rules` | `id`, `event_type`, `webhook_url` |
| `AlertHistoryRecord` | `alert_history` | `id`, `rule_id` |
| `RetentionPolicyRecord` | `retention_policies` | `id`, `max_age_days`, `action` |
| `ShareLinkRecord` | `share_links` | `id`, `token`, `expires_at` |
| `CriticalAlertRecord` | `critical_alerts` | `id`, `severity`, `status`, `escalation_count` |

**Rules:**
- Never alter a table without an Alembic migration.
- Never drop a column or table without explicit user confirmation.
- Never change `is_latest` logic in `_save_result()` without reading it first.
- Async FastAPI handlers: use `async_session_factory` from `infrastructure/database/session.py`.
- Celery tasks: use `_get_sync_session()` (separate sync engine, outside the event loop).

---

## 6. Feature Flags (all in `backend/app/config.py` — `Settings` class)

```python
# LLM Phases 1-4
llm_enabled: bool = False             # Phase 1: narrative report
vlm_qa_enabled: bool = False          # Phase 2: VLM image QA
cds_enabled: bool = False             # Phase 3: clinical decision support
longitudinal_enabled: bool = False    # Phase 4: longitudinal analysis

# Phase 7 DICOM export
dicom_sr_enabled: bool = False
dicom_seg_enabled: bool = False

# Enterprise features
alerting_enabled: bool = False
retention_enabled: bool = False
active_learning_enabled: bool = False

# Data governance
multi_tenant_enabled: bool = False
phi_deidentify_enabled: bool = False

# Auth
auth_mode: str = "jwt"                # "jwt" | "api_key" | "none"

# Other
model_registry_enabled: bool = True
fhir_enabled: bool = False
worklist_enabled: bool = False

# LLM config
gemini_api_key: str = ""
gemini_model: str = "gemini-1.5-flash"
vlm_qa_max_series: int = 3
longitudinal_max_prior_studies: int = 5
```

**When adding a new feature:**
1. Add a typed Pydantic field to `Settings` in `config.py`.
2. Gate the feature on that flag everywhere.
3. Document it in `.env.example`.

---

## 7. Celery Pipeline Lifecycle (Do Not Alter Without Reading `tasks.py`)

The `run_usecase_pipeline` task follows this exact progress sequence:

```
PENDING
→ PREPROCESSING (0.05)  — load pipeline module, init PACS client, build study/series domain objects
→ PREPROCESSING (0.15)  — pipeline.preprocess(study, series, working_dir, pacs, event_loop)
→ [PREPROCESSING (0.30) — VLM Image QA if vlm_qa_enabled && gemini_api_key]
→ INFERRING (0.40)      — pipeline.infer(preprocessed, working_dir)
→ POSTPROCESSING (0.75) — pipeline.postprocess(inference_output, working_dir)
   merge VLM QA flags (non-destructive append)
→ [POSTPROCESSING (0.80) — CDS if cds_enabled && gemini_api_key]
→ [POSTPROCESSING (0.83) — Longitudinal if longitudinal_enabled && gemini_api_key && patient_id]
→ POSTPROCESSING (0.85) — upload artifacts to MinIO, build result_data dict
   _save_result() — mark previous is_latest=False, insert new record
→ [POSTPROCESSING (0.90) — DICOM SR/Seg export if dicom_sr_enabled || dicom_seg_enabled]
→ _run_post_result_hooks() — alerting, active_learning, prior_comparison (all try/except)
→ COMPLETED (1.0)
```

**Critical invariants:**
- `_is_job_cancelled()` is checked before preprocessing AND before inference. Never remove these.
- The worker creates a **fresh** event loop: `loop = asyncio.new_event_loop()`. Never share loops.
- Never use `asyncio.get_event_loop()` in Celery tasks.
- LLM/VLM phases are **non-blocking** — wrapped in `try/except`, log `logger.warning` on failure.
- Post-result hooks are **non-blocking** — hook failures must never propagate to the pipeline result.
- `_save_result()` uses versioning: queries `is_latest=True`, increments version, sets old to False.

### Celery Beat Schedule (3 periodic tasks)

| Task name | Schedule | Queue | Purpose |
|-----------|---------|-------|---------|
| `run_retention_cleanup` | 86 400 s (24 h) | `celery` | Data retention policies |
| `run_critical_alert_escalation` | 300 s (5 min) | `celery` | Escalate unacked CRITICAL alerts |
| `run_stale_job_cleanup` | 600 s (10 min) | `celery` | Expire jobs stuck in active states >30 min |

`run_stale_job_cleanup` marks any `JobRunRecord` with status in `{pending, routing, preprocessing, inferring, postprocessing}` and `updated_at` older than 30 minutes as `FAILED`. This clears phantom "in-progress" entries caused by worker crashes or broker message loss.

---

## 8. Use-Case Plugin Contract

Every use case lives at `backend/app/usecases/<name>/` and must contain:

```
manifest.yaml           # name, version, supported_body_parts, required_sequences, model_type
routing_rules.yaml      # body_parts, study_description regex, modality, priority
pipeline.py             # class Pipeline(BasePipeline)
model/inference_config.yaml
outputs_schema.json
ui_schema.json
```

### BasePipeline Contract (`backend/app/usecases/base.py`)

```python
class Pipeline(BasePipeline):
    def preprocess(
        self, study: Study, series: list[Series], working_dir: str,
        pacs: PACSClient, event_loop: Any = None
    ) -> dict[str, Any]: ...

    def infer(self, preprocessed: dict[str, Any], working_dir: str) -> dict[str, Any]: ...

    def postprocess(self, inference_output: dict[str, Any], working_dir: str) -> dict[str, Any]: ...
```

### `postprocess()` Required Return Shape

```python
{
    "summary":        dict,          # human-readable result
    "measurements":   dict,          # quantitative measurements
    "qa_flags":       list[str],     # append-only — never destructively replace
    "qa_details":     dict,
    "model_version":  str,
    "model_checksum": str,
    "artifacts": [
        {
            "name":          str,    # filename (no path separators)
            "artifact_type": str,    # e.g. "segmentation_nifti", "fused_png", "mip_png"
            "local_path":    str,    # absolute path inside working_dir
            "content_type":  str,    # MIME type
        },
    ],
}
```

**Reference implementation: `backend/app/usecases/brain_mri/pipeline.py`**

### PET/CT artifact types (pet_ct pipeline)

| `artifact_type` | File pattern | Served by |
|----------------|-------------|-----------|
| `pet_nifti` | `pet_suv` | `/api/artifacts/…/pet_suv` |
| `segmentation_nifti` | `lesion_mask` | `/api/artifacts/…/lesion_mask` |
| `ct_nifti` | `ct` | `/api/artifacts/…/ct` |
| `fused_png` | `fused_{view}.png` | `/api/fused/{uid}/pet_ct/{view}` (preferred) or `/api/artifacts/…` |
| `mip_png` | `mip_{view}.png` | `/api/artifacts/…/mip_{view}.png` |
| `report_json` | `report` | `/api/artifacts/…/report` |

---

## 9. PET/CT Viewer & Fused Image Patterns

### DICOM viewer selection (study page)

```typescript
// study/[uid]/page.tsx
const hasPetSeries =
  study.series.some((s) => s.modality === "PT") ||
  results.some((r) => r.usecase_name.startsWith("pet_ct"));

const viewerUrl = hasPetSeries
  ? `/ohif/viewer?StudyInstanceUIDs=${uid}`          // OHIF: native PET/CT fusion
  : `/orthanc/stone-webviewer/index.html?study=${uid}`;  // Stone: MRI/CT
```

**Never use Stone WebViewer for PET studies — it lacks PET/CT fusion.** OHIF is configured with DICOMweb from Orthanc at `/ohif/viewer`.

### Fused image endpoint

`GET /api/fused/{study_uid}/{usecase}/{view}` — added to `results.py` router.

1. Tries to serve pre-generated `fused_{view}.png` from MinIO.
2. If absent, loads `pet_suv` and `ct` NIfTIs from MinIO → calls `generate_fused_png_bytes()` → caches result → returns PNG.

This means PET/CT fused images display even for results produced before fused generation was added.

### `fused_image_service.py` — the stable matplotlib wrapper

`backend/app/services/fused_image_service.py` owns all fused PNG generation. **Never call `cm.get_cmap()` directly** — it was deprecated in matplotlib 3.7 and fails silently in some builds. Always go through `_get_cmap()` in the service:

```python
def _get_cmap(name: str):
    import matplotlib
    try:
        return matplotlib.colormaps.get_cmap(name)   # stable since 3.5
    except AttributeError:
        import matplotlib.cm as cm
        return cm.get_cmap(name)                      # fallback only
```

### ReportView rendering split (ReportView.tsx)

```typescript
const PET_USECASES = ["pet_ct", "pet_ct_brain"];
const isPetCt = PET_USECASES.includes(result.usecase_name);

// PET/CT → always show 3 fused views via getFusedUrl() (on-demand endpoint)
// MRI/CT  → show segmentation preview via getPreviewUrl() (existing endpoint)
```

### Frontend URL helpers (api.ts)

```typescript
getFusedUrl(studyUid, usecase, view)   // → /api/fused/{uid}/{usecase}/{view}
getArtifactUrl(studyUid, usecase, artifactName)  // → /api/artifacts/…?redirect=false
getPreviewUrl(studyUid, usecase, view)            // → /api/preview/{uid}/{usecase}/{view}
api.jobs.cancel(jobId)                            // → POST /api/jobs/{id}/cancel
```

---

## 10. LLM Integration Pattern

- All LLM/VLM calls go through `GeminiClient` in `infrastructure/llm/gemini_client.py`.
- New LLM services live in `application/` and accept `GeminiClient` via constructor injection.
- Celery task handles `try/except`; LLM failures log `logger.warning`, never crash the pipeline.
- Results merge into `postprocessed["summary"]` with a **namespaced key** (e.g. `"clinical_context"`, `"longitudinal_analysis"`).
- Never overwrite existing summary keys — use `.setdefault()` then assign the namespaced key.

---

## 11. Worklist Job Status Rules

The worklist derives AI status **only from the latest job per use case**, not all historical jobs. A study with an old `pending` job from a failed attempt AND a newer `completed` job must show `completed`, not `in_progress`.

```typescript
// CORRECT — use latestPerUsecase() before any status check
function studyAIStatus(studyJobs: Job[]): AIStatusFilter {
  const latest = latestPerUsecase(studyJobs);
  if (latest.length === 0) return "not_started";
  if (latest.some((j) => ACTIVE_STATUSES.includes(j.status))) return "in_progress";
  if (latest.some((j) => j.status === "completed")) return "completed";
  if (latest.every((j) => j.status === "failed" || j.status === "cancelled")) return "failed";
  return "not_started";
}

// Stale: active status AND updated_at > 15 min old
function isStale(job: Job): boolean {
  if (!ACTIVE_STATUSES.includes(job.status)) return false;
  return Date.now() - new Date(job.updated_at).getTime() > 15 * 60 * 1000;
}
```

Stale jobs render a red "Stale" badge and an inline Cancel button in the AI Status column. The backend `run_stale_job_cleanup` Beat task cleans them up server-side every 10 minutes (30 min threshold).

### Job Dispatch Contract (`job_orchestrator.py` + `jobs.py`)

`apply_async` **must always be wrapped in try/except** inside `create_jobs_for_study`. If the Celery broker (Redis) is unreachable, `apply_async` raises synchronously. Without the guard, the `JobRunRecord` is saved to the DB with `PENDING` status but the Celery task is never queued — the job sits `PENDING` forever and the user sees no error.

```python
# CORRECT — in job_orchestrator.py
try:
    run_usecase_pipeline.apply_async(args=[...], task_id=job.id, priority=priority)
except Exception as exc:
    job.status = JobStatus.FAILED
    job.error_detail = f"Failed to queue task: {exc}"
    job.completed_at = datetime.now(timezone.utc)
    await self._job_repo.update(job)
    raise ValueError(f"Job could not be dispatched: {exc}") from exc
```

The `POST /api/studies/{uid}/jobs` handler catches **both** `ValueError` (→ 400) and bare `Exception` (→ 500). An empty `jobs` list (no routing match) is also a 400 with a descriptive hint about routing rules — not a silent 201 with zero jobs.

The frontend `handleRunAI` sets a `jobError` state on failure and renders a dismissible red banner below the stat cards with the exact API error message. Failed jobs show their `error_detail` inline (first line, 60-char truncation with full text on `title` hover).

**Diagnostic checklist when a job stays PENDING:**
1. Check the red error banner — did the dispatch fail immediately?
2. Check if the Celery worker container is running (`docker ps`)
3. Check if the worker is consuming from `mri_inference` queue (`docker logs worker`)
4. After 30 min, `run_stale_job_cleanup` will flip it to `FAILED` with an expiry message
5. Read `error_detail` on the failed job — it captures the root cause

---

## 12. Domains and Enums (verified — `backend/app/domain/enums.py`)

```python
class JobStatus(str, enum.Enum):
    PENDING, ROUTING, PREPROCESSING, INFERRING, POSTPROCESSING, COMPLETED, FAILED, CANCELLED

class QAFlag(str, enum.Enum):
    # Metadata-based
    MISSING_SEQUENCE, SPACING_INCONSISTENCY, MOTION_ARTIFACT, LOW_RESOLUTION,
    INCOMPLETE_COVERAGE, SLICE_GAP
    # VLM-detected (Phase 2)
    LOW_SNR, FIELD_INHOMOGENEITY, ALIASING_ARTIFACT, SUSCEPTIBILITY_ARTIFACT,
    TRUNCATION_ARTIFACT, CHEMICAL_SHIFT_ARTIFACT, PARALLEL_IMAGING_ARTIFACT

class BodyPart(str, enum.Enum):
    BRAIN, HEAD, SPINE, CSPINE, TSPINE, LSPINE, KNEE, SHOULDER, ABDOMEN, PELVIS

class AuditAction(str, enum.Enum):
    STUDY_RECEIVED, JOB_CREATED, JOB_STARTED, JOB_COMPLETED, JOB_FAILED,
    JOB_CANCELLED, JOB_RETRIED, RESULT_STORED, RESULT_VIEWED, CONFIG_CHANGED,
    USER_LOGIN, USER_LOGOUT, USER_CREATED, REPORT_GENERATED, BATCH_STARTED,
    BATCH_COMPLETED, REVIEW_SUBMITTED, ALERT_TRIGGERED, DATA_PURGED, PHI_DEIDENTIFIED

class UserRole(str, enum.Enum):
    ADMIN, RADIOLOGIST, TECHNICIAN, VIEWER
```

---

## 13. Registered FastAPI Routers (verified — `backend/app/main.py`)

```python
app.include_router(health_router)                              # /health
app.include_router(auth_router,            prefix="/api")     # /api/auth
app.include_router(studies_router,         prefix="/api")     # /api/studies
app.include_router(jobs_router,            prefix="/api")     # /api/jobs
app.include_router(results_router,         prefix="/api")     # /api/results
                                                              #   incl. /api/fused/{uid}/{uc}/{view}
                                                              #   incl. /api/preview/{uid}/{uc}/{view}
                                                              #   incl. /api/artifacts/{uid}/{uc}/{path}
app.include_router(usecases_router,        prefix="/api")     # /api/usecases
app.include_router(admin_router,           prefix="/api")     # /api/admin
app.include_router(orthanc_router,         prefix="/api")     # /api/orthanc
app.include_router(reports_router,         prefix="/api")     # /api/reports
app.include_router(critical_alerts_router, prefix="/api")     # /api/critical-alerts
app.include_router(ws_router)                                  # /ws
```

When adding a new router:
1. Create the module in `backend/app/interface/api/<name>.py`.
2. Import and `include_router` in `main.py`.
3. Never put business logic in the router — it belongs in `application/`.

---

## 14. Coding Standards

- Python 3.11+, line length 100 (black/ruff).
- `from __future__ import annotations` in every new Python file.
- Use `structlog.get_logger(__name__)` — never `logging.getLogger`.
- All new settings in `config.py` as typed Pydantic fields with defaults.
- All new DB columns need an Alembic migration (next: `015_...`).
- All exceptions in post-pipeline hooks must be caught and logged as `logger.warning`.
- Frontend API calls go through `ui/src/lib/api.ts` — never raw `fetch()` in components.
- Do not add new npm packages without checking existing utilities first.
- **Never call `matplotlib.cm.get_cmap()` directly** — use `_get_cmap()` from `fused_image_service.py` or `matplotlib.colormaps.get_cmap()` directly.

---

## 15. Hard Anti-Patterns (Never Do These)

| Anti-Pattern | Why |
|-------------|-----|
| Invent a file path | Hallucinated paths cause runtime failures |
| Invent a method on an existing class | Read the class first |
| Drop a column/table without user confirmation | Data loss |
| Alter `is_latest` logic without reading `_save_result()` | Breaks result versioning |
| Synchronous blocking I/O inside `async def` FastAPI handler | Blocks the event loop |
| Bypass `BasePipeline` | All use cases must subclass it |
| Business logic inside FastAPI routers | Belongs in `application/` |
| Hardcode credentials or secrets | Always read from `Settings` |
| Import between use-case plugins | Each plugin is self-contained |
| `asyncio.get_event_loop()` in Celery tasks | Always `asyncio.new_event_loop()` |
| Destructively replace `qa_flags` | Append-only — preserve existing flags |
| Skip audit trail for state-changing ops | Use `AuditLogRecord` |
| Overwrite existing `summary` keys | Use namespaced keys |
| LLM phase crashes the pipeline | Wrap in `try/except`, log `logger.warning` |
| `cm.get_cmap()` for matplotlib colormaps | Deprecated/broken; use `_get_cmap()` from `fused_image_service` |
| Stone WebViewer for PET studies | No fusion support; use OHIF (`/ohif/viewer?StudyInstanceUIDs=…`) |
| `studyAIStatus()` on all historical jobs | Must call `latestPerUsecase()` first — old pending jobs corrupt status |
| Counting all `jobs.flat()` in stat cards | Use `latestPerUsecase` per study — avoids inflating "In Progress" count |
| Uncaught `apply_async` in orchestrator | If Redis is down, job saves as PENDING but task never queues — phantom forever; guard it |
| Only catching `ValueError` in job handler | Other exceptions (broker errors) become silent 500s; catch `Exception` too |
| Silent `console.error` on job creation failure | User has no idea dispatch failed and keeps seeing stale PENDING — show error banner |

---

## 16. Pre-Implementation Checklist

Copy this into your working notes for every task:

```
[ ] Read every file I intend to modify
[ ] Grepped for existing similar code that can be reused or extended
[ ] Read the base class / interface that defines the contract
[ ] Confirmed all import paths exist on disk with Glob/Read
[ ] Checked config.py for existing feature flags covering this feature
[ ] If new feature flag needed: added to Settings with typed default
[ ] Checked alembic/versions/ — next migration is 015_...
[ ] Read tasks.py if the change affects pipeline execution order
[ ] Verified no layer boundary violations in the planned design
[ ] Confirmed postprocess() returns all required keys
[ ] Confirmed LLM phases are non-blocking (try/except + logger.warning)
[ ] Confirmed qa_flags are append-only (not replaced)
[ ] Confirmed new router is registered in main.py
[ ] Confirmed new DB columns have a migration
[ ] No business logic placed inside FastAPI routers
[ ] No raw fetch() in frontend components
[ ] No asyncio.get_event_loop() in Celery tasks
[ ] PET/CT viewer uses OHIF, not Stone WebViewer
[ ] Fused images use generate_fused_png_bytes() / _get_cmap(), not cm.get_cmap()
[ ] Worklist AI status derived from latestPerUsecase(), not all historical jobs
```

---

## 17. Docker Services Reference

```
postgres:16-alpine   :5432   ← metadata DB
redis:7-alpine       :6379   ← Celery broker (db/0) + result backend (db/1)
minio                :9000   ← S3-compatible artifact storage
orthanc              :8042   ← PACS HTTP, :4242 DICOM
backend              :8000   ← FastAPI (runs Alembic migrations on startup)
worker               GPU     ← Celery inference worker (NVIDIA runtime)
beat                 —       ← Celery Beat (retention + alert escalation + stale cleanup)
ohif                 —       ← DICOM viewer (PET/CT fusion via DICOMweb from Orthanc)
ui                   :3000   ← Next.js
nginx                :80     ← Reverse proxy\
```

GPU: `docker-compose.yml`. CPU-only: `docker-compose.cpu.yml`.
Model weights: `model_bundles` named volume, auto-downloaded via MONAI bundle on first run.

---

*Contract version: 2026-06-06 (rev 2). Reflects migration 014, Phases 1–7 + PET/CT fused viewer + stale job cleanup implemented.*


