import logging

from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.database.repository import ProductStateRepository
from estimator_king.sync.engine import sync_products


class BoomEmbedder:
    def embed_documents(self, texts):
        raise RuntimeError("embed failed")


class FakeVectorStore:
    def upsert(self, id, document, embedding, metadata):
        pass


def _snap():
    return ProductSnapshot(
        product_id=1, title="T", description="d",
        variants=[ProductVariant(1, "S", "2000")], html_details={},
    )


def test_sync_failure_logs_under_module_logger(caplog):
    with ProductStateRepository(":memory:") as repo:
        with caplog.at_level(logging.ERROR):
            result = sync_products(
                [("https://x/products/p1", _snap())], "hololive", repo,
                BoomEmbedder(), FakeVectorStore(),
            )

    assert result.failed == 1
    recs = [r for r in caplog.records if r.name == "estimator_king.sync.engine"]
    assert recs and recs[0].levelno == logging.ERROR
    assert "Sync failed for hololive:1" in recs[0].getMessage()
