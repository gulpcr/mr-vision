# Multi-Tenancy, RBAC & Dashboards — Implementation and Rollout

Branch `feat/multitenancy` · migrations **044–047** · companion to
`MULTITENANCY_HIPAA_GAP_ANALYSIS.md`.

Each hospital or clinic is a **workspace** (tenant). Workspaces are isolated from each other
in the database, in the shared PACS, in object storage and in live events.

---

## 1. What was built

### Isolation (Phases 1–2)

**Postgres Row-Level Security (044).** Every tenant table carries a `tenant_isolation` policy.
The request's tenant is `SET LOCAL` per transaction
(`infrastructure/tenant/db_scope.py`, `after_begin` hook in `database/session.py`).
- An unscoped session sees **nothing** (fail closed).
- Cross-tenant code must opt in with `platform_scope()`. This applies to login, platform-admin
  routes, the Orthanc webhook, DICOM upload and beat jobs.

**Application-level filters remain as defence in depth.** The following leaks were fixed:
- study delete;
- Orthanc list and delete;
- ingest UID conflicts (now 409);
- reading workflow;
- mammography;
- share links;
- artifacts;
- longitudinal priors;
- user role changes and deactivation.

**Shared PACS.**
- **Tenant attribution.** Each tenant has called AE titles (`tenant_dicom_endpoints`, 045). The
  stable-study webhook resolves the tenant in this order: pending upload claim, then AE title,
  then `DICOM_UNMAPPED_AET_TENANT`. Unmapped studies stay **unattributed**, labelled
  `unattributed` in Orthanc.
- **Image access.** nginx `auth_request` sends every `/dicom-web` and `/wado` request to
  `/api/internal/dicomweb-authz`. That endpoint checks the httpOnly viewer-session cookie and
  confirms the study belongs to the viewer's tenant (or to their referrals, for doctors).
- **Study search.** Study searches go through a tenant-filtered QIDO proxy.

**Storage.** MinIO keys are `{tenant}/{study}/{usecase}/{name}`.
`scripts/migrate_artifact_keys.py` moves legacy objects; until it runs, reads fall back to the
old keys.

**Realtime.** The WebSocket is authenticated by the viewer cookie. Events are delivered per
tenant over Redis pub/sub, so events from the worker reach the browser.

**Pre-existing bugs fixed along the way:**
- The Orthanc Lua webhook had never fired, because `PrintToLog` does not exist in Orthanc's Lua.
- An inline webhook ingest deadlocks Orthanc, so it now runs as a background task.

### Accounts, provisioning and lifecycle (Phase 3, migration 046)

**Accounts.**
- **Per-workspace accounts.** Usernames and emails are unique **per tenant**. Login resolves the
  workspace from the form field, `X-Tenant-Slug` or the subdomain. An ambiguous username
  without a workspace returns 409.
- **Invitations, not passwords.** Users are invited with a hashed one-time token (72 h) and set
  their own password at `/accept-invite`. Public `/auth/register` is disabled by default.

**One-step provisioning.** `POST /api/admin/tenants` creates, in one transaction:
- the tenant;
- the six system roles;
- settings and branding;
- role-default dashboards;
- an optional AE title;
- a seat limit;
- an **invited** first admin.

**Workspace settings and branding.** Institution name and report signatories appear on the
tenant's PDFs. The logo, colours and display name appear on the login page and in the sidebar.

**Lifecycle.**
- Suspend and offboard (existing).
- A seat limit, enforced at invite time.
- A platform **purge** that deletes all tenant data, artifacts and PACS studies while keeping the
  audit log.

### RBAC (Phase 4)

**Permission model.**
- Permissions follow the pattern `resource.action[.scope]`, with `*` and `resource.*`
  wildcards and `a||b` alternatives (`domain/permissions.py`).
- **Live permissions.** Every request resolves a live principal from the database
  (`infrastructure/auth/principal.py`). The account must be active, the token version must be
  current (so sessions can be revoked), and permissions come from the role's current
  definition. The role claim inside the token is never trusted.

**System roles, seeded into every tenant:**

| Role | Access |
|---|---|
| admin | Everything in the workspace (`*`, locked) |
| radiologist | Read, claim, report and sign; critical alerts |
| doctor | `study.view.referred` — only the patients they referred |
| technician | Upload and run AI |
| receptionist | Patient intake |
| viewer | Read-only |

**Doctor referral scope.**
- At intake, an order records `referring_user_id`, which is copied to the study.
- Restrictive RLS policies confine a doctor to referred studies and every row derived from
  them. The same filter is applied at application level.
- The doctor's viewer cookie carries the same restriction into OHIF.

**Enforcement.**
- **Every route declares a permission.** `test_every_route_declares_a_permission` fails CI if a
  new route doesn't.
- **Admin safeguards.** Last-admin protection. Only an admin can grant or invite an admin.
  System roles cannot be renamed. Role and user changes are audited.

**UI.**
- Server-driven `can()`, re-synced on focus and every 60 s.
- A route guard on every page.
- Pages: role builder (`/admin/roles`), users with invite links, re-invite and sign-out-everywhere
  (`/admin/users`), and a referring-doctor picker at intake.

### Dashboards (Phase 5, migration 047)

