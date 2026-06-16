from __future__ import annotations

import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)


class GPUScheduler:
    """Routes tasks to per-GPU Celery queues based on availability."""

    def __init__(self):
        settings = get_settings()
        self._queues = [q.strip() for q in settings.gpu_worker_queues.split(",") if q.strip()]
        self._round_robin_idx = 0

    def get_available_queues(self) -> list[str]:
        return list(self._queues)

    def select_queue(self, usecase_name: str | None = None, priority: int = 0) -> str:
        """Select the best GPU queue for a task.

        Uses round-robin for simplicity. Can be extended with
        actual GPU utilization monitoring.
        """
        if not self._queues:
            return "mri_inference"  # Default queue

        # Round-robin selection
        queue = self._queues[self._round_robin_idx % len(self._queues)]
        self._round_robin_idx += 1

        logger.debug(
            "gpu_queue_selected",
            queue=queue,
            usecase=usecase_name,
            priority=priority,
        )
        return queue

    def get_queue_for_gpu(self, gpu_id: int) -> str:
        """Get the queue name for a specific GPU index."""
        if gpu_id < len(self._queues):
            return self._queues[gpu_id]
        return "mri_inference"
