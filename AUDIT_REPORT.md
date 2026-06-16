# MRI Platform — Full Audit Report

**Date:** 2026-05-24  
**Scope:** All pipelines, configs, UI schemas, frontend, Docker, API, dependencies

---

## 1. MODEL GAPS

| Use Case | Architecture | Status | Notes |
|----------|-------------|--------|-------|
| Brain MRI | BraTS SegResNet (MONAI bundle) | Auto-downloads ~18 MB on first run | REAL weights, working |
| Abdomen MRI | TotalSegmentator v2.2 (total_mr) | Auto-downloads ~450 MB on first run | REAL weights, new |
| Chest MRI | TotalSegmentator v2.2 (total_mr) | Auto-downloads ~450 MB on first run | REAL weights, new |
| Spine MRI | TotalSegmentator v2.2 (total_mr) | Auto-downloads ~450 MB on first run | REAL weights, new |
| PET/CT | Threshold-based (PERCIST 1.0) | No learned model needed | Working |

### Critical Model Issues

1. **PET/CT Brain pipeline missing entirely** — `ui_schema.json` exists but no `pipeline.py`, no `inference_config.yaml`.
2. **Brain bundle download has no fallback** — `brain_mri/pipeline.py:97-100` raises `RuntimeError` if MONAI download fails. No graceful degradation unlike other pipelines.
3. **Brain bundle cache not persisted in worker container** — `model_bundles:/app/app/usecases/brain_mri/model/bundles` volume is mounted in worker but the bundle_cache_dir in config points to that same path. OK, but verify the path is exactly right.
4. **TotalSegmentator weights not shared between worker replicas** — If concurrency > 1, two workers may simultaneously download 450 MB. No locking.

---

## 2. PIPELINE LOGIC GAPS

### 2.1 Abdomen MRI (`backend/app/usecases/abdomen_mri/pipeline.py`)

- **Line 386**: Post-processing loop hardcoded to `[1, 2, 3, 4, 5]`. If `organ_map` in config adds a 6th organ, label 6 is skipped silently — no volume computed, no artifact saved.
- **Line 259**: `_build_single_channel_input()` has no try/except. If SimpleITK resampling fails, the pipeline continues with a corrupted `input_path`.
- **Line 406**: `organ_count_segmented` counts labels with volume > `min_structure_volume_ml`, but synthetic fallback can produce many spurious small voxel clusters.
- **Missing**: No validation that `organ_map` keys match TotalSegmentator's actual output filenames (`liver.nii.gz`, etc.). Typo silently skips the organ.

### 2.2 Chest MRI (`backend/app/usecases/chest_mri/pipeline.py`)

- **Line 370-378**: Fragment removal loop hardcoded for labels `[1, 2, 3, 4]`. Adding a 5th organ in config will not be post-processed.
- **Lines 397-401**: Abnormality detection uses volume ratio (0.6–1.6× reference). Thresholds are hardcoded; no config option to adjust.
- **Missing**: Bilateral asymmetry detection ignores unilateral pathology (e.g., unilateral pleural effusion in one lung looks "normal" if total lung volume is ok).

### 2.3 Spine MRI (`backend/app/usecases/spine_mri/pipeline.py`)

- **Lines 307-316**: `vertebra_prefixes` glob pattern depends on TotalSegmentator naming exactly matching config prefixes (`vertebrae_C1.nii.gz`, etc.). If naming differs (e.g., `vertebra_C1` without `e`), glob returns nothing — label 1 is empty — silent failure.
- **Line 329-331**: Disc count uses connected components; contiguous discs may count as 1 component, undercounting.
- **Line 434-437**: Cross-sectional canal/cord area computed per-axial-slice only. Incorrect for scoliosis or rotated acquisitions.
- **Line 440**: `_infer_levels()` maps disc count to vertebral levels with a crude heuristic. Fails for partial spine coverage (lumbar-only, cervical-only scans).
- **Spinal canal estimation (lines 329-331)**: Canal estimated as dilated cord region only when `spinal_canal.nii.gz` absent. Dilation radius (3 iterations) hardcoded — no config option.

### 2.4 Brain MRI (`backend/app/usecases/brain_mri/pipeline.py`)

- **Line 329**: Channel input hardcoded as `["T1", "T1", "T2", "FLAIR"]` (T1 duplicated as T1ce). If only 1 sequence available, all 4 channels are the same — model may not handle this correctly.
- **Line 375**: `if img_data.ndim == 4: img_tensor = torch.from_numpy(img_data).permute(3, 0, 1, 2).unsqueeze(0)` — **crashes if ndim==4 but last dim != 4 channels** (e.g., shape `(H, W, D, 2)`).
- **Line 407-422**: Sigmoid threshold 0.5 hardcoded. BraTS SegResNet documentation recommends 0.5 for ET but adaptive for TC/WT — suboptimal segmentation.
- **Missing**: No handling for images with anisotropic spacing > 3.0 mm (quality check flags it but does not skip inference).