**Data model.** Role-default dashboards per tenant, which that tenant's admins can edit, plus
personal and shared dashboards, and version history with restore.

**Widgets.** 15 widget types, all computed server-side in SQL inside the tenant (and referral)
scope:
- volume, modality and reading status;
- my worklist, pending sign-off, overdue and recent reports;
- critical alerts;
- turnaround time;
- radiologist workload;
- AI jobs and QA flags;
- intake;
- a notice.

**UI** (`/dashboard`): dashboard switcher, period filter, auto-refresh, and edit mode (add,
configure, reorder, resize). Also customise (clone), share, versions and delete. No new npm
dependencies.

### Operator console (Phase 6)

- `/admin/platform`: usage per workspace (users, studies, jobs, storage), a cross-tenant audit
  log, and user search with sign-out-everywhere and password-reset link.
- The tenant detail page adds AE titles, seat limit and purge.
- Platform flags are honoured **only for Admin-tenant accounts** and never for impersonation
  tokens.

---

## 2. Rollout checklist

1. **Back up** the database, MinIO and Orthanc.
2. **`.env` changes** (all documented in `.env.example`):

   | Variable | Value |
   |---|---|
   | `POSTGRES_APP_USER` / `POSTGRES_APP_PASSWORD` | Set to a new, strong app-role credential. Migration 044 creates the role. |
   | `ORTHANC_WEBHOOK_SECRET` | Random string, shared by backend and Orthanc. |
   | `DICOM_UNMAPPED_AET_TENANT` | Leave `default` while you are single-tenant; set it **empty** once each hospital has its AE title. |
   | `VIEWER_COOKIE_SECURE` | `true` once you serve HTTPS. |
   | `PUBLIC_REGISTRATION_ENABLED` | `false` |

3. **Rebuild images.**
   - **backend / worker / beat**: rebuild. The current image lacks `pyotp`, `qrcode` and
     `email-validator`.
   - **orthanc**: rebuild, because the Lua script is copied in at build time.
   - **ui**: rebuild.
   - **nginx**: reload, because the config changed.
4. **Start the backend.** `alembic upgrade head` applies 044–047 on startup. The log line
   `tenant_rls_enforced db_role=<app user>` confirms enforcement. Then set `REQUIRE_RLS=true` so
   a misconfiguration refuses to start.
5. **Move existing artifacts:**
   - Preview: `docker compose exec backend python scripts/migrate_artifact_keys.py --dry-run`
   - Then run it without `--dry-run`, and optionally with `--delete-legacy`.
6. **Existing users** keep working: their tokens carry `tv=0`, which matches the default. Users
   of the Admin tenant log in exactly as before (no workspace field needed while their username
   is unique).
7. **Onboard each hospital** in **Platform → Tenants → Create**, giving its slug, admin, AE title
   and seat limit. Send the admin their invite link, and point that hospital's scanners at its
   AE title.
8. **Entitlements.** Before setting `MULTI_TENANT_ENABLED=true`, give each plan its use-case
   features (`/admin/plans`); a tenant can only run use cases it is entitled to. The test suite
   passes with the flag on.
9. **Subdomains (optional).** Configure wildcard DNS and TLS for `*.TENANT_ROOT_DOMAIN`. Login
   then resolves the workspace from the host automatically.

---

## 3. Tests

| Suite | What it proves |
|---|---|
| `tests/rls/test_tenant_isolation.py` | Row-level security against real Postgres; cross-tenant reads and writes blocked |
| `tests/rls/test_shared_pacs_isolation.py` | DICOMweb authorization, viewer cookie, AE titles, WebSocket |
| `tests/rls/test_rbac_provisioning_dashboards.py` | Provisioning, invitations, per-workspace logins, live RBAC, revocation, doctor scope, dashboards, settings, operator console, route coverage |

- **Result:** 37 of 37 pass, both with `MULTI_TENANT_ENABLED` off and on.
- **How to run:** point `RLS_TEST_OWNER_DATABASE_URL` at a migrated database and connect the app
  as the RLS role (see the docstring of `test_tenant_isolation.py`).
- **Manual end-to-end check** (`tests/rls/e2e_cstore.py`): real C-STORE to Orthanc produces
  webhook attribution and a tenant label, then nginx returns 401 / 200 / 403 on DICOMweb as
  expected.
- **Regular suite:** unchanged at 48 failures, all present on the base branch.

---

## 4. Known limitations / next steps

- **StudyInstanceUID is a global primary key.** The same study cannot live in two workspaces;
  a second tenant sending it gets a 409 conflict.
- **Viewer tokens are not revoked with sessions.** They are not re-checked against
  `token_version`, so revoking sessions ends API access immediately but OHIF access only when
  the viewer cookie expires (the access-token lifetime).
- **WebSocket events are per tenant, not per referral.** A doctor's socket receives job events
  for the whole workspace (study UIDs only, no clinical content).
- **The QIDO study search filters after Orthanc applies `limit`,** so a page of the study list
  can come back short.
- **No mail relay.** Invite and reset links are shown to the admin to send.
- **HIPAA items outside this scope** are listed in `MULTITENANCY_HIPAA_GAP_ANALYSIS.md` §6:
  TLS, encryption at rest, Gemini BAA, audit read coverage.
