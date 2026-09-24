# Multi-Tenancy, RBAC, Dashboards & HIPAA — Gap Analysis

**Date:** 2026-09-23 · **Branch audited:** `ui` · **Reference system:** `C:\sistems\projects\pulsdesk`
**Method:** static code review only. Nothing was run against a live database or containers.
The most serious findings were re-checked directly in the code.

**Goal:** offer the platform to multiple tenants (hospitals/clinics). Each tenant is isolated
and has its own personalised dashboards and RBAC users (Radiologist, Doctor, Receptionist, etc.).
The target is pulsdesk's level of multi-tenancy, RBAC and dashboard management, and the
platform must meet HIPAA.

---

## 1. Summary

| Area | Status |
|---|---|
| Multi-tenancy | **Partial, about 35% of pulsdesk.** Tenants exist in the database, but isolation is enforced only in application code. Several endpoints leak data across tenants. PACS, object storage and WebSocket are shared and not tenant-aware. Off by default (`multi_tenant_enabled=False`). |
| RBAC | **Thin.** Roles are per tenant, but only 32 of 146 routes check a permission. There are 16 fixed permissions, no role builder UI, and no Doctor role. New tenants get no roles. |
| Dashboards | **None.** A single hard-coded dashboard is shown to every user. |
| Super-admin panel | **Basic.** Tenant CRUD, plan/features, status, API keys, users and impersonation. No monitoring, usage metrics, billing or health views (see §4). |
| HIPAA | **Not compliant.** Six critical blockers, including PHI committed to GitHub, a `"changeme"` JWT secret, unauthenticated DICOMweb, and PHI sent to Gemini without a BAA. |

---

## 2. Current multi-tenancy implementation

The tenancy work was reimplemented on the `ui` branch from `origin/Anas` (commit `bde78c23`,
migrations 034–043).

### 2.1 Data model
- **Isolation strategy:** a row-level `tenant_id` column (shared schema). There is **no** Postgres
  RLS, schema-per-tenant or DB-per-tenant.
- **`TenantRecord`** (`backend/app/infrastructure/database/models.py:468`) has `id`, `name`,
  `slug`, `status` (active/suspended/offboarded), `plan` and `features` (JSON). It has no
  branding and no settings.
- **Tables with `tenant_id` (22):** studies, series, job_runs, results_index, observations,
  conditions, audit_log, users, patients, orders, roles, user_roles, batch_uploads,
  review_queue, alert_rules, retention_policies, share_links, critical_alerts,
  mammography_reports, mri_reports, tenant_api_keys, pending_study_tenants.
- **Global tables (intended):** usecase_registry, model_versions, ab_experiments, plan_features.
- **Global uniqueness problems:**
  - `users.username` and `users.email` are unique across all tenants (`models.py:418-419`).
  - `studies.study_instance_uid` is a global primary key.

### 2.2 Backend enforcement
- **Tenant resolution:**
  - `RBACMiddleware` copies `tenant_id` from the JWT onto `request.state`
    (`interface/middleware/auth.py:124-131`).
  - JWTs are signed with a key derived per tenant (`application/auth_service.py:20-27`).
- **Isolation boundary:** `request_tenant_id()` (`interface/api/dependencies.py:78-86`) is
  injected into `PgStudyRepository`, `PgSeriesRepository`, `PgJobRepository`,
  `PgResultRepository` and `PgAuditRepository`. Their `_scope()` method adds
  `WHERE tenant_id = …`.
- **`TenantResolutionMiddleware`** (`interface/middleware/tenant.py`):
  - Resolves the tenant from subdomain, then `X-Tenant-Slug` header, then `?slug=`.
  - Returns 403 if that tenant disagrees with the JWT.
  - Blocks suspended and offboarded tenants.
  - Does nothing unless `multi_tenant_enabled=True`.
- **Write safety net:** a `before_flush` listener (`infrastructure/database/session.py:41-62`)
  fills in the tenant on new rows. It only runs when the tenant ContextVar is bound.
- **Celery:** propagates the tenant correctly (`infrastructure/queue/tasks.py:446-453`), and
  `_save_result` scopes `is_latest` by tenant.
- **Security features ported:**
  - TOTP MFA with lockout.
  - Impersonation (30 min, revocable through a Redis blocklist, audited).
  - Hashed tenant API keys (DICOM upload only).
  - HMAC hash-chained audit log.

