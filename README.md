# MRI AI Platform

Production-grade AI-based MRI analysis platform. Brain-MRI first, architected for extensibility to Spine, Knee, and other MRI use cases through a plugin system.

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌──────────────┐
│   OHIF +    │────▶│   Nginx     │────▶│   Orthanc    │
│   Next.js   │     │   Reverse   │     │   PACS +     │
│   Frontend  │     │   Proxy     │     │   DICOMweb   │
└─────────────┘     └──────┬──────┘     └──────────────┘
                           │
                    ┌──────▼──────┐
                    │   FastAPI   │
                    │   Backend   │
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
       ┌──────▼──┐  ┌─────▼─────┐  ┌──▼───────┐
       │ Postgres │  │   Redis   │  │  MinIO   │
       │ Metadata │  │  Broker   │  │ Artifacts│
       └─────────┘  └─────┬─────┘  └──────────┘
                          │
                   ┌──────▼──────┐
                   │   Celery    │
                   │   Worker    │
                   │  (GPU/CPU)  │
                   └─────────────┘
```

**Layers (Clean / Hexagonal Architecture):**
- **Domain**: Study, Series, UseCase, JobRun, Result entities
- **Application**: RoutingService, UseCaseRegistry, JobOrchestrator, ResultService
- **Infrastructure**: OrthancClient, PostgreSQL repos, MinIO, Celery workers
- **Interface**: FastAPI routers, DTOs, RBAC middleware

## Folder Structure

```
MR_Computer-Visuion/
├── backend/
│   ├── app/
│   │   ├── domain/           # Entities, enums, interfaces
│   │   ├── application/      # Business logic services
│   │   ├── infrastructure/   # DB, PACS, storage, queue
│   │   ├── interface/        # API routers, schemas, middleware
│   │   └── usecases/         # Plugin modules
│   │       ├── brain_mri/    # Full implementation
│   │       └── spine_mri/    # Skeleton for extensibility
│   ├── alembic/              # Database migrations
│   └── configs/sites/        # Site-specific configuration
├── orthanc/                  # PACS configuration
├── nginx/                    # Reverse proxy
├── ui/                       # Next.js frontend
├── docker-compose.yml        # Full stack (GPU)
└── docker-compose.cpu.yml    # CPU override
```

## Prerequisites

- Docker and Docker Compose v2
- NVIDIA Container Toolkit (for GPU inference)
- 8GB+ RAM (16GB recommended)
- NVIDIA GPU with CUDA support (or CPU fallback)

## Quick Start

### 1. Configure Environment

```bash
cp .env.example .env
# Edit .env with your passwords (change all "changeme_in_production" values)
```

### 2. Start All Services (GPU)

```bash
docker compose up -d --build
```

### CPU-only Mode

```bash
docker compose -f docker-compose.yml -f docker-compose.cpu.yml up -d --build
```

### 3. Verify Services

| Service    | URL                          |
|------------|------------------------------|
| Platform   | http://localhost              |
| API Docs   | http://localhost:8000/docs    |
| Orthanc    | http://localhost:8042         |
| MinIO      | http://localhost:9001         |
| PostgreSQL | localhost:5432               |

### 4. Upload a DICOM Study to Orthanc

**Option A: Orthanc Web UI**
1. Open http://localhost:8042 (login: orthanc/orthanc)
2. Click "Upload" and select DICOM files

**Option B: DICOM C-STORE**
```bash
# Using dcm4che storescu
storescu localhost 4242 -aec MRI_AI /path/to/dicom/files/
```

**Option C: DICOMweb STOW-RS**
```bash
curl -X POST http://localhost:8042/dicom-web/studies \
  -u orthanc:orthanc \
  -H "Content-Type: application/dicom" \
  --data-binary @study.dcm
```

### 5. Ingest and Run AI

```bash
# Ingest a study (fetches metadata from Orthanc into platform DB)
curl -X POST http://localhost:8000/api/studies \
  -H "Content-Type: application/json" \
  -d '{"study_instance_uid": "1.2.3.4.5.6.7.8.9"}'

# Trigger AI analysis (routing engine auto-selects use cases)
curl -X POST http://localhost:8000/api/studies/1.2.3.4.5.6.7.8.9/jobs \
  -H "Content-Type: application/json" \
  -d '{}'

# Or specify use case explicitly
curl -X POST http://localhost:8000/api/studies/1.2.3.4.5.6.7.8.9/jobs \
  -H "Content-Type: application/json" \
  -d '{"usecase_names": ["brain_mri"]}'

# Check job status
curl http://localhost:8000/api/jobs/{job_id}

