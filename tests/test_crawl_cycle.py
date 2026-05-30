import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from estimator_king.config_schema import AppConfig, CrawlerPolicy, Store
from estimator_king.crawler.cycle import run_crawl_cycle
from estimator_king.crawler.sitemap import SitemapError


class FakeEmbedder:
    def embed_documents(self, texts):
        return [[0.0] for _ in texts]


class FakeVectorStore:
    def upsert(self, *a, **k):
        pass

    def delete(self, ids):
        pass


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "state.db")


def _config():
    return AppConfig(
        stores=[Store(id="hololive", base_url="https://x", sitemap_url="https://x/sm.xml")],
        crawler=CrawlerPolicy(max_products_per_run=32),
    )


def test_run_cycle_invokes_inactive_once_after_stores(db_path):
    cfg = _config()
    with patch("estimator_king.crawler.cycle.populate_queue_from_sitemap", new=AsyncMock(return_value=0)), \
         patch("estimator_king.crawler.cycle.enqueue_oldest_products", return_value=0) as enq, \
         patch("estimator_king.crawler.cycle.async_process_queue") as proc, \
         patch("estimator_king.crawler.cycle.mark_inactive_products") as inactive:
        async def fake_proc(*a, **k):
            from estimator_king.crawler.async_pipeline import PipelineResult
            return PipelineResult()
        proc.side_effect = fake_proc

        counters = asyncio.run(run_crawl_cycle(cfg, db_path, FakeEmbedder(), FakeVectorStore()))

    assert inactive.call_count == 1  # once per cycle, cross-store
    assert enq.call_args.kwargs["limit"] == 32  # budget = 32 - new_count(0)
    assert "errors" in counters


def test_force_refetch_skips_budget_enqueue(db_path):
    cfg = _config()
    with patch("estimator_king.crawler.cycle.populate_queue_from_sitemap", new=AsyncMock(return_value=0)), \
         patch("estimator_king.crawler.cycle.enqueue_oldest_products") as enq, \
         patch("estimator_king.crawler.cycle.async_process_queue") as proc, \
         patch("estimator_king.crawler.cycle.mark_inactive_products"):
        async def fake_proc(*a, **k):
            from estimator_king.crawler.async_pipeline import PipelineResult
            return PipelineResult()
        proc.side_effect = fake_proc

        asyncio.run(run_crawl_cycle(cfg, db_path, FakeEmbedder(), FakeVectorStore(), force_refetch=True))

    enq.assert_not_called()


def test_sitemap_failure_counts_error_and_continues(db_path):
    cfg = _config()
    with patch("estimator_king.crawler.cycle.populate_queue_from_sitemap",
               new=AsyncMock(side_effect=SitemapError("boom"))), \
         patch("estimator_king.crawler.cycle.enqueue_oldest_products") as enq, \
         patch("estimator_king.crawler.cycle.async_process_queue") as proc, \
         patch("estimator_king.crawler.cycle.mark_inactive_products") as inactive:
        async def fake_proc(*a, **k):
            from estimator_king.crawler.async_pipeline import PipelineResult
            return PipelineResult()
        proc.side_effect = fake_proc

        counters = asyncio.run(run_crawl_cycle(cfg, db_path, FakeEmbedder(), FakeVectorStore()))

    assert counters["errors"] >= 1
    assert inactive.call_count == 1  # cross-store sweep still runs once
    enq.assert_not_called()  # store skipped via continue before budget enqueue
    proc.assert_not_called()  # store skipped via continue before queue processing