### 2.3 Confirmed cross-tenant leaks

| Endpoint / code | Problem |
|---|---|
| `DELETE /api/studies/{uid}` (`interface/api/studies.py:145-194`) | Raw delete by UID. No tenant filter, no permission check, no audit entry. Also purges MinIO. |
| `GET /api/orthanc/studies` (`studies.py:221`) | Lists every Orthanc study, with patient name and ID, to any logged-in user. |
| `POST /api/studies` → `ingest_study()` (`application/study_service.py:102`) | Returns an existing study from any tenant by UID. |
| Longitudinal analysis (`interface/api/reports.py:211-238`) | Prior results matched by MRN across tenants and sent to the LLM. |
| Reading workflow (`application/reading_service.py:80-89`) | Claim, assign, report and sign work on any tenant's study. |
| Mammography report get/upsert (`application/mammography_service.py:91-118`) | No tenant check. |
| Share links list/revoke (`interface/api/results.py:301-327`) | No tenant check. |
| Artifact download (`results.py:674`) | MinIO key built from the URL. Other tenants' artifacts are reachable by guessing UIDs (IDOR). |
| `PUT /auth/users/{id}/role`, `DELETE /auth/users/{id}` | No tenant check, no role validation, no audit. |
| `/api/admin/routing-rules`, `/site-config`, experiments, model versions, `/retention/apply` | Global settings that any tenant admin can change. |
| `/api/admin/reset` | Deletes audit-log rows, which breaks the global hash chain. |

### 2.4 Shared infrastructure (not tenant-aware)
- **Orthanc:** one PACS. nginx serves `/dicom-web/` and `/wado` straight from it with no
  authentication (`nginx/nginx.conf:195-214`).
- **MinIO:** one bucket. Keys are `{study_uid}/{usecase}/{name}` with no tenant prefix.
- **WebSocket `/ws`:** no authentication. `send_to_tenant()` broadcasts to everyone
  (`interface/api/ws.py:44-47`).
- **Feature entitlements:** `require_usecase_feature` is never used. Only manual job creation
  checks features.

---

## 3. How tenant isolation currently works via the frontend

The short answer is that the frontend does very little. Isolation depends entirely on the JWT the
browser sends.

1. **Login** (`ui/src/app/login/page.tsx`)
   - The user enters only a username and password. There is no workspace/tenant field, subdomain
     or tenant picker.
   - Because usernames are globally unique, the backend finds the user and puts their
     `tenant_id` into the JWT.
2. **Session storage**
   - The browser saves `auth_token` and `user` (`id`, `username`, `role`, `tenant_id`) in
     **`localStorage`**.
3. **Every API call** (`ui/src/lib/api.ts`: `fetchAPI`, `fetchBlob`, `fetchUpload`)
   - Sends only `Authorization: Bearer <token>`.
   - Does **not** send `X-Tenant-Slug`.
   - Does not use subdomains or add any tenant parameter.
   - The backend reads the tenant from the token and the repositories filter by it.
   - The frontend never passes or checks a tenant itself.
4. **Menu visibility** (`ui/src/lib/nav.ts`, `ui/src/components/Sidebar.tsx`)
   - Sidebar items are filtered by `requiredPermission`, using a **hard-coded role→permission
     map** in `ui/src/lib/permissions.ts`.
   - Custom roles or edited role permissions in the DB are therefore not reflected in the UI.
   - There are no page-level route guards, only a few in-page `can()` checks. Typing a URL shows
     the page, and the backend is the only barrier. Because most clinical routes lack permission
     checks, that barrier is often just "logged in".
5. **Platform admin detection** (`ui/src/lib/auth.tsx`)
   - `/auth/me` is called to read `is_platform_admin`, which unlocks the `tenant.manage` nav item
     (Tenants).
6. **Impersonation** (`ui/src/lib/impersonation.ts`)
   - The operator's session is stashed in `localStorage.impersonation_origin`.
   - The token is swapped for the target user's, and a banner is shown (`AppShell.tsx`).
