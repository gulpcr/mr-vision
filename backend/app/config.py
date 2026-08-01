from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # PostgreSQL
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "mri_platform"
    postgres_user: str = "mri_admin"
    postgres_password: str = "changeme_in_production"

    # Redis
    redis_host: str = "redis"
    redis_port: int = 6379

    # MinIO
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "mri_minio_admin"
    minio_secret_key: str = "changeme_in_production"
    minio_bucket: str = "mri-artifacts"
    minio_secure: bool = False

    # Orthanc
    orthanc_host: str = "orthanc"
    orthanc_http_port: int = 8042
    orthanc_dicom_port: int = 4242
    orthanc_username: str = "orthanc"
    orthanc_password: str = "orthanc"

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

    # Viewer: hide non-diagnostic series (localizers, shim/calibration, field
    # maps, scouts) from the OHIF series list by filtering the QIDO /series
    # response. OHIF builds its thumbnails/viewports from whatever that query
    # returns, so dropping a series here hides it everywhere; pixels/WADO are
    # untouched. Matched on SeriesDescription by the regex below.
    viewer_hide_nondiagnostic_series: bool = True
    viewer_nondiagnostic_series_pattern: str = (
        r"(?i)(\bshim|shimming|localiz|localis|\bscout\b|\bloc\b|3[\s-]?axis|"
        r"3[\s-]?plane|\bsurvey\b|calibration|field\s*map|fieldmap|b0\s*map|"
        r"b1\s*map|map\(|aascout|aahead|smartbrain|pre[\s_-]?scan)"
    )

    # Auth
    api_key: str = ""

    # CORS
    allowed_origins: str = "http://103.93.216.37,http://103.93.216.37:3000,http://103.93.216.37:80"

    # Auth / RBAC (F1)
    jwt_secret_key: str = "changeme"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 480
    auth_mode: str = "jwt"  # "jwt" | "api_key" | "none"

    # Multi-tenant (F2)
    multi_tenant_enabled: bool = False
    default_tenant_id: str = "default"

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
    def database_url(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def async_database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def orthanc_url(self) -> str:
        return f"http://{self.orthanc_host}:{self.orthanc_http_port}"

    @property
    def dicomweb_url(self) -> str:
        return f"{self.orthanc_url}/dicom-web"

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
