import asyncio
import logging
from unittest.mock import patch

import pytest

from estimator_king.config_schema import CrawlerPolicy
from estimator_king.crawler import async_pipeline
from estimator_king.crawler.async_pipeline import async_process_queue
from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.database.repository import ProductStateRepository


class FakeEmbedder:
    def embed_documents(self, texts):
        return [[0.1, 0.2] for _ in texts]


class FakeVectorStore:
    def upsert(self, id, document, embedding, metadata):
        pass

    def delete(self, ids):
        pass


@pytest.fixture
def repo():
    with ProductStateRepository(":memory:") as r:
        yield r


def _snap(pid):
    return ProductSnapshot(
        product_id=pid, title=f"T{pid}", description="d",
        variants=[ProductVariant(1, "S", "2000")], html_details={},
    )


def test_queue_start_heartbeat_and_done_logged(repo, caplog):
    n = async_pipeline._PROGRESS_LOG_EVERY + 5
    for pid in range(1, n + 1):
        repo.enqueue_url("hololive", f"https://x/products/{pid}")

    def fake_fetch(url, client):
        pid = int(url.rsplit("/", 1)[1])
        return _snap(pid)

    with caplog.at_level(logging.INFO, logger="estimator_king.crawler.async_pipeline"):
        with patch(
            "estimator_king.crawler.async_pipeline.fetch_product",
            side_effect=fake_fetch,
        ):
            result = asyncio.run(async_process_queue(
                "hololive", CrawlerPolicy(), repo,
                FakeEmbedder(), FakeVectorStore()))

    assert result.processed == n
    msgs = [
        r.getMessage() for r in caplog.records
        if r.name == "estimator_king.crawler.async_pipeline" and r.levelno == logging.INFO
    ]
    assert any(f"queue: {n} entries to process" in m for m in msgs)
    assert any("progress:" in m and f"/{n} processed" in m for m in msgs)
    assert any("done: created=" in m for m in msgs)