7. **What the frontend does NOT isolate**
   - **OHIF viewer** (`ohif/app-config.js`): fetches `/dicom-web` **without any token**, straight
     from the shared Orthanc. Any user, or anyone who can reach the server, can view any
     tenant's images.
   - **WebSocket:** unauthenticated. Every browser receives every tenant's job, result and alert
     events.
   - **Tenant identity is invisible:** there is no tenant name, logo or branding anywhere in the
     UI. A user cannot tell which tenant they are in, and there is no tenant switcher.
   - **"Invite user" (`ui/src/app/admin/users/page.tsx:58`)** calls the public `/auth/register`,
     so the new user is created in the **Admin/default tenant**, not the inviting admin's tenant.

**Net effect:** for database-backed pages that go through the scoped repositories (worklist,
studies, jobs, results), a user in tenant A sees only tenant A's rows. Images (OHIF/DICOMweb),
live events, artifacts and the leak endpoints in §2.3 are **not** isolated.

---

## 4. Super-admin panel: what exists

**Yes, there is a basic platform-admin console.** It is not a monitoring or control centre.

- **Access:** the `Tenants` sidebar item, visible only when `users.is_platform_admin=true`. The
  seeded `admin` user in the Admin tenant is granted this by migration 043.
- **Backend:** gated by `require_platform_admin` (tenant CRUD, API keys, plan features, audit
  verify, platform-admin grants). Impersonation is gated by `require_platform_operator`.

| Page | Capabilities |
|---|---|
| `/admin/tenants` (`ui/src/app/admin/tenants/page.tsx`) | List tenants (name, slug, status, plan). Create a tenant plus its admin user; shows a one-time temporary password. |
| `/admin/tenants/[id]` (`ui/src/app/admin/tenants/[id]/page.tsx`) | Change plan. Add or remove feature flags. Change status (active/suspended/offboarded). Create or revoke DICOM-upload API keys. List the tenant's users. Grant or revoke platform-admin and operator. **Impersonate** a user. |

Backend endpoints: `GET/POST /api/admin/tenants`, `GET /api/admin/tenants/{id}`,
`PUT …/plan`, `PUT …/features`, `PUT …/status`, `…/api-keys` CRUD, `/api/admin/plan-features`.

**Missing compared with pulsdesk's operator console:**
- **Operator identity:**
  - No separate operator identity. Platform admins are ordinary tenant users with a flag, and
    they share the tenant JWT secret.
  - No operator roles (support/billing/superadmin); there is only an admin/operator flag.
  - No forced password rotation for operators.
- **Monitoring:**
  - No platform overview dashboard: studies/jobs per tenant, active users, storage, GPU usage,
    failures.
  - No tenant usage/quota metrics (studies/month, storage GB, seats).
  - The existing `/admin/metrics` and `/admin/capacity` pages are scoped to the caller's own
    tenant.
  - No ops health view (DB pool, Redis, Celery queues, Orthanc, MinIO, GPU workers).
  - No failed-job retry or purge.
- **Audit:**
  - No cross-tenant audit viewer or export for operators. `/audit/verify` exists, but there is
    no UI for it.
  - No append-only, fail-closed platform audit of operator actions.
- **User operations:** no cross-tenant user search, forced password reset or revoke-sessions.
- **Tenant lifecycle:** no soft or hard delete, and no per-tenant migration or drift tooling
  (not needed with RLS).
- **Commercial:** no billing, invoices, plan catalogue editor, seat limits or announcements.
- **Onboarding:** no self-service signup and no invitation-based tenant onboarding.

---

## 5. Gaps vs. pulsdesk (reference)

**Pulsdesk overview:**
- **Stack:** NestJS + raw `pg`, React/Vite.
- **Isolation:** **schema-per-tenant** (`tenant_<slug>`, switched via `SET search_path`).
- **Tenant resolution:** API token, then custom domain, then subdomain, then `X-Tenant-Slug`
  header, then `?slug=`.
- **Request context:** tenant held in AsyncLocalStorage.

