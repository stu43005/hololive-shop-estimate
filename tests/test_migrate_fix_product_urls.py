from pathlib import Path

import pytest

from estimator_king.database.repository import ProductStateRepository
from scripts.migrate_2026_05_30_fix_product_urls import migrate

OLD_TS = "2020-01-01T00:00:00Z"


def _insert_product(repo, key, failures, misses):
    repo.connection.execute(
        "INSERT INTO products (external_key, store_id, product_id, product_url, "
        "content_hash, normalizer_version, created_at, updated_at, "
        "consecutive_failures, consecutive_sitemap_misses) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (key, "s", key.split(":")[1], f"https://x/products/{key.split(':')[1]}",
         "h", 2, OLD_TS, OLD_TS, failures, misses),
    )


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "state.db")


def _seed(db_path: str) -> None:
    with ProductStateRepository(db_path) as repo:
        _insert_product(repo, "s:1", failures=2, misses=3)   # affected
        _insert_product(repo, "s:2", failures=0, misses=0)   # control
        repo.enqueue_url("s", "https://x/products/1")
        repo.enqueue_url("s", "https://x/products/2")


def test_migrate_purges_queue_and_resets_only_affected_rows(db_path: str) -> None:
    _seed(db_path)

    queue_deleted, rows_reset = migrate(db_path)

    assert queue_deleted == 2
    assert rows_reset == 1  # only the affected row

    with ProductStateRepository(db_path) as repo:
        assert repo.peek_all("s") == []  # queue purged
        affected = repo.get_by_external_key("s:1")
        assert affected is not None
        assert affected.consecutive_failures == 0
        assert affected.consecutive_sitemap_misses == 0

        control_row = repo.connection.execute(
            "SELECT consecutive_failures, consecutive_sitemap_misses, updated_at "
            "FROM products WHERE external_key = ?", ("s:2",),
        ).fetchone()
        assert control_row["consecutive_failures"] == 0
        assert control_row["consecutive_sitemap_misses"] == 0
        assert control_row["updated_at"] == OLD_TS  # untouched


def test_migrate_is_idempotent(db_path: str) -> None:
    _seed(db_path)
    migrate(db_path)

    queue_deleted, rows_reset = migrate(db_path)

    assert queue_deleted == 0
    assert rows_reset == 0
    with ProductStateRepository(db_path) as repo:
        control_row = repo.connection.execute(
            "SELECT updated_at FROM products WHERE external_key = ?", ("s:2",),
        ).fetchone()
        assert control_row["updated_at"] == OLD_TS
