"""Integration test conftest — mock heavy infrastructure modules before app import."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Mock heavy infrastructure modules that require pydicom, nibabel, torch, etc.
# This allows us to import the FastAPI app without having all infra deps installed.
_MODULES_TO_MOCK = [
    "pydicom",
    "pydicom.dataset",
    "pydicom.uid",
    "nibabel",
    "SimpleITK",
    "monai",
    "monai.networks",
    "monai.networks.nets",
    "torch",
    "scipy",
    "scipy.ndimage",
    "PIL",
    "PIL.Image",
    "prometheus_client",
    "prometheus_fastapi_instrumentator",
]

for mod_name in _MODULES_TO_MOCK:
    sys.modules.setdefault(mod_name, MagicMock())

# Also mock the specific infrastructure modules that pull in heavy deps
sys.modules.setdefault("app.infrastructure.queue.tasks", MagicMock())
sys.modules.setdefault("app.infrastructure.queue.celery_app", MagicMock())
