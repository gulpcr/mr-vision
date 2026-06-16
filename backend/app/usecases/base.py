from __future__ import annotations

import abc
from typing import Any

from app.domain.interfaces import PACSClient
from app.domain.models import Series, Study


class BasePipeline(abc.ABC):
    """Base class that all use-case pipelines must inherit from.

    Provides the standardized three-phase interface:
    - preprocess: download, validate, and prepare input data
    - infer: run model inference
    - postprocess: compute measurements, generate artifacts, build result dict
    """

    @abc.abstractmethod
    def preprocess(
        self,
        study: Study,
        series: list[Series],
        working_dir: str,
        pacs: PACSClient,
        event_loop: Any = None,
    ) -> dict[str, Any]:
        """Download and prepare input data for inference.

        Returns a dict of preprocessed data paths and metadata.
        """
        ...

    @abc.abstractmethod
    def infer(self, preprocessed: dict[str, Any], working_dir: str) -> dict[str, Any]:
        """Run model inference on preprocessed data.

        Returns a dict of raw model outputs.
        """
        ...

    @abc.abstractmethod
    def postprocess(
        self, inference_output: dict[str, Any], working_dir: str
    ) -> dict[str, Any]:
        """Process inference outputs into final results.

        Must return a dict with these keys:
        - summary: dict with human-readable result summary
        - measurements: dict with quantitative measurements
        - qa_flags: list of QA flag strings
        - qa_details: dict with detailed QA information
        - model_version: str
        - model_checksum: str
        - artifacts: list of dicts with keys:
            - name: str
            - artifact_type: str
            - local_path: str
            - content_type: str
        """
        ...
