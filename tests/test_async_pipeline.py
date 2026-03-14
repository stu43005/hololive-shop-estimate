from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any, Generator

import pytest

from estimator_king.config_schema import CrawlerPolicy
from estimator_king.crawler.async_pipeline import async_process_queue
from estimator_king.database.repository import ProductState, ProductStateRepository


@pytest.fixture()
def repo() -> Generator[ProductStateRepository, None, None]:
    with ProductStateRepository(":memory:") as r:
        yield r


def _normalizer(
    snapshot: Any,
    store_id: str,
    product_url: str,
    existing_state: ProductState | None,
) -> ProductState:
    _ = existing_state
    return ProductState(
        external_key=f"{store_id}:{snapshot.product_id}",
        content_hash=snapshot.content_hash,
        normalizer_version=1,
        product_url=product_url,
    )


@pytest.mark.asyncio
async def test_async_process_queue_processes_items_in_parallel(
    repo: ProductStateRepository,
    monkeypatch,
) -> None:
    repo.enqueue_url("store-a", "https://shop.example.com/products/1")
    repo.enqueue_url("store-a", "https://shop.example.com/products/2")
    repo.enqueue_url("store-a", "https://shop.example.com/products/3")

    policy = CrawlerPolicy(
        rate_limit_rps=1000.0,
        jitter_max=0.0,
        concurrency_per_domain=3,
        max_retries=1,
    )

    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_fetch_product(url: str, _http_client):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.05)
            product_id = int(url.rstrip("/").split("/")[-1])
            return SimpleNamespace(
                product_id=product_id, content_hash=f"h-{product_id}"
            )
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(
        "estimator_king.crawler.async_pipeline.fetch_product",
        fake_fetch_product,
    )

    result = await async_process_queue("store-a", "https://shop.example.com", policy, repo, _normalizer)

    assert result.processed == 3
    assert result.failed == 0
    assert result.skipped == 0
    assert max_active >= 2
    assert repo.queue_size("store-a") == 0
    assert repo.get_by_external_key("store-a:1") is not None
    assert repo.get_by_external_key("store-a:2") is not None
    assert repo.get_by_external_key("store-a:3") is not None


@pytest.mark.asyncio
async def test_async_process_queue_partial_failure_keeps_others_processing(
    repo: ProductStateRepository,
    monkeypatch,
) -> None:
    repo.enqueue_url("store-a", "https://shop.example.com/products/1")
    repo.enqueue_url("store-a", "https://shop.example.com/products/2")
    repo.enqueue_url("store-a", "https://shop.example.com/products/3")

    policy = CrawlerPolicy(
        rate_limit_rps=1000.0,
        jitter_max=0.0,
        concurrency_per_domain=3,
        max_retries=1,
    )

    def fake_fetch_product(url: str, _http_client):
        product_id = int(url.rstrip("/").split("/")[-1])
        if product_id == 2:
            raise RuntimeError("boom")
        return SimpleNamespace(product_id=product_id, content_hash=f"h-{product_id}")

    monkeypatch.setattr(
        "estimator_king.crawler.async_pipeline.fetch_product",
        fake_fetch_product,
    )

    result = await async_process_queue("store-a", "https://shop.example.com", policy, repo, _normalizer)

    assert result.processed == 2
    assert result.failed == 1
    assert result.skipped == 0
    assert repo.queue_size("store-a") == 1
    assert repo.peek_next("store-a") == (
        2,
        "store-a",
        "https://shop.example.com/products/2",
    )
    assert repo.get_by_external_key("store-a:1") is not None
    assert repo.get_by_external_key("store-a:3") is not None


@pytest.mark.asyncio
async def test_async_process_queue_empty_queue_returns_zero_counts(
    repo: ProductStateRepository,
) -> None:
    policy = CrawlerPolicy(
        rate_limit_rps=1000.0,
        jitter_max=0.0,
        concurrency_per_domain=3,
        max_retries=1,
    )

    result = await async_process_queue("store-a", "https://shop.example.com", policy, repo, _normalizer)

    assert result.processed == 0
    assert result.failed == 0
    assert result.skipped == 0


def test_peek_all_returns_all_entries_without_modifying_queue(
    repo: ProductStateRepository,
) -> None:
    repo.enqueue_url("store-a", "https://shop.example.com/products/1")
    repo.enqueue_url("store-a", "https://shop.example.com/products/2")
    repo.enqueue_url("store-b", "https://shop.example.com/products/9")

    entries = repo.peek_all("store-a")

    assert entries == [
        {
            "id": 1,
            "store_id": "store-a",
            "product_url": "https://shop.example.com/products/1",
        },
        {
            "id": 2,
            "store_id": "store-a",
            "product_url": "https://shop.example.com/products/2",
        },
    ]
    assert repo.queue_size("store-a") == 2
    assert repo.peek_next("store-a") == (
        1,
        "store-a",
        "https://shop.example.com/products/1",
    )
