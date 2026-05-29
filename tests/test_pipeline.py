from datetime import datetime, timedelta, timezone

import pytest

from estimator_king.config_schema import Store
from estimator_king.crawler.pipeline import enqueue_oldest_products, populate_queue_from_sitemap
from estimator_king.database.repository import ProductState, ProductStateRepository


@pytest.fixture
def repo():
    with ProductStateRepository(":memory:") as r:
        yield r


def _store():
    return Store(id="hololive", base_url="https://x", sitemap_url="https://x/sitemap.xml")


def _state(pid, fetched):
    return ProductState(
        external_key=f"hololive:{pid}", store_id="hololive", product_id=str(pid),
        product_url=f"https://x/products/{pid}", content_hash="h", normalizer_version=2,
        last_fetch_success_at=fetched,
    )


def test_enqueue_oldest_products_picks_oldest_within_limit(repo):
    now = datetime.now(tz=timezone.utc)
    repo.upsert(_state(1, now))
    repo.upsert(_state(2, now - timedelta(days=3)))
    repo.upsert(_state(3, None))

    enqueued = enqueue_oldest_products(_store(), repo, limit=2)

    assert enqueued == 2
    queued = {e["product_url"] for e in repo.peek_all("hololive")}
    assert queued == {"https://x/products/3", "https://x/products/2"}  # NULL + oldest


def test_enqueue_oldest_products_limit_zero_is_noop(repo):
    repo.upsert(_state(1, None))
    assert enqueue_oldest_products(_store(), repo, limit=0) == 0
    assert repo.peek_all("hololive") == []


class FakeEnumerator:
    def __init__(self, urls):
        self._urls = urls

    def enumerate_products(self, base_url):
        return self._urls


def test_populate_enqueues_only_new_urls(repo):
    repo.upsert(_state(1, None))  # existing
    enum = FakeEnumerator(["https://x/products/1", "https://x/products/2"])

    new_count = populate_queue_from_sitemap(_store(), repo, enum)

    assert new_count == 1
    assert [e["product_url"] for e in repo.peek_all("hololive")] == ["https://x/products/2"]
