"""Persistence-layer regression tests for the ct_face_neck plugin.

Verifies — against a real SQLAlchemy schema (SQLite in-memory; the ORM models
use only String/JSON/Boolean/Integer/Float/DateTime columns, no Postgres-only
types, so this is a faithful stand-in for the production Postgres schema) —
that registering ct_face_neck's UseCase record and persisting/reading its
Result records via the real repository classes doesn't break the shared
database contracts or interfere with existing MRI use-case records.

The qa_flags round-trip test in particular guards a real contract: PgResultRepository
._to_domain() silently drops any qa_flag string that isn't a registered QAFlag
member (ValueError from QAFlag(f) is caught and swallowed) — see domain/enums.py.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.enums import QAFlag
from app.domain.models import Result, ResultArtifact, UseCase
from app.infrastructure.database.models import Base, StudyRecord
from app.infrastructure.database.repositories import (
    PgResultRepository,
    PgUseCaseRegistryRepository,
)

CT_FACE_NECK_QA_FLAGS = [
    "insufficient_slices",
    "excessive_slice_thickness",
    "preview_render_failed",
    "vlm_unavailable",
    "low_confidence_extraction",
]


def _study_uid(prefix: str) -> str:
    return f"{prefix}.{uuid.uuid4().int % 10**9}"


@pytest_asyncio.fixture
async def db_session():
    """A real (in-memory SQLite) async session against the actual ORM schema —
    not mocked — so these tests exercise a genuine SQL round-trip rather than
    asserting on mock call arguments.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


async def _seed_study(session, study_uid: str) -> None:
    session.add(StudyRecord(study_instance_uid=study_uid, modality="CT"))
    await session.flush()


class TestUseCaseRegistryPersistence:
    @pytest.mark.asyncio
    async def test_ct_face_neck_registers_alongside_existing_mri_usecase(self, db_session):
        repo = PgUseCaseRegistryRepository(db_session)

        brain_mri = UseCase(
            name="brain_mri",
            version="1.0.0",
            supported_body_parts=["BRAIN", "HEAD"],
            required_sequences=["T1", "FLAIR"],
            model_type="segresnet",
        )
        ct_face_neck = UseCase(
            name="ct_face_neck",
            version="1.0.0",
            supported_body_parts=["HEAD", "NECK"],
            required_sequences=["CT"],
            model_type="Gemini VLM structured extraction (no local segmentation/classification network)",
            enabled=False,
        )
        await repo.save(brain_mri)
        await repo.save(ct_face_neck)
        await db_session.commit()

        names = {uc.name for uc in await repo.list_all()}
        assert {"brain_mri", "ct_face_neck"} <= names

        fetched = await repo.get_by_name("ct_face_neck")
        assert fetched.supported_body_parts == ["HEAD", "NECK"]
        assert fetched.required_sequences == ["CT"]
        assert fetched.enabled is False

        # Existing MRI usecase record is untouched by ct_face_neck's insertion.
        fetched_brain = await repo.get_by_name("brain_mri")
        assert fetched_brain.supported_body_parts == ["BRAIN", "HEAD"]
        assert fetched_brain.required_sequences == ["T1", "FLAIR"]


