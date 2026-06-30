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
    allowed_origins: str = "http://localhost,http://localhost:3000,http://localhost:80"

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

    # Worklist (F12)
    worklist_enabled: bool = False
    worklist_scp_host: str = ""
    worklist_scp_port: int = 2575

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

    # MRI narrative report layout (formal radiology report for the MRI use cases:
    # brain/spine/chest/abdomen). Defaults match the departmental brain MRI template;
    # all body fields are editable per study by the radiologist.
    mri_report_examination_default: str = "MRI OF THE BRAIN PLAIN AND CONTRAST"
    mri_report_technique_default: str = (
        "Multiplanar, multi-sequential MRI images of brain acquired with and "
        "without contrast."
    )
    mri_report_signatory_name: str = "Dr. Ammar-e-Yasir"
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
