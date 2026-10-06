from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # PostgreSQL
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "mri_platform"
    postgres_user: str = "mri_admin"
    postgres_password: str = "changeme_in_production"
    # Row-Level Security app role (alembic 044). POSTGRES_USER owns the schema and runs
    # migrations; Postgres exempts owners/superusers from RLS, so tenant isolation is
    # only DB-enforced when the application connects as this separate NOSUPERUSER /
    # NOBYPASSRLS role. Leave empty to keep connecting as POSTGRES_USER (policies inert,
    # app-level tenant filters only).
    postgres_app_user: str = ""
    postgres_app_password: str = ""
    # Shared secret the Orthanc stable-study webhook must present (X-Orthanc-Webhook-Secret,
    # sent by orthanc/on_stable_study.lua from ORTHANC_WEBHOOK_SECRET). Empty = not
    # enforced (logged as a warning) for backward compatibility.
    orthanc_webhook_secret: str = ""
    # Tenant for C-STORE studies whose called AE title matches no tenant_dicom_endpoints
    # row. "default" (the Admin tenant) keeps single-tenant deployments working as before;
    # set to "" in multi-tenant production so unmapped studies are left unattributed
    # (visible only to platform admins) instead of landing in the Admin tenant.
    dicom_unmapped_aet_tenant: str = "default"
    # Refuse to start unless the DB session is actually subject to RLS (i.e. connected
    # as a non-superuser without BYPASSRLS). Turn on in production once the app role is
    # configured.
    require_rls: bool = False

    # Redis
    redis_host: str = "redis"
    redis_port: int = 6379
    # requirepass of the Redis server; used by every direct Redis client (rate limits,
    # impersonation blocklist, realtime events). Celery reads its own broker/backend URLs.
    redis_password: str = ""
    # TLS to Redis (prod: rediss:// + CA from the internal PKI, ops/pki/make-certs.sh).
    redis_tls: bool = False
    redis_ca_cert: str = ""

    # MinIO
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "mri_minio_admin"
    minio_secret_key: str = "changeme_in_production"
    minio_bucket: str = "mri-artifacts"
    minio_secure: bool = False
    # CA bundle that signed MinIO's certificate (internal PKI); empty = system trust store.
    minio_ca_cert: str = ""

    # Orthanc
    orthanc_host: str = "orthanc"
    orthanc_http_port: int = 8042
    orthanc_dicom_port: int = 4242
    orthanc_username: str = "orthanc"
    orthanc_password: str = "orthanc"
    # Orthanc REST/DICOMweb over HTTPS (prod) and the CA that signed its certificate.
    orthanc_scheme: str = "http"
    orthanc_ca_cert: str = ""
    # TLS to PostgreSQL: disable | require | verify-full (prod: verify-full + CA).
    db_ssl_mode: str = "disable"
    db_ssl_root_cert: str = ""
    # Production mode: the secure-config check also requires TLS on every internal
    # connection, MinIO server-side encryption and encrypted Orthanc storage.
    production_mode: bool = False
    minio_kms_configured: bool = False
    orthanc_storage_encrypted: bool = False
    # External AI (Gemini) receives PHI. In production it is used only when the operator
    # declares the provider is covered by a Business Associate Agreement.
    external_ai_baa_confirmed: bool = False
    # Destinations PHI may be sent to (webhooks, FHIR): the BAA register. Comma-separated
    # hostnames; empty = any https host (development).
    outbound_allowed_hosts: str = ""

    # Backend
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    log_level: str = "INFO"
    secret_key: str = "changeme_in_production_use_openssl_rand"

    # Celery
    celery_broker_url: str = "redis://redis:6379/0"
    celery_result_backend: str = "redis://redis:6379/1"
    celery_worker_concurrency: int = 2

    # Job cancellation: signal sent to a running worker child when a job is
    # stopped from the worklist. SIGTERM lets billiard reap the child and free
    # GPU memory; switch to SIGKILL for an immediate hard stop if a pipeline is
    # blocked in a long native/CUDA call and ignores SIGTERM.
    job_cancel_signal: str = "SIGTERM"

    # GPU
    enable_gpu: bool = True

    # Site
    site_id: str = "default"

    # Routing / auto-classification
    # When True, a rule that declares region conditions (body parts / study or
    # series description patterns) only matches a study that positively matches at
    # least one of them — modality is necessary but NOT sufficient. This stops an
    # MR study with sparse DICOM tags from routing to every MR use case. Set False
    # for legacy OR-matching (modality OR any region condition).
    routing_require_region_match: bool = True

    # Patient identity on screen: False (default) shows the MRN (DICOM PatientID) wherever
    # a patient name would appear — UI, PDF reports, the OHIF viewer (DICOMweb responses
    # are rewritten). Stored data is untouched, and machine interfaces (DICOM SR/SEG
    # written back to the PACS, modality worklist, FHIR) keep the real name so the PACS
    # files objects under the right patient. True restores patient names on screen.
    display_patient_names: bool = False

    # Viewer: hide non-diagnostic series (localizers, shim/calibration, field
    # maps, scouts) from the OHIF series list by filtering the QIDO /series
    # response. OHIF builds its thumbnails/viewports from whatever that query
    # returns, so dropping a series here hides it everywhere; pixels/WADO are
    # untouched. Matched on SeriesDescription by the regex below.
    viewer_hide_nondiagnostic_series: bool = True
    # PRV-03 minimum necessary: the viewer's study/series lists are cut to an attribute
    # allowlist and demographics it never shows are removed from metadata and retrieved
    # files (domain/viewer_minimum_necessary.py). Production: on.
    viewer_minimum_necessary: bool = False
    viewer_nondiagnostic_series_pattern: str = (
        r"(?i)(\bshim|shimming|localiz|localis|\bscout\b|\bloc\b|3[\s-]?axis|"
        r"3[\s-]?plane|\bsurvey\b|calibration|field\s*map|fieldmap|b0\s*map|"
        r"b1\s*map|map\(|aascout|aahead|smartbrain|pre[\s_-]?scan|"
        # Siemens non-image objects: the scanner protocol dump (PhoenixZIPReport / CSA
        # report) and syngo "Evidence Documents" SRs — no pixel data, nothing to read,
        # and their frames return 500 from Orthanc if requested.
        r"phoenix\s*zip|csa\s*report|evidence\s*doc)"
    )
    # Viewer: link CT-report flagged-slice tiles to their exact DICOM image
    # (GET /api/results/{uid}/{usecase}/flagged-slices) so the study page can jump
    # the embedded OHIF viewer there and highlight flagged images while scrolling.
    flagged_slice_viewer_link_enabled: bool = True

    # Auth
    api_key: str = ""

    # CORS
    allowed_origins: str = "http://103.93.216.37,http://103.93.216.37:3000,http://103.93.216.37:80"

    # Auth / RBAC (F1)
    jwt_secret_key: str = "changeme"
    jwt_algorithm: str = "HS256"
    # Master-key rotation: derive_secret() subkeys (tenant JWT signing, TOTP-at-rest
    # encryption, the audit hash chain) all come from jwt_secret_key. The audit chain is
    # verified with the PREVIOUS master key for rows with seq <= the cut-over, so a
    # rotation doesn't make the existing chain unverifiable (scripts/rotate_master_key.py).
    audit_chain_previous_master_key: str = ""
    audit_chain_rotated_after_seq: int = 0
    # Refuse to start with default/placeholder secrets or an unauthenticated auth mode
    # (checked in main.lifespan). Only disable for local development.
    enforce_secure_config: bool = False  # set ENFORCE_SECURE_CONFIG=true in deployments
    # OpenAPI docs (/docs, /redoc, /openapi.json) expose the full API surface; off by
    # default in deployments.
    api_docs_enabled: bool = False
    # Viewer session (OHIF / DICOMweb / WebSocket). The OHIF viewer and the browser
    # WebSocket cannot attach an Authorization header, so after login the API also sets
    # this httpOnly cookie carrying a narrowly-scoped "viewer" JWT (tenant-bound, same
    # lifetime as the access token). nginx auth_request validates it on every
    # /dicom-web and /wado request. Set VIEWER_COOKIE_SECURE=true once served over TLS.
    # Public self-registration (POST /api/auth/register). Off: accounts are created by
    # workspace admins through invitations, never by anonymous sign-up into a tenant.
    public_registration_enabled: bool = False
    # Lifetime of invitation / password-reset links (hours).
    invitation_ttl_hours: int = 3
    viewer_cookie_name: str = "mrv_viewer"
    viewer_cookie_secure: bool = False
    # Access tokens are short-lived; a session continues only through the rotating
    # refresh token (httpOnly cookie, alembic 051). The viewer cookie follows the access
    # token lifetime and is re-issued at every refresh.
    jwt_access_token_expire_minutes: int = 15
    # Automatic logoff (HIPAA 164.312(a)(2)(iii)): a session not refreshed within
    # session_idle_minutes ends (server-enforced; the UI warns shortly before), and no
    # session outlives session_absolute_hours regardless of activity.
    session_idle_minutes: int = 15
    session_absolute_hours: int = 12
    refresh_cookie_name: str = "mrv_refresh"
    # The access token also travels as an httpOnly cookie, so the browser UI never keeps
    # it in script-readable storage. Requests authenticated by this cookie must carry
    # ``X-Requested-With: mrcv`` on state-changing methods (CSRF, with SameSite=Strict).
    # access_token_in_body=false (production) stops returning the token in JSON at all,
    # so script injection cannot lift a reusable credential; API clients then use API
    # keys. The impersonation token is always returned (operators hold it per tab).
    access_cookie_name: str = "mrv_access"
    access_token_in_body: bool = True
    # Break-glass emergency access (alembic 053): how long a grant lasts, and the
    # minimum length of the stated reason.
    # Outbound integrations (webhooks, FHIR) must use https:// so PHI is encrypted in
    # transit; allow plain http only for local development.
    allow_insecure_outbound: bool = False
    # Webhook payloads omit the patient identifier (MRN) unless the receiver is covered
    # by a Business Associate Agreement and this is enabled.
    webhook_include_patient_id: bool = False
    # Debug endpoints (/api/debug/medgemma/*) expose raw model prompts and outputs for
    # a study; off unless explicitly enabled for development.
    debug_routes_enabled: bool = False
    # Retention purges never delete studies/results younger than this (medical-record
    # retention law, often 6-10 years depending on jurisdiction; 0 = no floor).
    retention_min_days: int = 0
    break_glass_minutes: int = 60
    break_glass_min_reason_chars: int = 20
    # Periodic audit-log review (alembic 055): Beat generates each tenant's summary for
    # the previous calendar month on the 1st; admins review and sign it off. Reads of
    # patient data outside business hours (local time zone) are called out.
    audit_review_enabled: bool = True
    # A/B experiments run the treatment model only as isolated SHADOW jobs (alembic 056) on
    # this Celery queue - consumed by the separate worker-shadow service, never by the
    # clinical worker. Off: experiments are recorded but nothing is run.
    ab_shadow_enabled: bool = False
    # Human-in-the-loop: AI results leave the platform (DICOM SR/SEG export, FHIR, webhook
    # findings) only after a radiologist's valid e-signature (application/export_gate.py).
    require_signed_for_export: bool = True
    # ADM-01: no PHI before a BAA. A tenant without an active BAA record (tenant_baas) is
    # provisioned suspended and cannot be activated or given a DICOM endpoint / API key.
    # Production: on.
    require_tenant_baa: bool = False
    # With PHI_DEIDENTIFY_ENABLED: OCR-mask text burned into US / secondary-capture /
    # BurnedInAnnotation=YES images before inference (fails closed if it cannot check).
    burned_in_text_check_enabled: bool = True
    # US deployments: a radiologist can sign (and SR export carries) a report only with a
    # valid NPI on their practitioner record (system http://hl7.org/fhir/sid/us-npi).
    require_npi_for_signing: bool = False
    shadow_queue: str = "mri_shadow"
    audit_review_timezone: str = "UTC"
    audit_review_business_hours: str = "07-19"
    # "monthly" (1st of the month) or "weekly" (Mondays) audit reviews.
    audit_review_cadence: str = "monthly"
    # Monthly signed audit-log archive into a compliance-mode Object Lock bucket (WORM,
    # created by minio/init.sh). Production: on.
    audit_archive_enabled: bool = False
    audit_archive_bucket: str = "audit-archive"
    audit_archive_retention_days: int = 2190
    # Security alert rules over the audit log every 10 min (security_alert_service.py).
    security_alerts_enabled: bool = True
    security_alert_failed_logins: int = 10
    auth_mode: str = "jwt"  # "jwt" | "api_key" | "none"

    # Multi-tenant (F2)
    multi_tenant_enabled: bool = False
    default_tenant_id: str = "default"
    # Subdomain a request's Host header is matched against to resolve a tenant slug
    # (e.g. "acme.mr-vision.ai" -> slug "acme"). Only consulted when
    # multi_tenant_enabled is True; header/query-param resolution works regardless.
    tenant_root_domain: str = "mr-vision.ai"

    # MFA (TOTP) lockout — shared chokepoint for the login-time verify step and
    # the /mfa/disable step (see infrastructure/ratelimit/mfa_lockout.py).
    mfa_max_attempts: int = 5
    mfa_lockout_minutes: float = 15
    # Password login lockout (HIPAA 164.312(d)): after login_max_attempts failed passwords
    # for one account the account is locked for login_lockout_minutes; independently each
    # client IP may attempt at most login_ip_attempts_per_minute logins.
    login_max_attempts: int = 5
    login_lockout_minutes: float = 15
    login_ip_attempts_per_minute: int = 20
    # Password policy (NIST SP 800-63B style: length + blocklist, no composition rules).
    password_min_length: int = 12
    # Roles that must use MFA; users in them are forced to enrol at their next sign-in
    # and can do nothing else until they have. Platform admins/operators are always
    # included when mfa_required_for_platform is true. Comma-separated role names.
    mfa_required_roles: str = ""
    mfa_required_for_platform: bool = False

    # Self-authenticated DICOM upload via a tenant API key (POST /api/dicom/upload).
    dicom_upload_rate_limit_per_minute: int = 60

    # PHI De-identification (F3)
    phi_deidentify_enabled: bool = False
    phi_deidentify_method: str = "hash"
    phi_hash_salt: str = "changeme"

    # Model Registry (F7)
    model_registry_enabled: bool = True

    # Multi-GPU (F8)
    gpu_worker_queues: str = "gpu0,gpu1"

    # DICOM SR/SEG (F9, F10)
    dicom_sr_enabled: bool = False
    dicom_seg_enabled: bool = False

    # In-app DICOM upload: lets authenticated users push DICOM files/folders from
    # the browser (POST /api/studies/upload) instead of the open Orthanc Explorer.
    dicom_upload_enabled: bool = True

    # Lets authenticated users permanently delete a study from Orthanc PACS
    # (DELETE /api/orthanc/studies/{orthanc_id}) from the Upload page. Irreversible —
    # the DICOM data is gone unless the scanner resends it.
    orthanc_delete_enabled: bool = True

    # FHIR (F11)
    fhir_enabled: bool = False
    fhir_server_url: str = ""
    # Identifier.system URIs for the identifiers this deployment issues/receives. An
    # Identifier without a system is not matchable — "12345" says nothing about which
    # numbering scheme issued it, and across tenants two hospitals' MRN "12345" are
    # indistinguishable. Only the operator knows their own namespace, so these are
    # config rather than constants (the code-system URIs live in
    # app/domain/fhir_terminology.py). Empty means "unknown": the identifier is then
    # omitted from FHIR output rather than emitted unqualified.
    # Per-tenant override belongs on the fhir_connections table (integration plan P0);
    # until that exists these apply deployment-wide.
    # Derive row-per-value Observations from intake orders and mammography reports into
    # the observations table (FHIR Observation shape). On by default: it is the platform's
    # structured clinical store, not an optional integration. The switch exists so an
    # operator can stop the derived writes without reverting a migration; the legacy
    # columns are dual-written either way, so nothing depends on it being on.
    observations_enabled: bool = True
    fhir_patient_identifier_system: str = ""      # namespace of studies.patient_id (MRN)
    fhir_accession_identifier_system: str = ""    # namespace of studies.accession_number
    fhir_order_identifier_system: str = ""        # namespace of orders.external_order_ref

    # Worklist (F12)
    worklist_enabled: bool = False
    worklist_scp_host: str = ""
    worklist_scp_port: int = 2575

    # HL7 v2 messaging (F21)
    # Inbound ADT (patient demographics) + ORM/OMG (imaging orders) received over
    # MLLP by a standalone listener process (app.hl7_listener), and outbound ORU
    # (results) sent back to the RIS/EHR. Fully gated: nothing runs unless enabled.
    # NB: MLLP defaults to 2576 because worklist_scp_port already claims 2575.
    hl7_enabled: bool = False
    hl7_mllp_host: str = "0.0.0.0"
    hl7_mllp_port: int = 2576
    hl7_sending_application: str = "MRCV"
    hl7_sending_facility: str = "MRCV_AI"
    hl7_accept_version: str = "2.5.1"
    hl7_default_tenant: str = "default"
    # Store raw inbound message text at rest. When False (default), only a sha256
    # hash + de-identified parsed fields are persisted (PHI minimisation); set True
    # only where a signed retention policy permits raw HL7 with PHI on disk.
    hl7_store_raw_messages: bool = False
    # Outbound ORU (results → RIS/EHR). Sent from the post-result Celery hook.
    hl7_outbound_enabled: bool = False
    hl7_outbound_host: str = ""
    hl7_outbound_port: int = 0
    hl7_outbound_timeout_s: int = 30
    hl7_outbound_max_retries: int = 3

    # Alerting (F14)
    alerting_enabled: bool = False
    alerting_default_webhook_url: str = ""

    # Retention (F15)
    retention_enabled: bool = False
    retention_default_max_age_days: int = 365

    # Active Learning (F20)
    active_learning_enabled: bool = False
    confidence_threshold: float = 0.7

    # LLM Report Generation (Phase 1)
    llm_enabled: bool = False
    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-flash"
    # Which Gemini endpoint GeminiClient talks to:
    #   "api_key" — Google AI Studio via GEMINI_API_KEY (local/dev; NOT covered by the
    #               Google Cloud BAA — never send PHI through it in production)
    #   "vertex"  — Vertex AI in GCP_PROJECT/GCP_LOCATION, authenticated with Application
    #               Default Credentials (the VM's service account); no key. Production.
    gemini_backend: Literal["api_key", "vertex"] = "api_key"
    gcp_project: str = ""
    gcp_location: str = "us-central1"

    # VLM Image Quality Assessment (Phase 2)
    vlm_qa_enabled: bool = False
    vlm_qa_max_series: int = 3

    # LLM Clinical Decision Support (Phase 3)
    cds_enabled: bool = False

    # LLM Longitudinal Analysis (Phase 4)
    longitudinal_enabled: bool = False
    longitudinal_max_prior_studies: int = 5

    # PET-CT AI-authored report findings (Gemini reads MIP/fused images + computed
    # lesion data and writes SCAN FINDINGS/CONCLUSIONS; falls back to the deterministic
    # template on failure or when disabled)
    petct_ai_report_enabled: bool = False

    # MedGemma 1.5 4B via Ollama — local vision-language engine for PET/CT findings.
    # When enabled, the pet_ct pipeline sends the composite lesion roadmap + regional
    # crop images and the deterministic data-matrix prompt to a locally-hosted
    # MedGemma model and merges the generated findings into
    # Result.summary["medgemma_findings"]. Fully non-blocking: any failure (Ollama
    # down, model not pulled, timeout) is logged as a warning and the deterministic
    # summary is preserved unchanged. Runs alongside the Gemini path — it does not
    # replace it — so the pipeline stays runnable when MedGemma is unavailable.
    medgemma_enabled: bool = False
    # Tier-1 deterministic organ grounding (abdomen_ct): run TotalSegmentator to measure
    # organ sizes, surface size-based findings (hepatomegaly/splenomegaly/AAA) as facts,
    # and feed them to the report-writer as authoritative ground truth so it reports
    # organomegaly the VLM cannot perceive and is anchored against confabulating a mass
    # over normal-measured organs. Needs TotalSegmentator in the worker; non-blocking.
    organ_grounding_enabled: bool = True
    # EXPERIMENT (abdomen_ct only): inject patient age/sex into the Stage-1 scan/flag
    # prompt as normal-for-age calibration context. TESTED 2026-08-10 on SANIA (pediatric)
    # and REVERTED: age did NOT improve flagging — with "female, 9 years" injected, 27B
    # swapped its vague confabulation ("free fluid") for a SPECIFIC age-primed hallucination
    # ("large right adrenal mass" — the classic pediatric tumor — at an anatomically wrong
    # pelvic level). The explicit "do NOT hunt age-associated disease" guard failed to stop
    # the priming. Left OFF; plumbing kept dormant so it can be re-tested (e.g. adults only).
    abdomen_scan_age_context: bool = False
    # Host-run default; the containerised worker overrides this to
    # http://host.docker.internal:11434 (see docker-compose.yml) because inside a
    # container "103.93.216.37" is the container itself, not the host running Ollama.
    ollama_base_url: str = "http://103.93.216.37:11434"
    medgemma_model: str = "medgemma1.5:latest"   # Ollama model tag; must be `ollama pull`ed
    medgemma_max_images: int = 6          # token budget: cap images sent per study
    medgemma_timeout_s: int = 300         # local inference can be slow on CPU

    # Coronary CTA AI-authored report narrative (Gemini reads calcium-overlay images +
    # the deterministic Agatston/stenosis findings and writes a synthesized narrative;
    # falls back to the deterministic diagnosis/processing_notes already set by
    # postprocess() on failure, disablement, or a malformed/ungrounded response)
    coronary_cta_ai_report_enabled: bool = False

    # PET-CT Molecular Imaging report layout (renders the formal departmental
    # FDG PET-CT report for the pet_ct / pet_ct_brain use cases).
    report_institution_name: str = "DEPARTMENT OF MOLECULAR IMAGING"
    report_signatory_primary: str = "Dr. Salman Habib"
    report_signatory_secondary: str = "Dr. Saifullah Sethar"

    # Mammography report layout (formal bilateral mammography report). Hospital
    # header, footer roster, and address are config so branding isn't hardcoded.
    report_hospital_name: str = "AECH-KIRAN"
    report_hospital_subtitle: str = (
        "Atomic Energy Cancer Hospital — "
        "Karachi Institute of Radiotherapy and Nuclear Medicine (KIRAN)"
    )
    report_footer_address: str = (
        "Haider Bux Gabol Road, Gulzar-e-Hijri, KDA Scheme 33, Karachi. "
        "Ph: 021-99261601-04 Ext. 222, 345"
    )
    # Footer doctor roster as "Name | Title" entries.
    report_footer_roster: list[str] = [
        "Dr. Asghar H. Asghar, FCPS | Oncologist, Director KIRAN",
        "Dr. Javed Mehboob, MCPS & FCPS | Radiologist (HOD)",
        "Dr. Muhammad Hanif, Ph.D | Molecular Pathologist",
        "Dr. Talal A. Rahman, M.Sc | Nuclear Physician",
        "Dr. Saifullah Sethar, FCPS | Radiologist",
        "Dr. Salman Habib, M.Sc, MD | Nuclear Physician (HOD)",
        "Dr. Adnan Hashmi, MCPS | Radiologist",
        "Dr. Javaid Iqbal, FCPS | Nuclear Physician",
        "Dr. Imran Hadi, M.Sc | Nuclear Physician",
        "Dr. Hasnain Dilawar, M.Sc | Nuclear Physician",
    ]
    mammography_procedure_default: str = (
        "Digital mammography of both breasts performed in routine CC and MLO views."
    )

    # Mammography structured findings — Mammo-CLIP zero-shot classifier (density,
    # mass presence, calcification presence). Gated + weights-path like GMIC; when
    # disabled or weights are absent the pipeline degrades gracefully and leaves the
    # model-fillable slots unset for the radiologist. NOTE: Mammo-CLIP is licensed
    # CC BY-NC-SA (non-commercial) — do not enable in a commercial deployment without
    # replacing it (e.g. ianpan/mammoscreen, Apache-2.0).
    mammography_clip_enabled: bool = False
    mammography_clip_weights_path: str = "/model_cache/mammo_clip"
    # Lesion localization detector (quadrant/margins). Deferred — needs fine-tuning
    # on VinDr-Mammo; scaffold only, off by default.
    mammography_detector_enabled: bool = False
    mammography_detector_weights_path: str = "/model_cache/mammo_detector"
    # Fine-tuned Mammo-CLIP B5 multi-label classifier (VinDr-Mammo): fills ALL seven
    # structured finding slots per breast — mass, calcification, architectural distortion,
    # skin thickening, nipple retraction, axillary lymph node, density (a-d) — with trained
    # probabilities (see backend/scripts/mammo_finetune/). Supersedes the zero-shot slots.
    # Needs the RELEASED B5 checkpoint too (mammography_clip_weights_path) to rebuild the
    # backbone architecture. CC BY-NC-SA (non-commercial) — same license caveat as above.
    mammography_finetuned_enabled: bool = False
    mammography_finetuned_weights_path: str = "/model_cache/mammo_clip_finetuned/best_all.pt"
    # AI-authored mammography report: Gemini reads the rendered views + the model's per-breast
    # finding probabilities and writes the per-breast findings / opinion / BI-RADS as a
    # radiologist would (mirrors petct_ai_report_enabled). Falls back to the deterministic
    # MammographyNarrativeService when disabled/failed. Requires gemini_api_key.
    mammography_ai_report_enabled: bool = False

    # MRI narrative report layout (formal radiology report for the MRI use cases:
    # brain/spine/chest/abdomen). Defaults match the departmental brain MRI template;
    # all body fields are editable per study by the radiologist.
    mri_report_examination_default: str = "MRI OF THE BRAIN PLAIN AND CONTRAST"
    mri_report_technique_default: str = (
        "Multiplanar, multi-sequential MRI images of brain acquired with and "
        "without contrast."
    )
    mri_report_signatory_name: str = "Dr"
    mri_report_signatory_title: str = "Consultant Radiologist"
    mri_report_signatory_qualifications: str = "MBBS, FCPS, M.Med"

    @property
    def _db_credentials(self) -> tuple[str, str]:
        if self.postgres_app_user and self.postgres_app_password:
            return self.postgres_app_user, self.postgres_app_password
        return self.postgres_user, self.postgres_password

    @property
    def database_url(self) -> str:
        user, password = self._db_credentials
        return (
            f"postgresql://{user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def async_database_url(self) -> str:
        user, password = self._db_credentials
        return (
            f"postgresql+asyncpg://{user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def orthanc_url(self) -> str:
        return f"{self.orthanc_scheme}://{self.orthanc_host}:{self.orthanc_http_port}"

    @property
    def dicomweb_url(self) -> str:
        return f"{self.orthanc_url}/dicom-web"

    @property
    def outbound_policy(self) -> dict:
        """Keyword arguments for domain.outbound_policy.check_outbound_url."""
        from app.domain.outbound_policy import parse_host_list

        return {
            "allow_insecure": self.allow_insecure_outbound,
            "allowed_hosts": parse_host_list(self.outbound_allowed_hosts),
            "require_listed": self.production_mode,
        }

    @property
    def external_ai_allowed(self) -> bool:
        """Gemini may receive PHI: it is configured and, in production, the operator has
        declared the provider is covered by a BAA (EXTERNAL_AI_BAA_CONFIRMED). Every
        GeminiClient checks this; without it the features fall back or are skipped."""
        return self.gemini_configured and (
            self.external_ai_baa_confirmed or not self.production_mode
        )

    @property
    def gemini_configured(self) -> bool:
        """True when GeminiClient has what it needs for the selected backend: an API key
        ("api_key") or a GCP project ("vertex", credentials come from ADC)."""
        if self.gemini_backend == "vertex":
            return bool(self.gcp_project)
        return bool(self.gemini_api_key)

    @property
    def usecases_dir(self) -> Path:
        return Path(__file__).parent / "usecases"

    @property
    def configs_dir(self) -> Path:
        return Path(__file__).parent.parent / "configs"

    @property
    def site_config_path(self) -> Path:
        return self.configs_dir / "sites" / f"{self.site_id}.yaml"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def derive_secret(purpose: str) -> str:
    """HMAC(jwt_secret_key, purpose) — a domain-separated subkey derived from the
    platform master secret, used wherever a purpose-specific signing/encryption key
    is needed: per-tenant JWT signing (``jwt:{tenant_id}``), the audit hash chain
    (``audit-chain-v1``), TOTP-secret-at-rest encryption (``totp-secret-encryption-v1``).

    Leaking one derived subkey does not reveal the master secret or any other
    purpose's subkey (HMAC is a PRF) — one compromised use case doesn't cascade.
    """
    import hashlib
    import hmac as _hmac

    return _hmac.new(
        get_settings().jwt_secret_key.encode("utf-8"), purpose.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def derive_secret_with(master_key: str, purpose: str) -> str:
    """``derive_secret`` for an explicit master key — used to verify/re-encrypt data
    protected under a previous master key during rotation."""
    import hashlib
    import hmac as _hmac

    return _hmac.new(master_key.encode("utf-8"), purpose.encode("utf-8"), hashlib.sha256).hexdigest()


_PLACEHOLDER_SECRETS = {"", "changeme", "changeme_in_production", "orthanc", "admin", "password"}


def insecure_config_problems(settings: Settings) -> list[str]:
    """Names (never values) of settings that make a deployment insecure."""
    problems: list[str] = []
    if settings.auth_mode != "jwt":
        problems.append(f"AUTH_MODE={settings.auth_mode} (must be jwt)")
    if settings.jwt_secret_key in _PLACEHOLDER_SECRETS or len(settings.jwt_secret_key) < 32:
        problems.append("JWT_SECRET_KEY is a default or shorter than 32 characters")
    if settings.phi_hash_salt in _PLACEHOLDER_SECRETS or len(settings.phi_hash_salt) < 16:
        problems.append("PHI_HASH_SALT is a default or shorter than 16 characters")
    if settings.orthanc_password in _PLACEHOLDER_SECRETS or len(settings.orthanc_password) < 16:
        problems.append("ORTHANC_PASSWORD is a default or shorter than 16 characters")
    if not settings.orthanc_webhook_secret:
        problems.append("ORTHANC_WEBHOOK_SECRET is empty")
    if not settings.redis_password:
        problems.append("REDIS_PASSWORD is empty")
    if settings.production_mode:
        problems.extend(production_config_problems(settings))
    return problems


def production_config_problems(settings: Settings) -> list[str]:
    """PRODUCTION_MODE requirements: encryption in transit and at rest is configured
    (HIPAA 164.312(a)(2)(iv), (e)). Names only, never values."""
    problems: list[str] = []
    if (settings.db_ssl_mode or "").lower() != "verify-full" or not settings.db_ssl_root_cert:
        problems.append("DB_SSL_MODE must be verify-full with DB_SSL_ROOT_CERT")
    if not settings.redis_tls:
        problems.append("REDIS_TLS is off")
    for name in ("celery_broker_url", "celery_result_backend"):
        if not getattr(settings, name).startswith("rediss://"):
            problems.append(f"{name.upper()} must use rediss://")
    if not settings.minio_secure:
        problems.append("MINIO_SECURE is off")
    if settings.orthanc_scheme != "https":
        problems.append("ORTHANC_SCHEME must be https")
    if not settings.minio_kms_configured:
        problems.append("MINIO_KMS_CONFIGURED is not declared (MinIO server-side encryption)")
    if not settings.orthanc_storage_encrypted:
        problems.append("ORTHANC_STORAGE_ENCRYPTED is not declared (Orthanc storage encryption)")
    if not settings.require_rls:
        problems.append("REQUIRE_RLS is off")
    if not settings.viewer_cookie_secure:
        problems.append("VIEWER_COOKIE_SECURE is off")
    if "*" not in {r.strip() for r in settings.mfa_required_roles.split(",")}:
        problems.append("MFA_REQUIRED_ROLES must be * (MFA for every account)")
    if settings.api_docs_enabled or settings.debug_routes_enabled:
        problems.append("API_DOCS_ENABLED / DEBUG_ROUTES_ENABLED must be off")
    if settings.allow_insecure_outbound:
        problems.append("ALLOW_INSECURE_OUTBOUND is on")
    if any(o.strip().startswith("http://") for o in settings.allowed_origins.split(",")):
        problems.append("ALLOWED_ORIGINS contains an http:// origin")
    # Compliance controls that must not be switched off in production.
    for name in ("require_tenant_baa", "require_signed_for_export", "phi_deidentify_enabled",
                 "viewer_minimum_necessary", "audit_archive_enabled"):
        if not getattr(settings, name):
            problems.append(f"{name.upper()} is off")
    return problems


def config_warnings(settings: Settings) -> list[str]:
    """Not fatal, but worth a line in the startup log."""
    warnings: list[str] = []
    if settings.production_mode and settings.gemini_configured and not settings.external_ai_baa_confirmed:
        warnings.append(
            "Gemini is configured but EXTERNAL_AI_BAA_CONFIRMED is not set: external AI "
            "features are disabled (local MedGemma only)"
        )
    if settings.production_mode and not settings.outbound_allowed_hosts:
        warnings.append(
            "OUTBOUND_ALLOWED_HOSTS is empty: every webhook / FHIR destination is refused"
        )
    return warnings