| Capability | Pulsdesk | Ours |
|---|---|---|
| DB-enforced isolation | Schema per tenant | ❌ App-code filter only |
| All endpoints isolated | Yes | ❌ Leaks in §2.3 |
| Storage/PACS/realtime isolation | Signed file URLs + tenant-keyed caches | ❌ Shared and unauthenticated |
| Tenant resolution in UI | Workspace slug at login + `X-Tenant-Slug` header | ❌ JWT only; no tenant in UI |
| Provisioning seeds roles, nav, dashboards, branding | Yes, one transaction | ❌ Tenant + admin only, **no roles** |
| Self-service signup / operator invite | Both | ❌ Operator create with temporary password only |
| Tenant admin invites users | Hashed 72-hour invite + Users UI | ❌ Invite lands in the wrong tenant |
| Custom roles + role builder UI | Yes (clone, deactivate, field-level visibility) | ⚠️ Backend CRUD only |
| Permission model | `resource:action`, wildcards, `:own`, any-of | ⚠️ 16 flat permissions |
| Permission enforcement | Guard on every controller, loaded live from DB | ❌ 32/146 routes |
| DB-driven nav, route guards, action gates | `permissions_config` + live re-sync | ⚠️ Hard-coded map; no route guards |
| Last-admin lockout protection | Yes | ❌ |
| Plans + feature flags + overrides, enforced | Yes | ⚠️ Stored, barely enforced |
| Branding / white-label / custom domain | Yes | ❌ |
| Personalised dashboards | Per user / shared / role default; grid builder; ~30 widgets; filters; versions; alerts; templates; export; SSE refresh | ❌ One static page |
| Operator console | Full (see §4) | ⚠️ Basic |
| Separate operator identity + secret | Yes | ❌ |
| Append-only platform audit | Trigger-enforced, fail-closed | ⚠️ Hash chain, but deletable |
| Sessions | 15-min JWT + refresh + revocation + idle guard | ❌ 8-hour JWT, no refresh or revocation |
| Login lockout, rate limits | Yes | ❌ |
| SSO (SAML/OIDC) | Yes | ❌ |
| Privacy (consent/export/erasure) | GDPR module | ❌ |

**Pulsdesk weaknesses not to copy:**
- It does not compare the JWT's tenant with the resolved tenant. We already do this check.
- Role scope (project/board) is stored but not enforced.
- Changes to roles are not audited.
- Seat quotas are not enforced.

**Recommended isolation approach:** keep `tenant_id` and **add Postgres Row-Level Security**,
setting `SET LOCAL app.tenant_id` on each connection, for both the async API session and the
Celery sync session. This gives DB-enforced isolation equivalent to schema-per-tenant without
rewriting 43 Alembic migrations or the pipeline sessions. For imaging, either run **one Orthanc
per tenant**, or put a single Orthanc behind an authorization proxy that validates the JWT and
the study's tenant on every DICOMweb/WADO request.

---

## 6. HIPAA Security Rule shortcomings

### Critical (deployment blockers)
1. **PHI committed to GitHub (possible reportable breach, 45 CFR 164.402)**
   - Tracked files: `dd/PHOTO-2026-06-03-20-58-39.jpg`, `dd/PHOTO-2026-06-03-20-58-39 (1).jpg`,
     `dd/Screenshot 2026-06-03 215452.png`, `sample_petct_report.pdf`,
     `sample_petct_report.png`.
   - They are on `origin/main` and other branches, and show a PRN, a diagnosis, patient details
     and physician names.
   - Action: purge them from history (`git filter-repo`, then force-push every branch) and run
     the 4-factor breach risk assessment.
2. **JWT master secret is `"changeme"`**
   - `backend/app/config.py:87`; `.env` has no `JWT_SECRET_KEY`.
   - Anyone can forge a platform-admin token for any tenant.
   - The same key protects the audit hash-chain HMAC and the TOTP Fernet encryption.
   - There is no startup guard against default secrets.
3. **Unauthenticated DICOMweb/WADO**
   - `orthanc/orthanc.json:15` has `AuthenticationEnabled: false`.
   - nginx proxies `/dicom-web` and `/wado` without auth and reflects any Origin.
   - `/api/dicomweb/*` is exempt from auth.
   - This exposes every patient's images and DICOM tags.
4. **PHI to Google Gemini without a BAA**
   - `.env` has `LLM_ENABLED`, `VLM_QA_ENABLED` and `CDS_ENABLED` set to true.
   - Images, age/sex, clinical history and findings are sent through the consumer
     `google-generativeai` SDK with an API key, not Vertex AI.
   - `phi_deidentify_enabled` is **never read** anywhere, and `PHIScrubber` is never called.
