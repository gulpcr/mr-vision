from __future__ import annotations

import abc
from typing import Any, Protocol

from app.domain.hl7_models import ParsedMessage, ProcessResult
from app.domain.models import (
    AuditEntry,
    JobRun,
    Result,
    RoutingRule,
    Series,
    Study,
    UseCase,
)


class StudyRepository(abc.ABC):
    @abc.abstractmethod
    async def save(self, study: Study) -> Study: ...

    @abc.abstractmethod
    async def get_by_uid(self, study_instance_uid: str) -> Study | None: ...

    @abc.abstractmethod
    async def list_studies(
        self, offset: int = 0, limit: int = 50, filters: dict[str, Any] | None = None
    ) -> list[Study]: ...

    @abc.abstractmethod
    async def count(self, filters: dict[str, Any] | None = None) -> int: ...

    @abc.abstractmethod
    async def update(self, study: Study) -> Study: ...


class SeriesRepository(abc.ABC):
    @abc.abstractmethod
    async def save(self, series: Series) -> Series: ...

    @abc.abstractmethod
    async def get_by_uid(self, series_instance_uid: str) -> Series | None: ...

    @abc.abstractmethod
    async def list_by_study(self, study_instance_uid: str) -> list[Series]: ...

    @abc.abstractmethod
    async def save_many(self, series_list: list[Series]) -> list[Series]: ...


class JobRepository(abc.ABC):
    @abc.abstractmethod
    async def save(self, job: JobRun) -> JobRun: ...

    @abc.abstractmethod
    async def get_by_id(self, job_id: str) -> JobRun | None: ...

    @abc.abstractmethod
    async def list_by_study(self, study_instance_uid: str) -> list[JobRun]: ...

    @abc.abstractmethod
    async def update(self, job: JobRun) -> JobRun: ...

    @abc.abstractmethod
    async def list_jobs(
        self, offset: int = 0, limit: int = 50, filters: dict[str, Any] | None = None
    ) -> list[JobRun]: ...


class ResultRepository(abc.ABC):
    @abc.abstractmethod
    async def save(self, result: Result) -> Result: ...

    @abc.abstractmethod
    async def get_by_study_and_usecase(
        self, study_instance_uid: str, usecase_name: str
    ) -> Result | None: ...

    @abc.abstractmethod
    async def get_by_id(self, result_id: str) -> Result | None: ...

    @abc.abstractmethod
    async def list_by_study(self, study_instance_uid: str) -> list[Result]: ...

    @abc.abstractmethod
    async def get_by_study_usecase_version(
        self, study_instance_uid: str, usecase_name: str, version: int
    ) -> Result | None: ...

    @abc.abstractmethod
    async def list_versions(
        self, study_instance_uid: str, usecase_name: str
    ) -> list[Result]: ...


class UseCaseRegistryRepository(abc.ABC):
    @abc.abstractmethod
    async def save(self, usecase: UseCase) -> UseCase: ...

    @abc.abstractmethod
    async def get_by_name(self, name: str) -> UseCase | None: ...

    @abc.abstractmethod
    async def list_all(self) -> list[UseCase]: ...

    @abc.abstractmethod
    async def update(self, usecase: UseCase) -> UseCase: ...


class AuditRepository(abc.ABC):
    @abc.abstractmethod
    async def save(self, entry: AuditEntry) -> AuditEntry: ...

    @abc.abstractmethod
    async def list_by_entity(
        self, entity_type: str, entity_id: str, limit: int = 100
    ) -> list[AuditEntry]: ...


class ArtifactStore(abc.ABC):
    @abc.abstractmethod
    async def put(self, path: str, data: bytes, content_type: str = "application/octet-stream") -> str: ...

    @abc.abstractmethod
    async def get(self, path: str) -> bytes: ...

    @abc.abstractmethod
    async def get_presigned_url(self, path: str, expires_secs: int = 3600) -> str: ...

    @abc.abstractmethod
    async def exists(self, path: str) -> bool: ...

    @abc.abstractmethod
    async def delete(self, path: str) -> None: ...


class PACSClient(abc.ABC):
    @abc.abstractmethod
    async def get_study(self, study_instance_uid: str) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def get_series_list(self, study_instance_uid: str) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    async def download_series_as_nifti(
        self, study_instance_uid: str, series_instance_uid: str, output_path: str
    ) -> str: ...

    @abc.abstractmethod
    async def download_series_dicoms(
        self, study_instance_uid: str, series_instance_uid: str, output_dir: str
    ) -> list[str]: ...

    @abc.abstractmethod
    async def upload_dicom_instance(self, dicom_bytes: bytes) -> str:
        """Push a single raw DICOM instance into the PACS. Returns its PACS id."""
        ...


class HL7InboundHandler(abc.ABC):
    """Handles one raw inbound HL7 v2 message end to end: parse → map → persist →
    decide the ACK. Implemented in the application layer; driven by the MLLP
    listener (infrastructure). Returns a ProcessResult the listener turns into an
    MLLP ACK; implementations must not raise for message-level errors — they encode
    the failure as an AE/AR ProcessResult so the sender always gets an acknowledgment.
    """

    @abc.abstractmethod
    def handle(self, raw_message: str) -> ProcessResult: ...


class HL7OutboundClient(abc.ABC):
    """Sends an outbound HL7 v2 message (ORU results) to the RIS/EHR over MLLP and
    waits for the application ACK. Implemented in infrastructure; called from the
    post-result Celery hook."""

    @abc.abstractmethod
    def send(self, message: ParsedMessage | str) -> ProcessResult: ...


class UseCasePipeline(Protocol):
    """Contract that every use-case pipeline must implement."""

    def preprocess(
        self, study: Study, series: list[Series], working_dir: str, pacs: PACSClient
    ) -> dict[str, Any]: ...

    def infer(self, preprocessed: dict[str, Any], working_dir: str) -> dict[str, Any]: ...

    def postprocess(
        self, inference_output: dict[str, Any], working_dir: str
    ) -> dict[str, Any]: ...
