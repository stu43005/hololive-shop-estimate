import asyncio
from unittest.mock import patch

import pytest

from estimator_king.config_schema import CrawlerPolicy, ProxyConfig
from estimator_king.crawler.async_pipeline import async_process_queue
from estimator_king.crawler.async_http_client import ClientError, ServerError
from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.database.repository import ProductStateRepository


class FakeEmbedder:
    def embed_documents(self, texts):
        return [[0.1, 0.2] for _ in texts]


class FakeVectorStore:
    def __init__(self):
        self.upserts = []

    def upsert(self, id, document, embedding, metadata):
        self.upserts.append(id)

    def delete(self, ids):
        pass


@pytest.fixture
def repo():
    with ProductStateRepository(":memory:") as r:
        yield r


def _snap(pid):
    return ProductSnapshot(product_id=pid, title=f"T{pid}", description="d",
                           variants=[ProductVariant(1, "S", "2000")], html_details={})


def test_success_indexes_and_clears_queue(repo):
    repo.enqueue_url("hololive", "https://x/products/1")
    vs = FakeVectorStore()
    policy = CrawlerPolicy()

    with patch("estimator_king.crawler.async_pipeline.fetch_product", return_value=_snap(1)):
        result = asyncio.run(async_process_queue(
            "hololive", policy, repo, FakeEmbedder(), vs))

    assert result.processed == 1
    assert vs.upserts == ["hololive:1"]
    assert repo.peek_all("hololive") == []  # queue drained
    state = repo.get_by_external_key("hololive:1")
    assert state is not None and state.last_fetch_success_at is not None


def test_fetch_failure_increments_failures_and_keeps_queue(repo):
    # Pre-existing product row so the failure can be recorded against it.
    repo.enqueue_url("hololive", "https://x/products/1")
    with patch("estimator_king.crawler.async_pipeline.fetch_product", return_value=_snap(1)):
        asyncio.run(async_process_queue("hololive", CrawlerPolicy(), repo,
                                        FakeEmbedder(), FakeVectorStore()))
    repo.enqueue_url("hololive", "https://x/products/1")  # re-queue for the failing run

    def boom(url, client):
        raise ServerError(url, status_code=500)

    with patch("estimator_king.crawler.async_pipeline.fetch_product", side_effect=boom):
        result = asyncio.run(async_process_queue("hololive", CrawlerPolicy(), repo,
                                                 FakeEmbedder(), FakeVectorStore()))

    assert result.failed == 1
    assert repo.peek_all("hololive") != []  # entry kept for retry
    assert repo.get_by_external_key("hololive:1").consecutive_failures == 1


def test_proxy_forwarded_to_async_http_client(repo):
    repo.enqueue_url("hololive", "https://x/products/1")
    captured = {}

    class _FakeClient:
        def __init__(self, policy, proxy=None):
            captured["proxy"] = proxy

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    proxy_cfg = ProxyConfig(enabled=True, http_proxy="http://p:8080")
    with patch("estimator_king.crawler.async_pipeline.AsyncHTTPClient", _FakeClient), \
         patch("estimator_king.crawler.async_pipeline.fetch_product", return_value=_snap(1)):
        asyncio.run(async_process_queue(
            "hololive", CrawlerPolicy(), repo,
            FakeEmbedder(), FakeVectorStore(), proxy=proxy_cfg))

    assert captured["proxy"] is proxy_cfg


def test_client_error_404_deletes_queue_entry_for_new_product(repo):
    repo.enqueue_url("hololive", "https://x/products/gone")

    def boom(url, client):
        raise ClientError(url, status_code=404)

    with patch("estimator_king.crawler.async_pipeline.fetch_product", side_effect=boom):
        result = asyncio.run(async_process_queue("hololive", CrawlerPolicy(), repo,
                                                 FakeEmbedder(), FakeVectorStore()))

    assert result.failed == 1
    assert repo.peek_all("hololive") == []  # definitively gone: dropped, not retried


def test_client_error_410_deletes_queue_and_increments_when_row_exists(repo):
    # First, a successful run creates the product row.
    repo.enqueue_url("hololive", "https://x/products/1")
    with patch("estimator_king.crawler.async_pipeline.fetch_product", return_value=_snap(1)):
        asyncio.run(async_process_queue("hololive", CrawlerPolicy(), repo,
                                        FakeEmbedder(), FakeVectorStore()))
    repo.enqueue_url("hololive", "https://x/products/1")  # re-queue for the failing run

    def boom(url, client):
        raise ClientError(url, status_code=410)

    with patch("estimator_king.crawler.async_pipeline.fetch_product", side_effect=boom):
        result = asyncio.run(async_process_queue("hololive", CrawlerPolicy(), repo,
                                                 FakeEmbedder(), FakeVectorStore()))

    assert result.failed == 1
    assert repo.peek_all("hololive") == []  # dropped
    assert repo.get_by_external_key("hololive:1").consecutive_failures == 1


def test_client_error_400_keeps_queue_entry(repo):
    repo.enqueue_url("hololive", "https://x/products/1")

    def boom(url, client):
        raise ClientError(url, status_code=400)

    with patch("estimator_king.crawler.async_pipeline.fetch_product", side_effect=boom):
        result = asyncio.run(async_process_queue("hololive", CrawlerPolicy(), repo,
                                                 FakeEmbedder(), FakeVectorStore()))

    assert result.failed == 1
    assert repo.peek_all("hololive") != []  # only 404/410 are definitive; 400 is retried


def test_worker_pool_processes_all_entries(repo):
    for i in range(1, 6):
        repo.enqueue_url("hololive", f"https://x/products/{i}")
    vs = FakeVectorStore()
    policy = CrawlerPolicy(concurrency_per_domain=2)
    snaps = {f"https://x/products/{i}": _snap(i) for i in range(1, 6)}

    def fake_fetch(url, client):
        return snaps[url]

    with patch("estimator_king.crawler.async_pipeline.fetch_product", side_effect=fake_fetch):
        result = asyncio.run(async_process_queue(
            "hololive", policy, repo, FakeEmbedder(), vs))

    assert result.processed == 5
    assert repo.peek_all("hololive") == []  # queue fully drained by the worker pool


def test_worker_pool_caps_concurrency_at_worker_count(repo):
    for i in range(1, 7):
        repo.enqueue_url("hololive", f"https://x/products/{i}")
    policy = CrawlerPolicy(concurrency_per_domain=2)

    active = 0
    max_active = 0

    async def fake_fetch(url, client):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return _snap(int(url.rsplit("/", 1)[-1]))

    with patch("estimator_king.crawler.async_pipeline.fetch_product", side_effect=fake_fetch):
        asyncio.run(async_process_queue(
            "hololive", policy, repo, FakeEmbedder(), FakeVectorStore()))

    assert max_active == 2  # worker pool caps concurrent fetches at concurrency_per_domain
