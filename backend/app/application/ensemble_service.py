from __future__ import annotations

from typing import Any

import structlog
import numpy as np

logger = structlog.get_logger(__name__)


class EnsembleService:
    """Merge predictions from multiple models using various strategies."""

    def majority_vote(self, predictions: list[np.ndarray]) -> np.ndarray:
        """Combine segmentation masks via majority voting."""
        if not predictions:
            raise ValueError("No predictions to ensemble")
        if len(predictions) == 1:
            return predictions[0]

        stacked = np.stack(predictions, axis=0)
        from scipy import stats
        result, _ = stats.mode(stacked, axis=0, keepdims=False)
        return result.astype(predictions[0].dtype)

    def weighted_average(
        self, predictions: list[np.ndarray], weights: list[float] | None = None
    ) -> np.ndarray:
        """Combine probability maps via weighted average."""
        if not predictions:
            raise ValueError("No predictions to ensemble")
        if len(predictions) == 1:
            return predictions[0]

        if weights is None:
            weights = [1.0 / len(predictions)] * len(predictions)

        weights = np.array(weights) / sum(weights)
        result = np.zeros_like(predictions[0], dtype=np.float32)
        for pred, w in zip(predictions, weights):
            result += pred.astype(np.float32) * w
        return result

    def merge_results(
        self,
        results: list[dict[str, Any]],
        strategy: str = "majority_vote",
    ) -> dict[str, Any]:
        """Merge multiple pipeline results into a single result."""
        if not results:
            return {}
        if len(results) == 1:
            return results[0]

        merged = {
            "summary": {"ensemble_strategy": strategy, "model_count": len(results)},
            "measurements": {},
            "qa_flags": [],
            "qa_details": {"individual_results": len(results)},
            "model_version": f"ensemble_{strategy}_{len(results)}",
            "model_checksum": "",
        }

        # Merge measurements by averaging numeric values
        all_keys = set()
        for r in results:
            all_keys.update(r.get("measurements", {}).keys())

        for key in all_keys:
            values = [
                r["measurements"][key]
                for r in results
                if key in r.get("measurements", {})
                and isinstance(r["measurements"][key], (int, float))
            ]
            if values:
                merged["measurements"][key] = sum(values) / len(values)

        # Collect all QA flags
        seen_flags = set()
        for r in results:
            for flag in r.get("qa_flags", []):
                if flag not in seen_flags:
                    merged["qa_flags"].append(flag)
                    seen_flags.add(flag)

        return merged
