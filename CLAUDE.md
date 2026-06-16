# MR Computer Vision Platform — Claude Code Guide

## Project Identity

Production-grade AI medical imaging analysis platform. FastAPI backend + Next.js 14 frontend,
Celery/Redis async inference workers, PostgreSQL + MinIO storage, Orthanc PACS integration.
Six active use-case plugins (brain_mri, spine_mri, chest_mri, abdomen_mri, pet_ct, pet_ct_brain).

**Domain**: Clinical radiology AI. Every output is potentially patient-affecting. Correctness and
data integrity are non-negotiable.

---

## Mandatory Pre-Implementation Protocol

Before writing a single line of code for any non-trivial change:

1. **Read the affected files first.** Use Read or Grep to locate actual code — never assume an
   interface, method signature, or import path.
2. **Grep for existing implementations.** The pattern you need likely already exists. Search before
   creating.
3. **Read the base class / interface.** If extending a service or pipeline, read
   `backend/app/usecases/base.py` and the relevant `domain/interfaces.py` contract first.
4. **Check the DB schema before touching models.** Read
   `backend/app/infrastructure/database/models.py` and
   `backend/alembic/versions/` before any SQLAlchemy change.
5. **Check `config.py` for feature flags.** All optional features are gated by `Settings` flags.
   Add a new flag there before using it anywhere else.
6. **Verify imports are real.** Never import a module without first confirming the file exists
   (`Glob`/`Read`). Hallucinated imports cause runtime failures, not type errors.

**If you are unsure whether something exists — stop and read the code.**

---

## Architecture: Four Strict Layers (Clean / Hexagonal)

```
domain/          ← Pure Python dataclasses + enums + abstract interfaces. ZERO framework imports.
application/     ← Business logic services. Depend only on domain interfaces.
infrastructure/  ← I/O adapters (PostgreSQL, Redis, MinIO, Orthanc, Gemini). Implement domain interfaces.
interface/       ← FastAPI routers + Pydantic schemas + WebSocket. Entry points only.
```

### Layer Rules (hard constraints)

| Layer | May import from | Must NOT import from |
|-------|----------------|----------------------|
| `domain/` | stdlib only | application, infrastructure, interface, FastAPI, SQLAlchemy |
| `application/` | domain | infrastructure (use injected interfaces), interface |
| `infrastructure/` | domain, application | interface |
| `interface/` | all layers | — |

Breaking these rules corrupts the dependency graph. If you need something from a lower layer in a
higher layer, introduce an interface in `domain/interfaces.py` and inject it.

### Use-Case Plugin System

Each use case lives in `backend/app/usecases/<name>/` and MUST contain:

```
manifest.yaml          # name, version, supported_body_parts, required_sequences, model_type
routing_rules.yaml     # auto-routing: body_parts, study_description regex, modality, priority
pipeline.py            # class Pipeline(BasePipeline) with preprocess/infer/postprocess
model/inference_config.yaml
outputs_schema.json
ui_schema.json
```

`Pipeline(BasePipeline)` has exactly three required methods:

```python
def preprocess(self, study: Study, series: list[Series], working_dir: str,
               pacs: PACSClient, event_loop: Any = None) -> dict[str, Any]: ...

def infer(self, preprocessed: dict[str, Any], working_dir: str) -> dict[str, Any]: ...

def postprocess(self, inference_output: dict[str, Any], working_dir: str) -> dict[str, Any]: ...
```

`postprocess()` MUST return a dict with these keys:
`summary`, `measurements`, `qa_flags`, `qa_details`, `model_version`, `model_checksum`, `artifacts`

Each artifact MUST have: `name`, `artifact_type`, `local_path`, `content_type`.

The Celery task (`infrastructure/queue/tasks.py:run_usecase_pipeline`) drives the full pipeline and
then fires post-result hooks (alerting, active learning, DICOM export). Do not replicate that logic
inside a pipeline.

---

## Key Files — Read Before Touching

| What you want to change | Read this first |
|------------------------|-----------------|
| Domain entities | `backend/app/domain/models.py` |
| Enums (JobStatus, QAFlag, BodyPart) | `backend/app/domain/enums.py` |
| Abstract contracts (PACSClient, etc.) | `backend/app/domain/interfaces.py` |
| DB ORM tables | `backend/app/infrastructure/database/models.py` |
| DB async session factory | `backend/app/infrastructure/database/session.py` |
| DB migrations | `backend/alembic/versions/` (all files) |
| Feature flags / settings | `backend/app/config.py` |
| Celery task (inference lifecycle) | `backend/app/infrastructure/queue/tasks.py` |
| Celery config + Beat schedule | `backend/app/infrastructure/queue/celery_app.py` |
| Any existing use case | `backend/app/usecases/<name>/pipeline.py` |
| Base pipeline contract | `backend/app/usecases/base.py` |
| Orthanc HTTP client | `backend/app/infrastructure/orthanc/client.py` |
| MinIO artifact store | `backend/app/infrastructure/storage/client.py` |
| Gemini LLM client | `backend/app/infrastructure/llm/gemini_client.py` |
| FastAPI app + router registration | `backend/app/main.py` |
| Critical alerts REST API | `backend/app/interface/api/critical_alerts.py` |
| All REST routers | `backend/app/interface/api/` |

---

## Celery Task Lifecycle (Do Not Break)

The `run_usecase_pipeline` task follows this exact progress sequence:

```
PENDING → PREPROCESSING (0.05) → PREPROCESSING (0.15) → [VLM QA 0.30] →
INFERRING (0.40) → POSTPROCESSING (0.75) → [CDS 0.80] → [Longitudinal 0.83] →
POSTPROCESSING (0.85, artifact storage) → [DICOM export 0.90] →
post-result hooks (alerting, active learning, prior comparison) → COMPLETED (1.0)
```