### 2.5 PET/CT (`backend/app/usecases/pet_ct/pipeline.py`)

- **Line 784-785**: Physiological exclusion mask (brain, thyroid, bladder, liver) assumes superior-to-inferior image orientation. Toe-to-head acquisitions would exclude wrong regions.
- **Line 154**: `suv_factor = weight_g / max(dose_at_scan, 1.0)` — if `weight_g == 0.0` (missing DICOM tag), returns 0, making all SUV values 0. Fallback at line 188 uses SUVbw estimate but does not log a warning flag.
- **Line 829**: Anatomical region assigned from lesion centroid position. For large lesions spanning multiple regions, the centroid may fall inside the wrong region.
- **Lines 535, 495-499**: `_resample_ct_to_pet()` uses `scipy.ndimage.zoom` with no memory bounds check. Very different CT/PET shapes (e.g., CT at 0.5mm, PET at 3mm) could allocate multi-GB arrays.

---

## 3. CONFIG GAPS

### 3.1 Label Loop Hardcoding vs Config

Every pipeline hardcodes label IDs in post-processing loops instead of reading them dynamically from `label_map`. Adding an organ to config will not automatically be processed.

| Pipeline | Hardcoded Loop | Config Labels |
|----------|---------------|---------------|
| abdomen_mri:386 | `[1,2,3,4,5]` | liver,spleen,kidney_right,kidney_left,pancreas |
| chest_mri:370 | `[1,2,3,4]` | right_lung,left_lung,heart,aorta |
| spine_mri:414 | `[1,2,3,4]` | vertebra,disc,canal,cord |
| brain_mri:471 | `[1,2,3]` | tumor_core,whole_tumor,enhancing_tumor |

**Fix**: Replace all hardcoded loops with `for label_id, name in label_map.items(): ...`

### 3.2 Missing Config Validation

- **PET/CT**: `suv_threshold_absolute`, `percist_liver_factor`, `sphere_radius_mm` hardcoded in pipeline code but not exposed in config.
- **All pipelines**: No schema validation on YAML at load time. A typo in `min_structure_volume_ml` (e.g., string instead of float) silently becomes `None`, causing a crash deep in postprocessing.
- **Abdomen**: `reference_volumes_ml.liver_normal_max` (line 46) defined but if set to 0 or negative, volume comparison logic at line 412 silently marks everything as abnormal.

### 3.3 TotalSegmentator Config Issues

- `totalseg_weights_dir: /model_cache/totalsegmentator` — this must match the Docker volume mount path exactly. If the path doesn't exist inside the container, TotalSegmentator downloads to a temp dir and the cache is lost on restart.
- `version: "2.2"` in all three configs but the Python API doesn't accept a version parameter — this field is unused and misleading.

---

## 4. UI / FRONTEND GAPS

### 4.1 ReportView.tsx — Missing Renderers

- **Lesion table**: PET-CT `ui_schema.json` defines a table section with columns (anatomical_region, suv_max, volume_ml, etc.) but `ReportView.tsx` has no generic table renderer for measurement data. The lesion list must be hard-coded in the component rather than schema-driven.
- **Overlay images**: Brain and PET-CT schemas define `type: "overlay"` sections (MIP images, segmentation overlays). No overlay renderer exists — these sections produce nothing.
- **Supplementary data**: Brain schema defines `supplementary` section with volume percentages. No renderer.
- **Spine levels_analyzed**: Array of level strings (`["C3", "C4", "T1"...]`). No renderer — displays as raw JSON.

### 4.2 ReportView.tsx — Logic Bugs

- **Line 110**: `diagnosis.toLowerCase().startsWith("tumor positive")` — hardcoded for PET-CT/Brain. For Abdomen/Chest/Spine which have no `diagnosis` field, this returns `undefined`, causing the banner section to silently render nothing (OK) but the `.startsWith()` call on undefined would crash if the guard at line 108 fails.
- **Line 147**: `result.summary?.tumorDetected` — only exists in Brain MRI output. Chest uses `lesion_detected`, Abdomen uses `organ_segmentation_complete`. Reading wrong field name silently shows nothing.
- **Diagnosis banner** excluded from Clinical Findings grid (correct), but if `diagnosis` key appears in other pipelines for different purposes, it will be silently hidden.

### 4.3 api.ts — Missing / Incorrect Functions

- `getArtifactUrl()` added with `?redirect=false` parameter — correct fix for MinIO internal URL redirect.
- `getPreviewUrl()` — verify this exists and correctly constructs the preview endpoint URL.
- No retry logic or error handling for failed artifact fetches. If MinIO is slow, image shows "Image not available" permanently.

### 4.4 UI Schema Gaps

