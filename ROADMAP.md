# MR Computer Vision Platform — Product & Engineering Roadmap

**Owner:** Platform team · **Created:** 2026-06-30 · **Status:** Living document
**Source of truth for:** outstanding ("to-be-completed") work, sequenced and trackable.

> This is the **forward backlog**. For historical bug findings see `AUDIT_REPORT.md`
> (2026-05-24, much already remediated). For the architecture contract see `CLAUDE.md`
> and `SESSION_CONTRACT.md`.

---

## 1. How to read & track this roadmap

Every line of work is a **story** with a stable ID (`SEC-03`, `PERF-01`, …). A story is the unit
that gets a branch, a PR, and a checkbox. Stories are deliberately small (≤ ~3 dev-days) so each is
independently **shippable**.

Each story card carries:

| Field | Meaning |
|-------|---------|
| **Goal** | The outcome in one sentence (the "why") |
| **Build** | Concrete dev steps with real file paths to touch |
| **Done when** | Acceptance criteria — binary, testable |
| **Metric** | The number that proves it works in production |
| **Size** | S ≤1d · M ≤3d · L ≤1wk (split L's before starting) |
| **Pri** | P0 blocker · P1 must-have · P2 should-have · P3 nice-to-have |
| **Deps** | Story IDs that must land first |

**Definition of Done (applies to every story):** code + unit test + (if applicable) Alembic
migration + feature flag in `config.py` + `.env.example` entry + green CI + the **Metric** wired to
a log/dashboard. No story is "done" until its metric is observable.

**Tracking:** copy the master table in §9 into your tracker (Jira/Linear/GitHub Projects). The
checkbox column is the single source of progress truth.

---

## 2. Business context & process flow

**What it is:** A clinical radiology AI platform. Imaging studies arrive from PACS, get auto-routed
to the right AI use-case, run async GPU inference, and produce structured results + narrative reports
that radiologists review, sign, and share. Every output is potentially patient-affecting →
**correctness, auditability, and data integrity are non-negotiable.**

**Primary actors:** Receptionist (intake), Technician (upload/run), Radiologist (read/sign),
Admin (config/users), Referring physician (read-only portal).

**Core process flow (today):**

```
Patient intake (order, clinical history)        ← receptionist  [/onboarding]
        │
DICOM ingest from Orthanc PACS                  ← technician/auto
        │
Auto-routing  (modality + region match)         ← RoutingService
        │
Async pipeline (Celery GPU worker):
   preprocess → [VLM QA] → infer → postprocess
   → [CDS] → [longitudinal] → artifacts → [DICOM SR/Seg export]
        │
Result versioning + critical-alert hooks        ← tasks.py
        │
Reading workflow: claim → report → sign         ← radiologist  [/worklist, /review]
        │
Report (PDF / FHIR) + referring-physician portal share
```

**Where it makes/loses money & trust:**
- **Throughput** of studies-to-signed-report (TAT) is the core KPI.
- **Diagnostic correctness** of the AI is the trust anchor (some pipelines are still heuristic/stubbed).
- **Compliance** (HIPAA/PHI, audit, access control) gates whether it can be deployed clinically at all.

---

## 3. Tech stack (as-built)

| Layer | Tech |
|-------|------|
| Frontend | Next.js 14 (App Router), React 18, TypeScript, Tailwind, SWR, Cornerstone/WADO, OHIF 3 |
| Backend | FastAPI, Python 3.11, Clean/Hexagonal layers (domain/application/infrastructure/interface) |
| Async | Celery + Redis (broker db0 / result db1), Celery Beat |
| Data | PostgreSQL 16 (SQLAlchemy + Alembic, 24 migrations), MinIO (S3 artifacts) |
| Imaging | Orthanc PACS (DICOMweb QIDO/WADO), DICOM SR/Seg export |
| AI/ML | MONAI (BraTS SegResNet), TotalSegmentator (nnU-Net), PERCIST, AAL3 atlas, GMIC (stub), SwinUNETR (optional) |
| LLM | Gemini (report gen, VLM QA, CDS, longitudinal) — all flag-gated |
| Infra | Docker Compose (10 services), nginx reverse proxy, NVIDIA runtime |

---

## 4. Current-state maturity scorecard

Grounded in code inventory (2026-06-30). Drives prioritisation.

| Domain | Grade | One-line status |
|--------|:-----:|-----------------|
| Core pipeline lifecycle | 🟢 B+ | Solid: routing, versioning, cancellation, progress, hooks |
| AI model coverage | 🟡 C+ | 5 real, 2 mixed/atlas, **coronary stubbed, mammography non-diagnostic** |
| User & identity mgmt | 🟡 C | RBAC + JWT work; **no reset/MFA/lockout/refresh/complexity** |
| Multi-tenancy | 🟠 D+ | Columns + per-tenant roles exist; **enforcement partial, MinIO not isolated** |
| Security & compliance | 🟠 D | **No rate-limit, no TLS in-app, no encryption-at-rest, audit not comprehensive, seed admin/admin123** |
| Performance & scale | 🟡 C | GPU queues + retry; **no task timeout, no batch, no autoscale, no caching** |
| Observability / ops | 🟠 D | structlog only; **no metrics/tracing/alerting/backup/restart policies/CI/CD** |
| Testing & quality | 🟠 D+ | Backend pytest exists; **no frontend tests, no coverage gate, no CI** |
| Frontend UX | 🟡 C | Rich screens; **no loading/error states, a11y, i18n, responsive** |

---

## 5. Roadmap structure — Epics

| Epic | Code | Theme | Why |
|------|------|-------|-----|
| Security & Compliance | **SEC** | HIPAA-grade access, PHI, audit, secrets | Deployment blocker |
| User & Identity Mgmt | **UM** | Reset, MFA, lockout, lifecycle | Clinical access control |
| Multi-Tenancy | **MT** | True isolation across tenants | Multi-site/SaaS readiness |
| Performance & Scale | **PERF** | Timeouts, batch, autoscale, caching | Throughput / cost |
| Model Ops & Clinical AI | **ML** | Finish stubs, registry, drift, validation | Trust / correctness |
| Functional Product | **FUNC** | Reporting, viewer, workflow gaps | User value |
| Platform & Observability | **OPS** | TLS, monitoring, backup, CI/CD | Run it in production |
| Quality & Testing | **QA** | Coverage, E2E, perf tests | Ship safely |

Release sequencing in §8.

---

## 6. Epics & stories

> Legend: ☐ not started. Replace with ☑ on merge. File paths are relative to repo root.

### EPIC SEC — Security & Compliance  *(P0 — clinical deployment blocker)*

**SEC-01 · Rotate & remove the seeded super-admin** — `S · P0`
- **Goal:** Eliminate the `admin/admin123` credential shipped in migration 016.
- **Build:** In `backend/alembic/versions/016_roles_rbac.py` seed path, replace fixed password with a value read from `Settings.bootstrap_admin_password` (`config.py`); if unset, generate a random one and log it once. Add a startup check in `backend/app/main.py` that refuses to boot in `auth_mode="jwt"` if a user still has the default hash. Add `force_password_change` boolean column to `users` (new migration).
- **Done when:** Fresh deploy has no known static password; first admin login forces a change.
- **Metric:** 0 accounts with default-hash in `users` (assert in deep-health check).
- **Deps:** —

**SEC-02 · API rate limiting** — `M · P0`
- **Goal:** Block brute-force and abuse on auth + write endpoints.
- **Build:** Add `slowapi` (or a Redis token-bucket middleware) registered in `backend/app/main.py` after CORS. Strictest limits on `POST /api/auth/login`, `/register`. Add `rate_limit_*` fields to `config.py`. Return `429` with `Retry-After`.
- **Done when:** >N logins/min from one IP returns 429; limits configurable per route class.
- **Metric:** Count of 429s emitted/day; login attempts/IP p99.
- **Deps:** —

**SEC-03 · Account lockout on failed logins** — `S · P0`
- **Goal:** Lock an account after K consecutive failures.
- **Build:** Add `failed_login_count`, `locked_until` to `users` (migration). Enforce in `application/auth_service.py` login path; reset on success. Configurable `auth_max_failed_attempts`, `auth_lockout_minutes`.
- **Done when:** K bad passwords → account locked for the window; audit row written.
- **Metric:** Lockout events/day; ratio of locked vs total accounts.
- **Deps:** SEC-08 (audit) ideally first.

**SEC-04 · Password complexity & change-password** — `S · P1`
- **Goal:** Enforce strong passwords and allow self-service change.
- **Build:** Validator in `interface/api/auth.py` register/change schemas (min length, char classes, breached-list optional). New `POST /api/auth/change-password` (verify current → set new). Reuse `_hash_password` in `auth_service.py`.
- **Done when:** Weak passwords rejected with field errors; users can change own password.
- **Metric:** % of accounts meeting policy; change-password success rate.
- **Deps:** —

**SEC-05 · Secrets management (no plaintext)** — `M · P1`
- **Goal:** Remove plaintext secrets; fail closed on placeholder values.
- **Build:** In `config.py`, mark `secret_key`, `jwt_secret_key`, `phi_hash_salt`, DB/MinIO creds as required with validators that **reject** `changeme_in_production` at startup. Document Docker secrets / external secret store in `docker-compose.yml` (use `secrets:` + `_FILE` env convention). Update `.env.example` with warnings.
- **Done when:** Boot fails fast if any secret is a placeholder; compose reads from secret files.
- **Metric:** Startup secret-validation pass (1/0) surfaced in `/deep-health`.
- **Deps:** —

**SEC-06 · TLS / HTTPS termination** — `M · P1`
- **Goal:** Encrypt traffic in transit end-to-end.
- **Build:** Add a TLS-terminating nginx config (`nginx/nginx.conf`) with cert mounts + HTTP→HTTPS redirect + HSTS; document cert provisioning (Let's Encrypt/companion or corp CA). Set secure/SameSite on any cookies. Add `force_https` guard middleware option.
- **Done when:** Platform served over HTTPS; HTTP redirects; HSTS header present.
- **Metric:** SSL Labs grade ≥ A; % requests over TLS = 100%.
- **Deps:** —

**SEC-07 · Encryption at rest** — `M · P2`
- **Goal:** Protect PHI in DB and object store at rest.
- **Build:** Enable Postgres volume encryption (deployment doc) and MinIO SSE (`storage/client.py` put with server-side-encryption headers; bucket policy). Document key custody.
- **Done when:** New objects stored encrypted; DB volume encrypted in deploy guide.
- **Metric:** % artifacts written with SSE header; DB encryption flag in audit.
- **Deps:** MT-02 (bucket strategy) preferred.

**SEC-08 · Comprehensive audit trail** — `M · P0`
- **Goal:** Every state-changing & PHI-access action is auditable.
- **Build:** `AuditLogRecord` exists (`infrastructure/database/models.py`); add a reusable `audit(action, entity, actor, details)` helper + FastAPI dependency, and call it in all write routers (`auth`, `roles`, `reading`, `results`, `studies`, `onboarding`, `critical_alerts`, `admin`). Include actor + tenant. Add `GET /api/audit` filters (actor, entity, date) — `/admin/audit` page already exists.
- **Done when:** Each write endpoint emits exactly one audit row; admin can filter/export.
- **Metric:** Audit-coverage = (# write endpoints emitting audit / total) = 100%.
- **Deps:** —

**SEC-09 · PHI-safe logging** — `S · P1`
- **Goal:** Ensure no PHI leaks into application logs.
- **Build:** Add a structlog processor in the logging setup that scrubs known PHI keys (reuse tag list from `deidentify/phi_scrubber.py`). Audit existing `logger.*` calls in pipelines for raw PatientName/ID.
- **Done when:** Sampled logs contain zero PHI; processor unit-tested.
- **Metric:** PHI-in-logs scan findings = 0 (CI grep gate).
- **Deps:** —

**SEC-10 · Enable & verify PHI de-identification path** — `S · P2`
- **Goal:** Make `phi_deidentify_enabled` production-verified, not just present.
- **Build:** Add ingest test in `backend/tests/` proving scrubber hashes/removes the 37 tags in `phi_scrubber.py`; document salt rotation. Surface de-ID status per study.
- **Done when:** Test proves tags scrubbed on ingest when flag on.
- **Metric:** % ingested studies with de-ID applied (when enabled).
- **Deps:** —

---

### EPIC UM — User & Identity Management  *(P1)*

**UM-01 · JWT refresh tokens + logout** — `M · P1`
- **Goal:** Short-lived access tokens with refresh, plus real logout.
- **Build:** Issue access (short) + refresh (long) in `auth_service.py`/`interface/api/auth.py`; add `POST /api/auth/refresh` and `POST /api/auth/logout` (refresh-token denylist in Redis). Reduce `jwt_access_token_expire_minutes` from 480. Update `ui/src/lib/api.ts` to refresh on 401 before logging out.
- **Done when:** Expired access token auto-refreshes; logout invalidates refresh.
- **Metric:** Forced-logout (hard 401) rate ↓; refresh success rate.
- **Deps:** —

**UM-02 · Password reset flow** — `M · P1`
- **Goal:** Self-service forgot-password.
- **Build:** `POST /api/auth/forgot` (issue signed, expiring token) + `POST /api/auth/reset`. Token store in Redis. Delivery via existing notification channel or email (gate behind `Settings`). Frontend pages under `ui/src/app/`.
- **Done when:** User resets password via token without admin.
- **Metric:** Reset completion rate; admin-initiated resets ↓.
- **Deps:** UM-01.

**UM-03 · MFA / TOTP** — `M · P2`
- **Goal:** Second factor for privileged roles (admin, radiologist).
- **Build:** Add `pyotp`; `mfa_secret`, `mfa_enabled` on `users` (migration). Enroll + verify endpoints; require at login when enabled. Frontend enrollment in `/settings`.
- **Done when:** Enrolled users must provide TOTP; recovery codes issued.
- **Metric:** MFA adoption % among privileged roles.
- **Deps:** UM-01.

**UM-04 · User lifecycle & profile** — `S · P2`
- **Goal:** Full CRUD beyond current basics (activate/deactivate, edit profile, email).
- **Build:** Extend `interface/api/auth.py` users endpoints (edit email/name, reactivate). Email verification on register (gate). Wire to `/admin/users` page.
- **Done when:** Admin can fully manage a user's lifecycle from the UI.
- **Metric:** Admin user-mgmt actions completed without DB access.
- **Deps:** SEC-08.

**UM-05 · Session/device visibility** — `S · P3`
- **Goal:** Let users see & revoke active sessions.
- **Build:** Track refresh tokens per device in Redis; `GET/DELETE /api/auth/sessions`. Frontend list in `/settings`.
- **Done when:** User revokes a session; that refresh token stops working.
- **Metric:** Sessions revoked/month (security hygiene signal).
- **Deps:** UM-01.

---

### EPIC MT — Multi-Tenancy  *(P1 if SaaS/multi-site, else P2)*

**MT-01 · Tenant-scoped query enforcement (audit + close gaps)** — `M · P1`
- **Goal:** Guarantee every data query filters by `request.state.tenant_id`.
- **Build:** Audit all services in `application/` for `tenant_id` filtering (StudyService, ResultService, JobOrchestrator, ReadingService, OnboardingService, AnalyticsService…). Introduce a shared tenant-scoped session/query helper so filtering can't be forgotten. Add tests asserting cross-tenant reads return empty.
- **Done when:** No endpoint returns another tenant's row; tests prove isolation.
- **Metric:** Cross-tenant leakage tests passing = 100%; 0 prod leakage incidents.
- **Deps:** —

**MT-02 · Tenant-isolated artifact storage** — `M · P1`
- **Goal:** Stop sharing one MinIO bucket across tenants.
- **Build:** In `infrastructure/storage/client.py`, prefix all object keys with `{tenant_id}/` (or per-tenant bucket). Update artifact write in `tasks.py` and presign/get paths. Migration plan for existing objects.
- **Done when:** New artifacts land under tenant prefix; presigned URLs respect tenant.
- **Metric:** % objects under tenant prefix; cross-tenant artifact fetch = 0.
- **Deps:** MT-01.

**MT-03 · Tenant admin & provisioning** — `S · P2`
- **Goal:** Create/manage tenants and their default roles from the UI.
- **Build:** `TenantRecord` exists; add `/api/tenants` CRUD (super-admin only) that seeds the 5 system roles per new tenant. Wire `/admin/sites`.
- **Done when:** Super-admin provisions a tenant; it boots with system roles + bootstrap admin.
- **Metric:** Time-to-provision a new tenant (target < 5 min).
- **Deps:** MT-01, SEC-01.

**MT-04 · Per-tenant config & feature flags** — `M · P2`
- **Goal:** Let flags (LLM, alerting, retention) differ per tenant.
- **Build:** Move tenant-overridable flags from global `config.py` into a `tenant_settings` table; resolver merges global default + tenant override. Cache in Redis.
- **Done when:** Two tenants can run with different enabled use-cases/features.
- **Metric:** # flags resolvable per-tenant; override hit-rate.
- **Deps:** MT-01.

---

### EPIC PERF — Performance & Scale  *(P1)*

**PERF-01 · Celery task soft/hard timeouts** — `S · P0`
- **Goal:** Stop pipelines hanging indefinitely on GPU/PACS ops.
- **Build:** Set `soft_time_limit` + `time_limit` on `run_usecase_pipeline` (`infrastructure/queue/tasks.py` / `celery_app.py`); the existing `SoftTimeLimitExceeded` handler already marks CANCELLED. Make limits configurable per use-case.
- **Done when:** A stuck task is killed at the limit and recorded, not hung.
- **Metric:** Count of timed-out tasks; max task wall-time bounded.
- **Deps:** —

**PERF-02 · GPU concurrency / OOM guard** — `M · P1`
- **Goal:** Prevent two large jobs from exhausting VRAM on one GPU.
- **Build:** Pin `--concurrency=1` per GPU queue or add a GPU semaphore; verify routing in `celery_app.py` gpu_queues. Add VRAM pre-check before inference in `base.py` helper.
- **Done when:** No CUDA OOM under 2 concurrent large studies.
- **Metric:** CUDA OOM count/week = 0; GPU utilisation %.
- **Deps:** PERF-01.

**PERF-03 · Shared model-weight cache / download lock** — `S · P1`
- **Goal:** Stop concurrent workers re-downloading 450 MB weights.
- **Build:** File lock (or Redis lock) around MONAI/TotalSegmentator download in pipelines (`brain_mri` `_download_brats_bundle`, TotalSeg path). Ensure `MONAI_HOME`/weights dirs map to the persistent `model_cache` volume (verify `docker-compose.yml`).
- **Done when:** Weights download once and persist across restarts/replicas.
- **Metric:** Weight (re)downloads/day → ~0 after warm-up; cold-start time ↓.
- **Deps:** —

**PERF-04 · Result/artifact caching & N+1 fix** — `M · P2`
- **Goal:** Cut redundant DB/MinIO round-trips in study list & viewer.
- **Build:** Add `latest_result`/`result_count` to study-list response (`interface/api/studies.py`) to kill the N+1 the frontend does. Cache presigned URLs / rendered slices (FusedViewer already caches 1h) in Redis with TTL.
- **Done when:** Study list is a single query; viewer slice cache hit-rate measurable.
- **Metric:** Study-list p95 latency ↓; cache hit-rate %.
- **Deps:** —

**PERF-05 · Worker autoscaling & queue depth signals** — `M · P2`
- **Goal:** Scale workers to demand instead of fixed concurrency=2.
- **Build:** Expose Celery queue depth + active tasks as metrics (see OPS-02). Document/automate worker replica scaling (KEDA/compose scale) keyed on queue depth.
- **Done when:** Backlog drains by adding workers; scaling driven by queue depth.
- **Metric:** Queue depth p95; time-in-queue p95.
- **Deps:** OPS-02.

**PERF-06 · LLM cost controls (cache + budget + rate-limit)** — `M · P2`
- **Goal:** Bound Gemini token spend; cache deterministic prompts.
- **Build:** In `infrastructure/llm/gemini_client.py` add per-tenant rate limiting, response cache (hash of prompt → result in Redis) for idempotent calls, and a daily token budget counter with a kill-switch. Log token usage per call.
- **Done when:** Repeat prompts hit cache; budget overflow disables LLM phases gracefully (non-blocking).
- **Metric:** Gemini tokens/day, $/study, cache hit-rate.
- **Deps:** —

---

### EPIC ML — Model Ops & Clinical AI  *(P1 — trust/correctness)*

**ML-01 · Finish or quarantine Coronary CTA stenosis** — `L · P1`
- **Goal:** Remove the `NotImplementedError` stenosis path or clearly gate it as non-diagnostic.
- **Build:** In `usecases/coronary_cta/pipeline.py` (`_run_lumen_segmentation`, ~L460–478): either integrate a real lumen/CAD-RADS model (behind a flag + weights in registry) **or** return Agatston-only result with an explicit `non_diagnostic` QA flag and UI banner. Never raise into the pipeline.
- **Done when:** Coronary studies complete with either real grading or a clearly-labelled calcium-only result; no unhandled exception.
- **Metric:** Coronary job failure rate = 0; % flagged non-diagnostic surfaced.
- **Deps:** ML-04 (registry) if loading weights.

**ML-02 · Mammography model: weights or honest placeholder** — `M · P1`
- **Goal:** Stop shipping a non-diagnostic heuristic without a clear label.
- **Build:** In `usecases/mammography/pipeline.py` `infer`, when GMIC weights absent, force a prominent `non_diagnostic` flag into `qa_flags` + summary and reflect it in `MammographyReport.tsx`. Track NYU GMIC weight integration as a follow-up (registry).
- **Done when:** No user can mistake placeholder output for a real read.
- **Metric:** % mammography results carrying explicit diagnostic-status flag = 100%.
- **Deps:** —

**ML-03 · Model drift & performance monitoring** — `M · P2`
- **Goal:** Detect input-distribution drift and output anomalies.
- **Build:** Log per-study input stats (spacing, intensity, shape) + output summary stats per use-case to a metrics store; alert on distribution shift. New lightweight service in `application/`.
- **Done when:** Drift dashboard shows per-use-case input/output trends; alert fires on shift.
- **Metric:** Drift alerts triaged/month; % studies with QA outliers.
- **Deps:** OPS-02, ML-04.

**ML-04 · Wire model registry into inference + versioning truth** — `M · P1`
- **Goal:** Make `ModelRegistryService` authoritative for which weights run.
- **Build:** Have pipelines resolve active version via `application/model_registry.py` (`get_active_version`) instead of self-managing paths; record real `model_version`/`model_checksum` in results (fix the known wrong version-string for TotalSeg pipelines, AUDIT BUG-6). Surface in `/admin/usecases`.
- **Done when:** Result `model_version` always matches the actually-loaded weights.
- **Metric:** % results with registry-verified version/checksum = 100%.
- **Deps:** —

**ML-05 · Active-learning feedback → retraining loop** — `L · P2`
- **Goal:** Turn the manual review queue into a labelled-data pipeline.
- **Build:** Extend `ActiveLearningService` so radiologist corrections in `/review/[id]` persist as ground-truth labels exportable for retraining; track confidence-threshold yield. (Queue + gating already exist.)
- **Done when:** Corrected cases export as a training-ready dataset.
- **Metric:** Labelled cases/week; review-queue throughput.
- **Deps:** ML-04.

**ML-06 · Clinical validation gates per use-case** — `M · P1`
- **Goal:** No use-case is "diagnostic" without a recorded validation result.
- **Build:** Add a `validation_status` (research/validated/non-diagnostic) to `usecase_registry`; block "diagnostic" labelling in UI/reports unless validated. Document evidence per use-case.
- **Done when:** UI/report shows validation status; only validated use-cases marked diagnostic.
- **Metric:** % active use-cases with explicit validation_status.
- **Deps:** ML-04.

---

### EPIC FUNC — Functional Product  *(P1/P2)*

**FUNC-01 · Schema-driven report renderers** — `M · P1`
- **Goal:** Render tables/overlays/supplementary sections from `ui_schema.json` instead of hardcoding.
- **Build:** In `ReportView.tsx`, add generic renderers for `type: table`, `type: overlay`, `supplementary` (per AUDIT §4.1). Normalise the summary field-name drift (`tumorDetected` vs `lesion_detected` vs `organ_segmentation_complete`).
- **Done when:** All 8 use-cases render their schema sections with no per-use-case code.
- **Metric:** # use-cases fully schema-rendered = 8/8.
- **Deps:** —

**FUNC-02 · Frontend loading/error/empty states** — `M · P1`
- **Goal:** Every async screen has spinner, error, and empty states.
- **Build:** Add error boundaries + SWR error/loading handling across `ui/src/app/` pages and key components (worklist, viewer, reports). Standard `<LoadingState>`/`<ErrorState>` components.
- **Done when:** No screen shows a blank/janky state on slow/failed API.
- **Metric:** % pages with all three states (lint/checklist) = 100%.
- **Deps:** —

**FUNC-03 · Accessibility (WCAG 2.1 AA pass)** — `M · P2`
- **Goal:** Keyboard nav, ARIA, contrast for clinical users.
- **Build:** a11y audit (axe) across components; add ARIA labels to custom viewers/forms, focus management, contrast fixes.
- **Done when:** axe reports 0 critical violations on key flows.
- **Metric:** axe critical violations = 0; keyboard-only task completion.
- **Deps:** FUNC-02.

**FUNC-04 · Responsive layout for reading rooms/tablets** — `S · P3`
- **Goal:** Usable below desktop widths.
- **Build:** Tailwind breakpoints on `AppShell`, `Sidebar`, worklist, report views.
- **Done when:** Core flows usable at tablet width.
- **Metric:** Layout-break count at target breakpoints = 0.
- **Deps:** FUNC-02.

**FUNC-05 · Study DELETE / GDPR erase** — `S · P2`
- **Goal:** Compliant deletion of a study + artifacts + results.
- **Build:** `DELETE /api/studies/{uid}` (audited) cascading to results, artifacts (MinIO), and Orthanc instance; permission `study.delete`. (AUDIT §7.1.)
- **Done when:** Deleting a study removes all traces and writes an audit record.
- **Metric:** Erase requests fulfilled within SLA; orphaned-artifact count = 0.
- **Deps:** SEC-08, MT-02.

**FUNC-06 · Notifications hardening (critical alerts e2e)** — `S · P2`
- **Goal:** Reliable delivery + escalation of critical findings.
- **Build:** Verify `critical_alerts` escalation Beat job, channel config, and frontend badge/ack flow end-to-end; add delivery-failure retry + dead-letter.
- **Done when:** A simulated critical finding alerts, escalates, and is acknowledged with audit trail.
- **Metric:** Alert delivery success %; mean time-to-acknowledge.
- **Deps:** SEC-08.

---

### EPIC OPS — Platform & Observability  *(P0/P1 to run in prod)*

**OPS-01 · Health checks + restart policies for all services** — `S · P0`
- **Goal:** Self-healing containers; orchestrator knows true health.
- **Build:** Add healthchecks for backend/worker/beat/minio/orthanc/ui and `restart: unless-stopped` to every service in `docker-compose.yml`. Backend `/health` + `/deep-health` already exist; add a worker liveness ping.
- **Done when:** Every service has a healthcheck + restart policy; a killed service recovers.
- **Metric:** Unhealthy-service auto-recovery rate; uptime %.
- **Deps:** —

**OPS-02 · Metrics & dashboards (Prometheus + Grafana)** — `M · P1`
- **Goal:** Observe latency, queue depth, GPU, error rates.
- **Build:** Add `prometheus-fastapi-instrumentator` to backend; export Celery queue metrics; Grafana dashboards for TAT, queue depth, GPU util, 4xx/5xx. Compose services for prometheus+grafana.
- **Done when:** Dashboards live for the KPIs in §10.
- **Metric:** All §10 KPIs visible on a dashboard.
- **Deps:** OPS-01.

**OPS-03 · Centralized logging + error tracking** — `M · P1`
- **Goal:** Searchable logs + exception alerting.
- **Build:** Ship structlog JSON to a log aggregator (Loki/ELK) and add Sentry (backend + frontend) for exceptions. PHI-scrubbed (SEC-09).
- **Done when:** Logs searchable centrally; unhandled exceptions alert.
- **Metric:** MTTR on incidents; unhandled-exception rate.
- **Deps:** SEC-09.

**OPS-04 · Backup & disaster recovery** — `M · P0`
- **Goal:** Recoverable Postgres + MinIO with defined RPO/RTO.
- **Build:** Scheduled `pg_dump` + MinIO mirror to off-host storage (Beat or external cron); documented restore runbook; periodic restore test.
- **Done when:** A restore drill recovers the platform from backups.
- **Metric:** Backup success rate; tested RTO/RPO met.
- **Deps:** —

**OPS-05 · CI/CD pipeline** — `M · P1`
- **Goal:** Automated lint/test/build/scan on every PR.
- **Build:** GitHub Actions: ruff+black (backend), eslint (frontend), pytest, frontend tests (QA-01), bandit/semgrep, Docker build. Block merge on red.
- **Done when:** PRs require green CI to merge.
- **Metric:** CI pass-rate; mean PR-to-merge time.
- **Deps:** QA-01.

**OPS-06 · Resource limits** — `S · P2`
- **Goal:** Prevent any container starving the host/GPU.
- **Build:** Add CPU/memory limits to all services and GPU constraints to worker in `docker-compose.yml` (AUDIT §6.4).
- **Done when:** Every service has bounded resources.
- **Metric:** OOM-kills/week = 0.
- **Deps:** OPS-01.

---

### EPIC QA — Quality & Testing  *(P1)*

**QA-01 · Frontend test harness** — `M · P1`
- **Goal:** Establish frontend testing (currently zero).
- **Build:** Add Vitest + React Testing Library; smoke-test API client (`lib/api.ts`) and critical components (worklist, ReportView, auth guard). Add `npm test` to CI.
- **Done when:** Frontend tests run in CI with a baseline suite.
- **Metric:** Frontend test count; critical-path coverage %.
- **Deps:** —

**QA-02 · Backend coverage gate** — `S · P1`
- **Goal:** Track and floor backend coverage.
- **Build:** Add `pytest-cov`, publish coverage, set a minimum threshold in CI. Backfill tests for auth/tenant/security stories.
- **Done when:** CI fails below threshold; coverage reported per PR.
- **Metric:** Backend coverage % (target floor agreed by team).
- **Deps:** OPS-05.

**QA-03 · E2E smoke (Playwright)** — `M · P2`
- **Goal:** Guard the intake→infer→report→sign happy path.
- **Build:** Playwright test driving login → upload → run → view result → sign, against a compose test stack.
- **Done when:** E2E happy path green in CI nightly.
- **Metric:** E2E pass-rate; flake rate.
- **Deps:** QA-01, OPS-05.

**QA-04 · Pipeline config validation & hardcoded-label fix** — `M · P2`
- **Goal:** Fail-fast on bad use-case YAML; remove hardcoded label loops (AUDIT §3.1).
- **Build:** Schema-validate each `inference_config.yaml`/manifest at load (`usecases/base.py`); replace hardcoded `[1,2,3..]` loops with `label_map.items()` across pipelines.
- **Done when:** Bad config rejected at load; adding an organ to config is processed automatically.
- **Metric:** Config-validation failures caught at startup vs runtime; silent-skip incidents = 0.
- **Deps:** —

---

## 7. Cross-cutting dependencies (build order hints)

```
SEC-08 (audit) ─┬─> SEC-03, UM-04, FUNC-05, FUNC-06
SEC-09 (PHI logs) ─> OPS-03
UM-01 (refresh) ─┬─> UM-02, UM-03, UM-05
MT-01 (scoping) ─┬─> MT-02 ─> SEC-07
                 └─> MT-03, MT-04
OPS-01 ─> OPS-02 ─> PERF-05, ML-03, OPS-06
QA-01 ─> OPS-05 ─> QA-02, QA-03
ML-04 (registry) ─> ML-01, ML-03, ML-05, ML-06
```

---

## 8. Release plan (milestones)

Sequenced so each milestone is independently deployable. Re-scope per team capacity.

### 🚀 M1 — "Safe to deploy clinically" (P0 hardening)
Blockers that make a clinical deployment defensible.
`SEC-01, SEC-02, SEC-03, SEC-08, PERF-01, OPS-01, OPS-04`
**Exit:** No default creds, brute-force protected, fully audited, no hung jobs, self-healing + recoverable.

### 🔐 M2 — "Access & data integrity"
`SEC-04, SEC-05, SEC-06, SEC-09, UM-01, UM-02, MT-01, ML-04, QA-01, QA-02, OPS-05`
**Exit:** Strong auth + TLS + secrets, tenant query isolation, registry-truthful versions, CI with tests.

### 🧠 M3 — "Trustworthy AI & product polish"
`ML-01, ML-02, ML-06, FUNC-01, FUNC-02, FUNC-05, FUNC-06, MT-02, OPS-02, OPS-03, UM-03`
**Exit:** No silent stubs, validation status everywhere, polished UX, tenant-isolated storage, observability live.

### 📈 M4 — "Scale, cost & continuous improvement"
`PERF-02, PERF-03, PERF-04, PERF-05, PERF-06, ML-03, ML-05, MT-03, MT-04, SEC-07, SEC-10, FUNC-03, FUNC-04, UM-04, UM-05, OPS-06, QA-03, QA-04`
**Exit:** Autoscaling, LLM cost control, drift + retraining loop, per-tenant config, a11y, full test pyramid.

---

## 9. Master tracking table

| ☐ | ID | Story | Epic | Pri | Size | Milestone | Metric |
|---|----|-------|------|:---:|:----:|:---------:|--------|
| ☐ | SEC-01 | Rotate/remove seed admin | SEC | P0 | S | M1 | 0 default-hash accounts |
| ☐ | SEC-02 | API rate limiting | SEC | P0 | M | M1 | 429s/day, login/IP p99 |
| ☐ | SEC-03 | Account lockout | SEC | P0 | S | M1 | Lockout events/day |
| ☐ | SEC-08 | Comprehensive audit trail | SEC | P0 | M | M1 | Audit coverage = 100% |
| ☐ | PERF-01 | Task timeouts | PERF | P0 | S | M1 | Timed-out tasks count |
| ☐ | OPS-01 | Healthchecks + restart | OPS | P0 | S | M1 | Auto-recovery rate |
| ☐ | OPS-04 | Backup & DR | OPS | P0 | M | M1 | Restore drill pass |
| ☐ | SEC-04 | Password policy + change | SEC | P1 | S | M2 | % accounts in policy |
| ☐ | SEC-05 | Secrets management | SEC | P1 | M | M2 | Secret-validation pass |
| ☐ | SEC-06 | TLS/HTTPS | SEC | P1 | M | M2 | SSL grade ≥ A |
| ☐ | SEC-09 | PHI-safe logging | SEC | P1 | S | M2 | PHI-in-logs = 0 |
| ☐ | UM-01 | Refresh tokens + logout | UM | P1 | M | M2 | Hard-401 rate ↓ |
| ☐ | UM-02 | Password reset | UM | P1 | M | M2 | Reset completion rate |
| ☐ | MT-01 | Tenant query enforcement | MT | P1 | M | M2 | Leakage tests 100% |
| ☐ | ML-04 | Registry → inference truth | ML | P1 | M | M2 | Version-verified 100% |
| ☐ | QA-01 | Frontend test harness | QA | P1 | M | M2 | FE test count |
| ☐ | QA-02 | Backend coverage gate | QA | P1 | S | M2 | Coverage % floor |
| ☐ | OPS-05 | CI/CD pipeline | OPS | P1 | M | M2 | CI pass-rate |
| ☐ | ML-01 | Coronary CTA finish/quarantine | ML | P1 | L | M3 | Coronary failure = 0 |
| ☐ | ML-02 | Mammography honest output | ML | P1 | M | M3 | Diag-status flag 100% |
| ☐ | ML-06 | Clinical validation gates | ML | P1 | M | M3 | % with validation_status |
| ☐ | FUNC-01 | Schema-driven renderers | FUNC | P1 | M | M3 | 8/8 use-cases |
| ☐ | FUNC-02 | Loading/error states | FUNC | P1 | M | M3 | % pages w/ states |
| ☐ | FUNC-05 | Study DELETE/GDPR | FUNC | P2 | S | M3 | Orphaned artifacts = 0 |
| ☐ | FUNC-06 | Critical alerts e2e | FUNC | P2 | S | M3 | Delivery success % |
| ☐ | MT-02 | Tenant-isolated storage | MT | P1 | M | M3 | % objects tenant-prefixed |
| ☐ | OPS-02 | Metrics + Grafana | OPS | P1 | M | M3 | KPIs on dashboard |
| ☐ | OPS-03 | Central logging + Sentry | OPS | P1 | M | M3 | MTTR |
| ☐ | UM-03 | MFA/TOTP | UM | P2 | M | M3 | MFA adoption % |
| ☐ | SEC-07 | Encryption at rest | SEC | P2 | M | M4 | % SSE artifacts |
| ☐ | SEC-10 | Verify PHI de-ID | SEC | P2 | S | M4 | % de-ID studies |
| ☐ | UM-04 | User lifecycle/profile | UM | P2 | S | M4 | Admin actions in UI |
| ☐ | UM-05 | Session visibility | UM | P3 | S | M4 | Sessions revoked/mo |
| ☐ | MT-03 | Tenant provisioning | MT | P2 | S | M4 | Time-to-provision |
| ☐ | MT-04 | Per-tenant config | MT | P2 | M | M4 | Flags per-tenant |
| ☐ | PERF-02 | GPU concurrency guard | PERF | P1 | M | M4 | CUDA OOM = 0 |
| ☐ | PERF-03 | Weight cache/lock | PERF | P1 | S | M4 | Redownloads ~0 |
| ☐ | PERF-04 | Result/artifact caching | PERF | P2 | M | M4 | List p95 ↓ |
| ☐ | PERF-05 | Worker autoscaling | PERF | P2 | M | M4 | Queue depth p95 |
| ☐ | PERF-06 | LLM cost controls | PERF | P2 | M | M4 | $/study, cache hit |
| ☐ | ML-03 | Drift monitoring | ML | P2 | M | M4 | Drift alerts/mo |
| ☐ | ML-05 | Active-learning retrain loop | ML | P2 | L | M4 | Labelled cases/wk |
| ☐ | FUNC-03 | Accessibility AA | FUNC | P2 | M | M4 | axe critical = 0 |
| ☐ | FUNC-04 | Responsive layout | FUNC | P3 | S | M4 | Layout breaks = 0 |
| ☐ | OPS-06 | Resource limits | OPS | P2 | S | M4 | OOM-kills = 0 |
| ☐ | QA-03 | E2E smoke (Playwright) | QA | P2 | M | M4 | E2E pass-rate |
| ☐ | QA-04 | Config validation/labels | QA | P2 | M | M4 | Silent-skip = 0 |

**45 stories** · P0: 7 · P1: 17 · P2: 18 · P3: 3.

---

## 10. North-star KPIs (wire these in OPS-02)

| KPI | Definition | Why it matters |
|-----|-----------|----------------|
| **TAT** | Median intake → signed report | Core business throughput |
| **Pipeline success rate** | COMPLETED / (COMPLETED+FAILED) | AI reliability |
| **Time-in-queue p95** | Job submit → start | Capacity health |
| **Diagnostic-status coverage** | % results with explicit validation/diagnostic flag | Clinical trust/safety |
| **Audit coverage** | % write endpoints emitting audit | Compliance |
| **Cross-tenant leakage** | Incidents/tests failing | Isolation guarantee |
| **$/study (LLM+GPU)** | Inference + token cost per study | Unit economics |
| **MTTR** | Mean time to recover from incident | Operability |
| **Uptime** | Service availability % | SLA |

---

*Update protocol: when a story merges, flip its ☐→☑ in §9, note the PR, and confirm its Metric is
live on a dashboard. Add new work as a new story ID under the right epic — never as an untracked TODO.*
