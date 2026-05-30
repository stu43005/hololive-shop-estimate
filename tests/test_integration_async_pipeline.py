"""Integration test: drive run_crawl_cycle end-to-end with fakes."""

from __future__ import annotations

# pyright: reportUnknownParameterType=false
# pyright: reportMissingParameterType=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from estimator_king.config_schema import AppConfig, CrawlerPolicy, Store
from estimator_king.crawler.cycle import run_crawl_cycle
from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.database.repository import ProductStateRepository


STORE_ID = "test-store"
BASE_URL = "https://shop.example.com"
SITEMAP_URL = "https://shop.example.com/sitemap.xml"


class FakeEmbedder:
    """Returns a fixed-dimension embedding for any document."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[0.1, 0.2] for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        _ = text
        return [0.1, 0.2]


class FakeVectorStore:
    """Records upsert calls so tests can assert on them."""

    def __init__(self) -> None:
        self.upserts: list[str] = []

    def upsert(self, id: str, document: str, embedding: list[float],
               metadata: dict[str, Any]) -> None:
        _ = (document, embedding, metadata)
        self.upserts.append(id)

    def delete(self, ids: list[str]) -> None:
        _ = ids

    def query(self, embedding: list[float], n_results: int,
              where: dict[str, Any] | None = None) -> list[Any]:
        _ = (embedding, n_results, where)
        return []


def _config(max_products: int = 32) -> AppConfig:
    return AppConfig(
        stores=[Store(id=STORE_ID, base_url=BASE_URL, sitemap_url=SITEMAP_URL)],
        # concurrency_per_domain=3 exercises the real default concurrent path now
        # that the repository serializes DB access with an RLock.
        crawler=CrawlerPolicy(max_products_per_run=max_products, concurrency_per_domain=3),
    )


def _snap(pid: int) -> ProductSnapshot:
    return ProductSnapshot(
        product_id=pid,
        title=f"Product {pid}",
        description=f"A great product #{pid}",
        variants=[ProductVariant(variant_id=pid * 10, title="Regular", price="3000")],
        html_details={},
    )


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "state.db")


def test_run_cycle_indexes_pre_seeded_urls(db_path: str) -> None:
    """Pre-seeded handle URLs land in the DB; the stored product_url is the
    fetched handle URL, NOT a numeric one fabricated from product_id."""
    embedder = FakeEmbedder()
    vs = FakeVectorStore()

    handle_url_a = f"{BASE_URL}/products/voice-pack-001"
    handle_url_b = f"{BASE_URL}/products/voice-pack-002"
    pid_a = 8087824892124
    pid_b = 8087824892125

    # Pre-seed two handle URLs directly into the queue so we don't need the sitemap.
    with ProductStateRepository(db_path) as repo:
        _ = repo.enqueue_url(STORE_ID, handle_url_a)
        _ = repo.enqueue_url(STORE_ID, handle_url_b)

    snapshots: dict[str, ProductSnapshot] = {
        handle_url_a: _snap(pid_a),
        handle_url_b: _snap(pid_b),
    }

    def fake_fetch(url: str, client: Any) -> ProductSnapshot:
        _ = client
        return snapshots[url]

    with (
        patch("estimator_king.crawler.cycle.populate_queue_from_sitemap", return_value=0),
        patch("estimator_king.crawler.async_pipeline.fetch_product", side_effect=fake_fetch),
    ):
        counters = asyncio.run(
            run_crawl_cycle(_config(), db_path, embedder, vs)  # pyright: ignore[reportArgumentType]
        )

    # Both products should have been processed and upserted.
    assert counters["fetched_ok"] == 2
    assert counters["created"] == 2
    assert counters["errors"] == 0

    expected_keys = {f"{STORE_ID}:{pid_a}", f"{STORE_ID}:{pid_b}"}
    assert set(vs.upserts) == expected_keys

    # Stored URL must be the fetched handle URL, not a fabricated numeric one.
    with ProductStateRepository(db_path) as repo:
        state_a = repo.get_by_external_key(f"{STORE_ID}:{pid_a}")
        assert state_a is not None
        assert state_a.last_indexed_at is not None
        assert state_a.product_url == handle_url_a
        assert state_a.product_url != f"{BASE_URL}/products/{pid_a}"


def test_run_cycle_skips_unchanged_products_on_second_run(db_path: str) -> None:
    """A second crawl cycle with identical snapshots does not re-upsert."""
    embedder = FakeEmbedder()
    vs = FakeVectorStore()

    with ProductStateRepository(db_path) as repo:
        _ = repo.enqueue_url(STORE_ID, f"{BASE_URL}/products/201")

    def fake_fetch(url: str, client: Any) -> ProductSnapshot:
        _ = (url, client)
        return _snap(201)

    # First run: creates the product.
    with (
        patch("estimator_king.crawler.cycle.populate_queue_from_sitemap", return_value=0),
        patch("estimator_king.crawler.async_pipeline.fetch_product", side_effect=fake_fetch),
    ):
        counters1 = asyncio.run(
            run_crawl_cycle(_config(), db_path, embedder, vs)  # pyright: ignore[reportArgumentType]
        )

    assert counters1["created"] == 1

    # Re-enqueue the same URL to simulate re-crawl.
    with ProductStateRepository(db_path) as repo:
        _ = repo.enqueue_url(STORE_ID, f"{BASE_URL}/products/201")

    upserts_after_first = list(vs.upserts)

    # Second run: same snapshot → sync engine skips it.
    with (
        patch("estimator_king.crawler.cycle.populate_queue_from_sitemap", return_value=0),
        patch("estimator_king.crawler.async_pipeline.fetch_product", side_effect=fake_fetch),
    ):
        counters2 = asyncio.run(
            run_crawl_cycle(_config(), db_path, embedder, vs)  # pyright: ignore[reportArgumentType]
        )

    assert counters2["fetched_ok"] == 1
    assert counters2["updated"] == 0
    # No new upserts were added to the vector store.
    assert vs.upserts == upserts_after_first