class TestResultPersistence:
    @pytest.mark.asyncio
    async def test_qa_flags_round_trip_without_silent_loss(self, db_session):
        """Every qa_flag string pipeline.py's postprocess() can emit must survive
        save() -> get() intact. Fails if a new flag is added to pipeline.py without
        a matching QAFlag member (the exact bug found while writing this test).
        """
        study_uid = _study_uid("1.2")
        await _seed_study(db_session, study_uid)
        repo = PgResultRepository(db_session)

        result = Result(
            study_instance_uid=study_uid,
            usecase_name="ct_face_neck",
            summary={"conclusion": ["AI structured extraction unavailable for this study."]},
            measurements={"findings": {}},
            qa_flags=list(CT_FACE_NECK_QA_FLAGS),
            model_version="ct_face_neck_gemini_vlm_v1.0.0",
            model_checksum="abc123",
            artifacts=[],
        )
        await repo.save(result)
        await db_session.commit()

        fetched = await repo.get_by_study_and_usecase(study_uid, "ct_face_neck")
        assert fetched is not None
        fetched_values = {f.value for f in fetched.qa_flags}
        assert fetched_values == set(CT_FACE_NECK_QA_FLAGS), (
            "one or more ct_face_neck qa_flags were silently dropped on read — "
            "they must be registered as QAFlag members in domain/enums.py"
        )

    @pytest.mark.asyncio
    async def test_version_increment_and_is_latest_flip(self, db_session):
        study_uid = _study_uid("1.3")
        await _seed_study(db_session, study_uid)
        repo = PgResultRepository(db_session)

        r1 = Result(
            study_instance_uid=study_uid, usecase_name="ct_face_neck",
            model_version="v1", model_checksum="c1", qa_flags=[],
        )
        await repo.save(r1)
        await db_session.commit()

        r2 = Result(
            study_instance_uid=study_uid, usecase_name="ct_face_neck",
            model_version="v1", model_checksum="c2", qa_flags=[],
        )
        await repo.save(r2)
        await db_session.commit()

        assert r2.version == 2
        latest = await repo.get_by_study_and_usecase(study_uid, "ct_face_neck")
        assert latest.model_checksum == "c2"
        assert latest.is_latest is True

        versions = await repo.list_versions(study_uid, "ct_face_neck")
        assert [v.version for v in versions] == [2, 1]

    @pytest.mark.asyncio
    async def test_coexists_with_existing_mri_usecase_result_on_same_study(self, db_session):
        """A study can be routed to multiple use cases (e.g. a combined protocol).
        Saving a ct_face_neck result must not disturb an existing brain_mri result
        for the same study_instance_uid.
        """
        study_uid = _study_uid("1.4")
        await _seed_study(db_session, study_uid)
        repo = PgResultRepository(db_session)

        brain_result = Result(
            study_instance_uid=study_uid, usecase_name="brain_mri",
            summary={"tumor_detected": False}, measurements={"total_volume": 1200.0},
            qa_flags=[QAFlag.MOTION_ARTIFACT.value],
            model_version="brats_v1", model_checksum="bc1",
        )
        await repo.save(brain_result)

        ct_result = Result(
            study_instance_uid=study_uid, usecase_name="ct_face_neck",
            summary={"conclusion": ["No evidence of residual or recurrent mass."]},
            measurements={"findings": {}},
            qa_flags=list(CT_FACE_NECK_QA_FLAGS),
            model_version="ct_face_neck_gemini_vlm_v1.0.0", model_checksum="cf1",
        )
        await repo.save(ct_result)
        await db_session.commit()

        fetched_brain = await repo.get_by_study_and_usecase(study_uid, "brain_mri")
        fetched_ct = await repo.get_by_study_and_usecase(study_uid, "ct_face_neck")

        assert fetched_brain is not None and fetched_brain.model_checksum == "bc1"
        assert fetched_ct is not None and fetched_ct.model_checksum == "cf1"
        assert {f.value for f in fetched_brain.qa_flags} == {"motion_artifact"}
        assert {f.value for f in fetched_ct.qa_flags} == set(CT_FACE_NECK_QA_FLAGS)

        by_study = await repo.list_by_study(study_uid)
        assert {r.usecase_name for r in by_study} == {"brain_mri", "ct_face_neck"}

    @pytest.mark.asyncio
    async def test_artifacts_with_new_artifact_type_round_trip(self, db_session):
        """pipeline.py's postprocess() emits a new artifact_type ('ct_preview_png')
        not used by any other use case — confirm the generic JSON artifacts column
        round-trips it without requiring a schema change.
        """
        study_uid = _study_uid("1.5")
        await _seed_study(db_session, study_uid)
        repo = PgResultRepository(db_session)

        result = Result(
            study_instance_uid=study_uid, usecase_name="ct_face_neck",
            model_version="v1", model_checksum="c1", qa_flags=[],
            artifacts=[
                ResultArtifact(
                    name="extraction", artifact_type="report_json",
                    storage_path=f"{study_uid}/ct_face_neck/extraction.json",
                    content_type="application/json", size_bytes=512,
                ),
                ResultArtifact(
                    name="slice_0002_soft_tissue.png", artifact_type="ct_preview_png",
                    storage_path=f"{study_uid}/ct_face_neck/slice_0002_soft_tissue.png",
                    content_type="image/png", size_bytes=4096,
                ),
            ],
        )
        await repo.save(result)
        await db_session.commit()

        fetched = await repo.get_by_study_and_usecase(study_uid, "ct_face_neck")
        assert len(fetched.artifacts) == 2
        assert {a.artifact_type for a in fetched.artifacts} == {"report_json", "ct_preview_png"}
