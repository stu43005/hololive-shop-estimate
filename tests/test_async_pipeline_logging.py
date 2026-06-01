import asyncio
import logging
from unittest.mock import patch

import pytest

from estimator_king.config_schema import CrawlerPolicy
from estimator_king.crawler import async_pipeline
from estimator_king.crawler.async_pipeline import async_process_queue
from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.database.repository import ProductStateRepository


class FakeTypingProvider:
    def classify_via_llm(self, text, item_types):
        return "その他"


class FakeEmbedder:
    def embed_documents(self, texts):
        return [[0.1, 0.2] for _ in texts]


class FakeVectorStore:
    def __init__(self):
        self._meta = {}

    def upsert(self, id, document, embedding, metadata):
        self._meta[id] = dict(metadata)

    def delete(self, ids):
        pass

    def get_by_product(self, store_id, product_id):
        from estimator_king.vectorstore.store import QueryHit
        return [QueryHit(id=i, document="", metadata=m, distance=0.0)
                for i, m in self._meta.items()
                if m.get("store_id") == store_id and m.get("product_id") == product_id]


@pytest.fixture
def repo():
    with ProductStateRepository(":memory:") as r:
        yield r


def _snap(pid):
    return ProductSnapshot(
        product_id=pid, title=f"T{pid}", description="d",
        variants=[ProductVariant(1, "S", "2000")], html_details={},
    )


def _run(repo, caplog, n):
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
                FakeEmbedder(), FakeVectorStore(),
                typing_provider=FakeTypingProvider(), talents=frozenset(),
                item_types=[], item_types_version=0))
    msgs = [
        r.getMessage() for r in caplog.records
        if r.name == "estimator_king.crawler.async_pipeline" and r.levelno == logging.INFO
    ]
    return result, msgs


def test_queue_start_heartbeat_and_done_logged(repo, caplog):
    n = async_pipeline._PROGRESS_LOG_EVERY + 5
    result, msgs = _run(repo, caplog, n)

    assert result.processed == n
    assert any(f"queue: {n} entries to process" in m for m in msgs)
    assert any("progress:" in m and f"/{n} processed" in m for m in msgs)
    assert any("done: created=" in m for m in msgs)


def test_heartbeat_and_done_append_aggregates(repo, caplog):
    n = async_pipeline._PROGRESS_LOG_EVERY + 5
    result, msgs = _run(repo, caplog, n)

    # each product yields 1 item, item_types=[] -> LLM source on every item.
    assert result.items == n
    assert result.typing_llm == n
    assert result.embed_indexed == n

    heartbeat = [m for m in msgs if "progress:" in m]
    assert heartbeat
    hb = heartbeat[0]
    assert "\n" in hb  # single multi-line record
    assert "items" in hb
    assert "excluded" in hb
    assert "detail hit:" in hb
    assert "typing:" in hb and "(vocab)" in hb and "(cache)" in hb and "(llm)" in hb
    assert "embed indexed:" in hb

    done = [m for m in msgs if "done: created=" in m]
    assert done
    dn = done[0]
    assert f"{n} items" in dn  # final cumulative total
    assert "embed indexed:" in dn