- JobStatus enum values: `PENDING ROUTING PREPROCESSING INFERRING POSTPROCESSING COMPLETED FAILED CANCELLED`
- Progress is a float 0.0–1.0 stored on `JobRunRecord`
- The worker creates a **new** asyncio event loop per task (`asyncio.new_event_loop()`). Do not
  share event loops between tasks.
- `_is_job_cancelled()` is checked before preprocessing and before inference. Preserve these checks.

---

## Database Rules

- All ORM models are in `backend/app/infrastructure/database/models.py`.
- Every schema change needs an Alembic migration. Never alter tables without one.
- The async session is `async_session_factory` from `infrastructure/database/session.py`.
- Celery tasks use a **synchronous** `Session` via `_get_sync_session()` (separate engine) because
  Celery workers run outside the FastAPI event loop.
- Result versioning: `_save_result()` marks the previous latest `is_latest=False` before inserting.
  Preserve this when touching result persistence.

---

## Configuration / Feature Flags

All runtime feature flags live in `backend/app/config.py` (`Settings` class):

```
llm_enabled, vlm_qa_enabled, cds_enabled, longitudinal_enabled   # LLM phases 1-4
dicom_sr_enabled, dicom_seg_enabled                               # Phase 7 DICOM export
alerting_enabled, retention_enabled, active_learning_enabled      # Enterprise features
multi_tenant_enabled, phi_deidentify_enabled                      # Data governance
auth_mode  ("jwt" | "api_key" | "none")                          # Authentication
```

Adding a new feature: add a typed field to `Settings`, gate the feature on that flag, document it
in `.env.example`.

---

## LLM Integration Pattern (Phases 1–4)

All LLM/VLM calls go through `GeminiClient` in `infrastructure/llm/gemini_client.py`. New LLM
services live in `application/` and accept `GeminiClient` via constructor injection. The calling
code (Celery task) handles `try/except` and logs warnings on failure — LLM features are
non-blocking; they must never crash the pipeline.

Results from LLM services are merged into `postprocessed["summary"]` with a namespaced key
(e.g., `"clinical_context"`, `"longitudinal_analysis"`). Do not overwrite existing summary keys.

---

## Frontend (Next.js 14 + TypeScript)

Entry: `ui/src/`. API calls go through `ui/src/lib/api.ts`. Components are in `ui/src/components/`.
Pages follow the App Router convention (`ui/src/app/`).

Do not add raw `fetch()` calls in components — use the existing API module. Do not add new npm
packages without checking whether an equivalent utility already exists in the project.

---

## Anti-Patterns — Never Do These

- **Never invent a file path.** If you're not sure a file exists, `Glob` for it.
- **Never invent a method on an existing class.** Read the class first.
- **Never write a migration that drops a column or table** without explicit user confirmation.
- **Never change `is_latest` logic** in result versioning without reading `_save_result()` first.
- **Never add synchronous blocking I/O** inside an `async def` FastAPI handler.
- **Never bypass `BasePipeline`** — all use cases must subclass it.
- **Never add business logic** (routing decisions, result computation) inside FastAPI routers; that
  belongs in `application/` services.
- **Never hardcode credentials or secrets** — always read from `Settings`.
- **Never import between use-case plugins** — each plugin is self-contained.
- **Never run `asyncio.get_event_loop()`** in Celery tasks — always create a fresh loop with
  `asyncio.new_event_loop()`.
- **Never mutate `postprocessed["qa_flags"]` destructively** — append only, preserving existing flags.
- **Never skip the audit trail** for state-changing operations — use `AuditLogRecord`.

---

## Coding Standards

- Python 3.11+, line length 100 (black/ruff).
- Use `structlog.get_logger(__name__)` — not `logging.getLogger`.
- All new settings/config in `app/config.py` as typed Pydantic fields.
- All new DB columns need a corresponding Alembic migration.
- Async FastAPI handlers use `async_session_factory` for DB access.
- Celery tasks use the sync `_get_sync_session()` helper.
- All exceptions in post-pipeline hooks must be caught and logged as `logger.warning` — never let
  hook failures propagate to the pipeline result.
- Use `from __future__ import annotations` in all new Python files.

---

## Research Checklist Before Implementing Anything

```
[ ] Read the file(s) I intend to modify
[ ] Grepped for existing similar code that can be reused or extended
[ ] Read the base class / interface that defines the contract
[ ] Confirmed all import paths exist on disk
[ ] Checked config.py for existing feature flags covering this feature
[ ] Checked alembic/versions/ if DB schema change is involved
[ ] Read the Celery task if the change affects pipeline execution
[ ] Verified no layer boundary violations in the planned design
```

---

## Docker & Deployment Context

```yaml
# Services (docker-compose.yml)
postgres:16-alpine   # metadata DB
redis:7-alpine       # Celery broker (db 0) + result backend (db 1)
minio                # S3-compatible artifact storage
orthanc              # PACS server (HTTP :8042, DICOM :4242)
backend              # FastAPI on :8000, runs DB migrations on startup
worker               # Celery GPU worker (NVIDIA runtime)
beat                 # Celery Beat (retention cleanup + alert escalation)
ohif                 # DICOM viewer
ui                   # Next.js :3000
nginx                # Reverse proxy :80
```

GPU inference uses `docker-compose.yml` (NVIDIA runtime). CPU-only override: `docker-compose.cpu.yml`.
Model weights are in the `model_bundles` named volume, auto-downloaded on first run via MONAI bundle.