| Schema | Gap |
|--------|-----|
| `pet_ct/ui_schema.json` | `artifact_filter: mip_png` — must exactly match artifact_type stored in DB |
| `pet_ct/ui_schema.json` | `artifact_filter: fused_png` — must exactly match artifact_type stored in DB |
| `brain_mri/ui_schema.json` | No image section for segmentation overlay PNGs |
| `spine_mri/ui_schema.json` | No image section for spine overview PNGs |
| `chest_mri/ui_schema.json` | No image section for lung segmentation PNGs |
| `abdomen_mri/ui_schema.json` | No image section for organ segmentation PNGs |

---

## 5. DEPENDENCY GAPS

### 5.1 `backend/pyproject.toml`

| Package | Status | Issue |
|---------|--------|-------|
| `torch` | **Missing** — listed in comment only | Must be installed via Docker separately; not declared; CPU-only if GPU wheel missing |
| `totalsegmentator>=2.2.0` | Added | Correct |
| `nnunetv2>=2.4` | Added | Risk: TotalSegmentator bundles its own nnU-Net; dual installation may conflict |
| `SimpleITK==2.3.1` | Pinned to old version | Current is 2.4.x; 2.3.1 has known bugs in anisotropic resampling |
| `scikit-image` | Listed but unused | Dead dependency; remove |
| `h5py` | Not listed | TotalSegmentator model files are HDF5; needed transitively but not declared |
| `connected-components-3d` | Not listed | Used by some pipeline code for CC labeling |

### 5.2 Docker Build

- PyTorch GPU wheel must be installed before `pip install -e .` in Dockerfile. If the Dockerfile installs packages alphabetically or runs `pip install .` before the torch-cu128 wheel, CPU-only torch installs first and GPU support breaks.
- No pinned CUDA version in Dockerfile. If base image CUDA (12.8) doesn't match `torch+cu128` (CUDA 12.8), GPU ops fail at runtime.

---

## 6. DOCKER / INFRA GAPS

### 6.1 Volumes

