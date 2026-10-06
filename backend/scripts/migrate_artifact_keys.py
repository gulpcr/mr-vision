"""Move pre-tenancy MinIO artifacts under their tenant prefix.

Artifacts used to be stored at ``{study_uid}/{usecase}/{name}``; they now live at
``{tenant_id}/{study_uid}/{usecase}/{name}`` (see app/domain/storage_keys.py). Readers
fall back to the legacy key, so running this is not required for correctness — but until
it runs, per-tenant prefix operations (listing, purge, bucket policies) miss old objects.

For every result row it copies each legacy-keyed artifact to the tenant-prefixed key,
rewrites ``results_index.artifacts[*].storage_path``, and (with --delete-legacy) removes
the old object. Idempotent: rows already on the new layout are skipped.

Run as the schema OWNER (bypasses Row-Level Security, needed to see every tenant):

    docker compose exec backend python scripts/migrate_artifact_keys.py --dry-run
    docker compose exec backend python scripts/migrate_artifact_keys.py [--delete-legacy]

DATABASE_URL must point at the owner role (it does in the backend container).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sqlalchemy as sa  # noqa: E402
import structlog  # noqa: E402
from minio.commonconfig import CopySource  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.domain.storage_keys import artifact_key  # noqa: E402
from app.infrastructure.storage.client import MinIOArtifactStore  # noqa: E402

logger = structlog.get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    parser.add_argument("--delete-legacy", action="store_true", help="remove old objects after copy")
    args = parser.parse_args()

    owner_url = os.environ.get("DATABASE_URL")
    if not owner_url:
        print("DATABASE_URL (schema owner) is required", file=sys.stderr)
        return 2

    store = MinIOArtifactStore()
    bucket = get_settings().minio_bucket
    from app.infrastructure.tls import db_sync_url

    engine = sa.create_engine(db_sync_url(owner_url))
    moved = skipped = missing = 0

    with engine.begin() as conn:
        rows = conn.execute(sa.text(
            "SELECT id, tenant_id, study_instance_uid, usecase_name, artifacts FROM results_index"
        )).all()
        for row in rows:
            artifacts = list(row.artifacts or [])
            changed = False
            for art in artifacts:
                old = art.get("storage_path") or ""
                name = art.get("name") or old.rsplit("/", 1)[-1]
                new = artifact_key(row.tenant_id, row.study_instance_uid, row.usecase_name, name)
                if not old or old == new or old.startswith(f"{row.tenant_id}/"):
                    skipped += 1
                    continue
                try:
                    store._client.stat_object(bucket, old)
                except Exception:
                    missing += 1
                    logger.warning("artifact_missing", result_id=row.id, key=old)
                    continue
                if not args.dry_run:
                    store._client.copy_object(bucket, new, CopySource(bucket, old))
                    if args.delete_legacy:
                        store._client.remove_object(bucket, old)
                art["storage_path"] = new
                changed = True
                moved += 1
            if changed and not args.dry_run:
                conn.execute(
                    sa.text("UPDATE results_index SET artifacts = CAST(:a AS json) WHERE id = :id"),
                    {"a": json.dumps(artifacts), "id": row.id},
                )

    print(f"moved={moved} skipped={skipped} missing={missing} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
