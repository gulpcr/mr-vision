"""Integration test conftest — mock heavy infrastructure modules before app import."""
from __future__ import annotations

import importlib.util
import sys
from unittest.mock import MagicMock

# Mock heavy infrastructure modules that require pydicom, nibabel, torch, etc.,
# so the FastAPI app can be imported in environments without all infra deps
# installed. Only mock a module if it isn't actually installed — when the real
# package IS present (as it is in this image), setdefault() would otherwise
# wedge a MagicMock into sys.modules for the rest of the pytest process,
# silently breaking any later unit test (e.g. coronary_cta) that does a real
# `import scipy` / `from scipy import ndimage`.
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

def _is_installed(mod_name: str) -> bool:
    try:
        return importlib.util.find_spec(mod_name) is not None
    except ModuleNotFoundError:
        # Raised when a dotted submodule's parent package isn't installed
        # either (e.g. "scipy.ndimage" when "scipy" itself is missing).
        return False


for mod_name in _MODULES_TO_MOCK:
    if mod_name not in sys.modules and not _is_installed(mod_name):
        sys.modules[mod_name] = MagicMock()

# Also mock the specific infrastructure modules that pull in heavy deps —
# same reasoning and same guard as above: only when genuinely unimportable
# (e.g. no Celery broker deps installed), never unconditionally. An
# unconditional mock here previously made `app.infrastructure.queue.tasks`
# a permanent no-op MagicMock for the rest of the pytest process — including
# for unit tests that import real functions from it directly (_write_audit).
for mod_name in ("app.infrastructure.queue.tasks", "app.infrastructure.queue.celery_app"):
    if mod_name not in sys.modules and not _is_installed(mod_name):
        sys.modules[mod_name] = MagicMock()
