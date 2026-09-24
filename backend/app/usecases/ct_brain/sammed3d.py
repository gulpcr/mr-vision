from __future__ import annotations

"""SAM-Med3D promptable segmentation → tumour dimensions (mm), for ct_brain.

Thin per-use-case shim: the actual model (a lazy singleton, kept resident on the GPU
for the life of the worker process) and inference code live in
``app.infrastructure.ml.sammed3d_engine``, shared by every CT use case's measurement
step so the checkpoint is loaded once and reused, never duplicated per plugin. This
file exists only because ``infrastructure/queue/tasks.py`` dynamically imports
``app.usecases.<usecase_name>.sammed3d`` — kept as a stable per-plugin entry point.
"""

from app.infrastructure.ml.sammed3d_engine import segment_and_measure

__all__ = ["segment_and_measure"]