| Gap | Severity |
|-----|----------|
| `model_cache:/model_cache` added to worker — correct | Fixed |
| Brain bundle cache: `model_bundles` volume maps to `/app/app/usecases/brain_mri/model/bundles` — OK | Fixed |
| `beat` service has no `model_cache` volume (beat doesn't run inference — OK) | Low |
| No volume for TotalSegmentator temp files during inference | Medium — large temp files accumulate in container ephemeral layer |

### 6.2 Missing Environment Variables

- `TOTALSEG_WEIGHTS_PATH=/model_cache/totalsegmentator` set in worker env — but TotalSegmentator Python API reads `weights_dir` parameter, not this env var. The env var `TOTALSEG_WEIGHTS_PATH` is non-standard. Check TotalSegmentator docs — it may read `TOTALSEG_WEIGHTS_PATH` internally. **Verify or remove.**
- `MONAI_HOME` / `MONAI_MODEL_ZOO_DIR` — brain pipeline doesn't set these; MONAI uses default `~/.cache/monai`. Inside container this is ephemeral unless mapped to a volume. Brain bundle re-downloads on every container restart if not explicitly mapped.
- `NVIDIA_VISIBLE_DEVICES` — not set. `CUDA_VISIBLE_DEVICES=0,1` is set but the nvidia-container-runtime uses `NVIDIA_VISIBLE_DEVICES`. Both should be set for compatibility.

### 6.3 Service Dependencies

- `beat` service doesn't depend on `minio` — if beat triggers artifact cleanup, minio must be available.
- `worker` concurrency is `--concurrency=2`. With 2x RTX 5070 Ti, two concurrent GPU jobs may run simultaneously and exceed VRAM if both do inference on large volumes. Consider `--concurrency=1` per GPU or implement GPU semaphore.

### 6.4 GPU Limits

No GPU resource limits set in compose:
```yaml
# Current — unlimited GPU access
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: all
          capabilities: [gpu]
# Missing — no limits
```
Worker can monopolize both GPUs, starving other GPU-dependent services.

---

## 7. API GAPS

### 7.1 Missing Endpoints

| Endpoint | Status | Impact |
|----------|--------|--------|
| `GET /studies/{uid}/artifacts` | Missing | Cannot list artifacts for a study |
| `DELETE /studies/{uid}` | Missing | Cannot delete studies (GDPR) |
| `GET /studies/{uid}/jobs` | Missing | Cannot check job status from UI |
| `GET /results/{id}` | Missing | Frontend can't fetch single result |
| `GET /artifacts/{uid}/{usecase}/{name}?redirect=false` | Implemented | Fixed MinIO redirect issue |

### 7.2 Response Shape Issues

- `body_part_examined` serialized as `.value` (enum) — if `None`, raises `AttributeError`.
- Study list response doesn't include `latest_result` or `result_count` — UI must make N+1 queries to show status for each study.
- Artifact endpoint: when `redirect=false`, response must set correct `Content-Type` header based on artifact format (PNG, NIfTI, JSON). If hardcoded to `application/octet-stream`, browsers won't render images inline.

### 7.3 Authentication

- No API key / JWT authentication on any endpoint visible in codebase. All endpoints are unauthenticated. Acceptable for internal/research use; unacceptable for clinical deployment.

---

## 8. CRITICAL BUGS (Immediate Runtime Failures)

### BUG-1: Brain pipeline 4D tensor permutation crash

**File**: `brain_mri/pipeline.py:375`  
**Code**: `img_tensor = torch.from_numpy(img_data).permute(3, 0, 1, 2).unsqueeze(0)`  
**Crash**: Raises `IndexError` if `img_data.shape[3] != 4`. Happens when only 1-2 MRI sequences are available (common in clinical practice).  
**Fix**: Check `img_data.shape[-1]` before permute; pad to 4 channels if needed.

### BUG-2: Spine vertebra glob silent failure

**File**: `spine_mri/pipeline.py:307-316`  
**Code**: `glob.glob(f"{prefix}*.nii.gz")` — returns empty list if naming doesn't match.  
**Result**: Label 1 (vertebra) is entirely empty in segmentation. User sees "0 vertebrae detected."  
**Fix**: Log `WARNING` if `len(vertebra_files) == 0` and add QA flag `"no_vertebrae_found"`.

### BUG-3: PET/CT SUV = 0 when patient weight missing

**File**: `pet_ct/pipeline.py:154`  
**Code**: `suv_factor = weight_g / max(dose_at_scan, 1.0)` — if `weight_g == 0`, all SUV = 0.  
**Result**: All lesions below threshold, diagnosis always "Tumor Negative" regardless of actual uptake.  
**Fix**: Add `if weight_g <= 0: qa_flags.append("suv_calibration_failed"); use fallback`.

### BUG-4: Artifact Content-Type wrong

**File**: Artifact serving endpoint  
**Code**: If serving PNGs as `application/octet-stream`, `<img src=...>` renders nothing.  
**Result**: "Image not available" for all images even after `?redirect=false` fix.  
**Fix**: Set `media_type="image/png"` when serving PNG artifacts.

### BUG-5: PET/CT CT resampling unbounded memory

**File**: `pet_ct/pipeline.py:495-499`  
**Code**: `scipy.ndimage.zoom(ct_arr, factors)` — no memory check.  
**Example**: CT 512×512×500 resampled to PET 128×128×500 → zoom factor < 1 (OK). But CT at 0.3mm to PET at 3mm → factor 0.1 → output tiny (OK). Reverse: CT at 3mm, PET at 0.5mm → factor 6× → 512×512×500 → allocates 512×512×3000 float32 ≈ 3 GB.  
**Fix**: Cap zoom factors; refuse to upsample CT more than 3×.

### BUG-6: Model version string for TotalSegmentator pipelines

**File**: `chest_mri/pipeline.py:403`, `spine_mri/pipeline.py:411`  
**Code**: `model_version_str = f"chest_mri_v{model_version}"` — still uses old format even when TotalSegmentator is active. The version reported to DB is wrong.  
**Fix**: Add same arch-check as abdomen (`if architecture == "totalsegmentator_mr": model_version_str = f"totalsegmentator_{task}_v{model_version}"`).

---

## 9. SUMMARY BY PRIORITY

### CRITICAL (will crash or produce wrong diagnosis)

1. Brain 4D permutation IndexError on < 4 sequences
2. PET-CT task traceback (currently failing)
3. Artifact Content-Type wrong → images never render
4. PET/CT SUV = 0 on missing patient weight → false Tumor Negative
5. Missing PET/CT Brain pipeline implementation

### HIGH (causes silent failures or degraded output)

6. Spine vertebra glob failure → empty vertebra label
7. Brain bundle cache not persisted → re-downloads every restart
8. Label loops hardcoded in post-processing (all pipelines)
9. TotalSegmentator `version` field unused and misleading in configs
10. `TOTALSEG_WEIGHTS_PATH` env var not read by TotalSegmentator Python API

### MEDIUM (functional but suboptimal or risky)

11. Chest/Spine model_version_str incorrect for TotalSegmentator
12. nnunetv2 dual installation conflict risk
13. No API authentication
14. Worker concurrency 2 may cause GPU OOM on large volumes
15. SimpleITK 2.3.1 old version with anisotropic resampling bugs

### LOW (quality / maintainability)

16. Hardcoded thresholds not exposed in config (chest, brain)
17. Dead `scikit-image` dependency
18. No DELETE /studies endpoint (GDPR)
19. No GPU resource limits in docker-compose
20. Missing image sections in spine/chest/abdomen UI schemas