5. **No transmission security**
   - nginx listens on HTTP `:80` only: no TLS, no HSTS.
   - Postgres, Redis (no password), MinIO, backend `:8000`, OHIF, UI and DICOM `4242`
     (`DicomAlwaysAllowStore: true`) are published on all host interfaces.
   - Internal traffic is plaintext.
6. **Audit log is deletable**
   - `/api/admin/reset` deletes audit rows, and a tenant admin is enough to run it.
   - Retention can purge audit logs with no 6-year floor (164.316(b)(2)).

### High
- **Public self-registration:** `/auth/register` gives the viewer role in the Admin tenant.
- **PHI routes without permission checks:** `reports.py`, `results.py`, `jobs.py`,
  `medgemma_debug.py` and `/orthanc/studies` have no `require_permission`.
- **Unaudited study delete:** `DELETE /studies/{uid}` has no tenant scope, permission check or
  audit entry.
- **Artifact IDOR:** artifact downloads are reachable across tenants; presigned URLs last 1 hour
  over HTTP.
- **WebSocket:** no authentication; broadcasts to all.
- **Default credentials:** seeded `admin/admin123` with no forced change
  (`alembic/versions/016_roles_rbac.py`).
- **Weak login protection:**
  - No password-login lockout or rate limit.
  - MFA is optional and not enforced for admins or radiologists.
- **Incomplete read audit (164.312(b)):**
  - Not audited: report PDF, consolidated, narrative, clinical-context and longitudinal
    reports; study list and detail; patient views; mammography reports; images and artifacts;
    **all DICOMweb/OHIF viewing**.
  - `AuditService.record` skips the hash chain and writes `tenant_id="default"`.
- **Failed logins and logouts not audited;** several deletes are unaudited.
- **No encryption at rest:**
  - Postgres, MinIO (no SSE), Orthanc and Redis sit on plain Docker volumes.
  - Backups are unencrypted `pg_dump` output.
- **Insecure secret defaults** in `config.py`: `changeme_in_production`, `orthanc/orthanc`,
  `phi_hash_salt="changeme"`.
- **Administrative safeguards (164.308) missing:**
  - Risk analysis
  - Security official
  - Sanctions policy
  - Workforce training
  - Incident response / breach notification
  - Contingency / DR plan with tested restores
  - BAA register
  - Emergency-access (break-glass) procedure
  - Media disposal

### Medium
- **Sessions:**
  - 8-hour JWT, no refresh token, no server-side logout or revocation.
  - Deactivated users stay valid until their token expires (`is_active` is not checked).
  - No UI idle timeout.
  - JWT stored in `localStorage` (XSS-exposed) instead of httpOnly cookies.
- **Unsafe auth modes:** `auth_mode="none"`/`"api_key"` grant platform admin, with no production
  guard.
- **Unauthenticated operational endpoints:** `/metrics`, `/docs`, `/openapi.json` and
  `/api/orthanc/notify-stable-study`.
- **Weak password policy:** 8 characters, no complexity rules.
- **Integrity:** no integrity checksums on MinIO artifacts or PDFs.
- **Retention is incomplete:** it only deletes DB rows; MinIO objects and Orthanc DICOM remain.
  It is also not tenant-scoped.
- **Dev configuration in production compose:** `--reload` plus a bind-mounted source tree.
- **Error leakage:** raw exception text is returned in HTTP errors.
- **Patient access:** no patient right-of-access or accounting-of-disclosures report
  (164.524/164.528).

### Already compliant or partially compliant
- Unique user IDs (UUID).
- Tenant-scoped repositories.
- `pbkdf2_sha256` password hashing.
- Hashed API keys.
- TOTP MFA with Fernet-encrypted secrets.
- Revocable, audited impersonation.
- FHIR AuditEvent-shaped audit log with an HMAC hash chain and verify endpoint.
- Result versioning plus `model_version`/`model_checksum`.
- Local MedGemma (Ollama) keeps PHI on the host.
- Logs avoid patient names and MRNs.
- The live `.env` has `AUTH_MODE=jwt`.
- Orthanc Explorer is blocked in nginx.

---

## 7. Prioritised remediation roadmap

