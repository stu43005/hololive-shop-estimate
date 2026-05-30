from pathlib import Path

import pytest

from estimator_king.database.repository import ProductStateRepository
from scripts.clean_crawl_queue import clean


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "state.db")


def _seed(db_path: str) -> None:
    with ProductStateRepository(db_path) as repo:
        repo.enqueue_url("vspo", "https://store.vspo.jp/products/a")
        repo.enqueue_url("vspo", "https://store.vspo.jp/en/products/a")
        repo.enqueue_url("hololive", "https://shop.hololivepro.com/products/b")


def test_clean_purges_all_by_default(db_path: str) -> None:
    _seed(db_path)
    before, deleted = clean(db_path)
    assert before == 3
    assert deleted == 3
    with ProductStateRepository(db_path) as repo:
        assert repo.queue_size() == 0


def test_clean_dry_run_keeps_queue(db_path: str) -> None:
    _seed(db_path)
    before, deleted = clean(db_path, dry_run=True)
    assert before == 3
    assert deleted == 0
    with ProductStateRepository(db_path) as repo:
        assert repo.queue_size() == 3


def test_clean_store_scope(db_path: str) -> None:
    _seed(db_path)
    before, deleted = clean(db_path, store_id="vspo")
    assert before == 2
    assert deleted == 2
    with ProductStateRepository(db_path) as repo:
        assert repo.queue_size() == 1
        assert repo.queue_size("hololive") == 1
