"""Migrations apply once, refuse edits, and produce time-ordered UUIDv7 IDs."""

from __future__ import annotations

import pytest

from empire_platform.devdb import migrate


def test_rerunning_migrations_is_a_no_op(cluster):
    assert migrate(cluster) == []


def test_edited_migration_is_refused(cluster):
    with cluster.connect("empire_migrator") as conn:
        original = conn.execute(
            "SELECT sha256 FROM public.schema_migrations WHERE name = '0001_platform_core.sql'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE public.schema_migrations SET sha256 = 'edited' WHERE name = '0001_platform_core.sql'"
        )
        conn.commit()
    try:
        with pytest.raises(SystemExit, match="edited"):
            migrate(cluster)
    finally:
        with cluster.connect("empire_migrator") as conn:
            conn.execute(
                "UPDATE public.schema_migrations SET sha256 = %s WHERE name = '0001_platform_core.sql'",
                (original,),
            )
            conn.commit()


def test_uuid_v7_has_version_7_and_is_time_ordered(api):
    ids = [api.execute("SELECT platform.uuid_v7()").fetchone()[0] for _ in range(50)]
    assert all(value.version == 7 for value in ids)
    assert all(value.variant == "specified in RFC 4122" for value in ids)
    timestamps = [value.int >> 80 for value in ids]
    assert timestamps == sorted(timestamps)