### Phase 0: HIPAA blockers (immediately)
1. Purge PHI files from git history on all branches and the remote. Do the breach assessment.
2. Set strong `JWT_SECRET_KEY`, `PHI_HASH_SALT`, Orthanc, MinIO and Postgres secrets, and add
   them to `.env.example`. Add a startup check that refuses to boot on `changeme*` defaults.
   Plan MFA re-enrolment and re-anchoring the audit chain after rotation.
3. Disable the Gemini flags until a Google Cloud BAA (Vertex AI) is signed. Wire `PHIScrubber`
   into every outbound LLM payload, and default to local MedGemma.
4. Put JWT/tenant authorization in front of `/dicom-web` and `/wado` (nginx `auth_request` or
   the Orthanc authorization plugin). Pass the token from OHIF. Remove the `/api/dicomweb`
   auth bypass.
5. Bind Postgres, Redis, MinIO, backend, OHIF and UI to `127.0.0.1` or the internal network
   only. Add Redis `requirepass`. Restrict DICOM 4242 by AE title and IP.
6. TLS 443 plus HSTS in nginx; Postgres `sslmode=require`; MinIO TLS.
7. Remove `audit_log` from `/admin/reset` and retention. Make the audit table append-only
   (REVOKE UPDATE/DELETE plus trigger).

### Phase 1: Real tenant isolation
1. Postgres RLS policies on every tenant table, with `app.tenant_id` set per connection (API
   and Celery).
2. Fix every leak in §2.3. Tenant-scope the reading, mammography, share-link, longitudinal and
   user-management services.
3. MinIO keys prefixed `{tenant_id}/…`, and artifact reads validated through the result
   repository.
4. Authenticated WebSocket with a per-tenant channel.
5. Per-tenant Orthanc, or tenant-aware DICOMweb authorization. Orthanc webhook ingest must
   attribute the tenant, not fall back to `default`.
6. Make username and email unique per tenant (`(tenant_id, username)`), and add tenant (slug)
   selection at login.
7. Move global settings (routing, site-config, models, experiments, retention/apply) to
   platform-admin only, or make them per-tenant overrides.
8. Cross-tenant isolation test suite.

### Phase 2: RBAC to pulsdesk level
1. `resource:action` permission catalogue with wildcards and `:own` scoping, e.g.
   `study:read:assigned`, `report:sign`, `patient:create`.
2. `require_permission` on **every** route.
3. Seed system roles on tenant creation: Tenant Admin, Radiologist, **Doctor/Referring
   Physician**, Technician, Receptionist, Viewer.
4. Invitation flow (hashed 72-hour token), tenant-admin user management, last-admin protection,
   and audited role/user changes.
5. Backend-served permission config (`/api/permissions/me`) that drives the sidebar, route
   guards and action gates. Delete the hard-coded map. Add a role builder UI.
6. Enforce feature entitlements on routing and LLM add-ons.

### Phase 3: Dashboards, branding and operator console
1. `dashboard_layouts` table (owner, role default, shared, widgets, layout, filters) plus
   version history.
2. Widget catalogue: worklist counts, TAT, critical alerts, AI QA flags, studies by modality,
   radiologist workload, pending sign-off, and so on. Grid builder UI, role-default dashboards
   seeded per tenant.
3. Tenant branding: logo, colours, display name, report header/footer, optional custom domain.
4. Operator console:
   - Separate operator identity and secret.
   - Platform overview with per-tenant usage and quotas.
   - Ops health.
   - Cross-tenant audit viewer.
   - User oversight (reset, revoke sessions).

### Phase 4: Remaining HIPAA and session hardening
1. 15–30-minute access token plus refresh token, server revocation, and an `is_active` check
   per request. httpOnly SameSite cookies; UI idle logoff.
2. Login lockout and rate limiting, password policy, MFA required for admin and clinical roles,
   forced password change for seeded admin.
3. Complete PHI read auditing, including DICOMweb. Audit failed logins, logouts and all deletes.
   Enforce a 6-year audit retention floor.
4. Encryption at rest (encrypted volumes / MinIO SSE-KMS), encrypted off-site backups with
   restore tests.
5. Administrative documentation: risk analysis, policies, incident response, contingency plan,
   BAA register, break-glass procedure, training records.
6. Production compose without `--reload` or source mounts; sanitised error messages; retention
   covering MinIO and Orthanc.