# Get results
curl http://localhost:8000/api/results/1.2.3.4.5.6.7.8.9/brain_mri
```

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/studies` | List all studies |
| GET | `/api/studies/{uid}` | Get study detail |
| POST | `/api/studies` | Ingest study from Orthanc |
| POST | `/api/studies/{uid}/jobs` | Create AI jobs |
| GET | `/api/jobs/{id}` | Get job status |
| GET | `/api/results/{uid}/{usecase}` | Get AI results |
| GET | `/api/artifacts/{uid}/{usecase}/{path}` | Download artifact |
| GET | `/api/usecases` | List registered use cases |
| GET | `/api/usecases/{name}/ui-schema` | Get UI rendering schema |
| GET | `/api/admin/routing-rules` | Get all routing rules |
| PUT | `/api/admin/routing-rules` | Update site routing overrides |

Full OpenAPI documentation: http://localhost:8000/docs

## Adding a New Use Case

Create a new directory under `backend/app/usecases/<name>/` with:

### 1. manifest.yaml

```yaml
name: knee_mri
version: "1.0.0"
description: "Knee MRI meniscus and ACL analysis"
supported_body_parts:
  - KNEE
required_sequences:
  - PD_SAG
  - T2_SAG
model_type: nnunet_v2
enabled: true
```

### 2. routing_rules.yaml

```yaml
rules:
  - body_parts: [KNEE]
    study_description_patterns:
      - "(?i)knee"
    modality: MR
    priority: 10
    enabled: true
```

### 3. pipeline.py

```python
from app.usecases.base import BasePipeline

class Pipeline(BasePipeline):
    def preprocess(self, study, series, working_dir, pacs):
        # Download DICOM, convert to NIfTI, validate sequences
        ...

    def infer(self, preprocessed, working_dir):
        # Load model, run inference
        ...

    def postprocess(self, inference_output, working_dir):
        # Compute measurements, generate artifacts
        # Must return dict with: summary, measurements, qa_flags,
        # qa_details, model_version, model_checksum, artifacts
        ...
```

### 4. outputs_schema.json & ui_schema.json

Define the JSON schema for outputs and the UI rendering schema.

### 5. model/

Place trained model weights under `model/weights/`.

**The platform will automatically discover and register the new use case on startup.**

## Brain MRI Use Case Details

The included Brain MRI use case provides:

- **Sequence identification**: Automatic T1/T2/FLAIR classification from DICOM metadata
- **Preprocessing**: Multi-channel (4ch) input construction, resampling to 1mm isotropic, z-score normalization
- **Segmentation**: SegResNet (BraTS-trained) via MONAI with sliding window inference
  - Tumor core, whole tumor, enhancing tumor labels
  - Handles missing sequences by replicating available channels
- **Volumetric measurements**: Per-structure volumes in mL and percentages
- **QA checks**:
  - Missing required sequences
  - Voxel spacing inconsistencies
  - Motion artifact detection (edge energy analysis)
  - Incomplete anatomical coverage
  - Slice gap detection
- **Artifacts**: NIfTI segmentation mask + structured JSON report

### Model Weights (Auto-Download)

**No manual weight provisioning required.** The pipeline automatically downloads the
`brats_mri_segmentation` bundle from the [MONAI Model Zoo](https://github.com/Project-MONAI/model-zoo)
on first inference run. The download is cached in a Docker volume (`model_bundles`).

To pre-download before the first patient study (recommended):
```bash
docker compose exec worker python scripts/download_model.py
```

If you have your own custom-trained weights, set `custom_weights_path` in
`backend/app/usecases/brain_mri/model/inference_config.yaml` and the pipeline
will use those instead.

## Routing Engine

The routing engine determines which use cases to run for each study:

1. **DICOM tag matching**: BodyPartExamined, StudyDescription, SeriesDescription
2. **Per-use-case rules**: Each use case defines patterns in `routing_rules.yaml`
3. **Site overrides**: `configs/sites/<site_id>.yaml` can override or extend rules
4. **Multi-use-case**: A single study can route to multiple use cases

## Operational Notes

### Database Migrations
```bash
docker compose exec backend alembic upgrade head
docker compose exec backend alembic revision --autogenerate -m "description"
```

### Monitoring Worker
```bash
docker compose logs -f worker
```

### Scaling Workers
```bash
docker compose up -d --scale worker=3
```

### MinIO Console
Access at http://localhost:9001 to browse stored artifacts.

### Backup
- PostgreSQL: `docker compose exec postgres pg_dump -U mri_admin mri_platform > backup.sql`
- MinIO: Use `mc` CLI or S3-compatible backup tools
- Orthanc: Volume-level backup of `orthanc_data`

## Technology Stack

| Component | Technology |
|-----------|-----------|
| PACS | Orthanc + DICOMweb |
| Backend | FastAPI + Python 3.11 |
| Task Queue | Celery + Redis |
| Database | PostgreSQL 16 |
| Object Storage | MinIO |
| AI Framework | PyTorch + MONAI |
| Model Architecture | SegResNet (BraTS, auto-downloaded from MONAI Model Zoo) |
| DICOM Processing | SimpleITK + NiBabel + pydicom |
| Frontend | Next.js 14 + Tailwind CSS |
| DICOM Viewer | OHIF (via Orthanc) |
| Reverse Proxy | Nginx |
| Deployment | Docker Compose (GPU-enabled) |
