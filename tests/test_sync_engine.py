import pytest

from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.database.repository import ProductStateRepository
from estimator_king.sync.engine import _format_product_document, sync_products


class FakeEmbedder:
    def __init__(self):
        self.calls = []

    def embed_documents(self, texts):
        self.calls.append(list(texts))
        return [[0.1, 0.2] for _ in texts]


class FakeVectorStore:
    def __init__(self):
        self.upserts = []
        self.deletes = []

    def upsert(self, id, document, embedding, metadata):
        self.upserts.append((id, document, embedding, metadata))

    def delete(self, ids):
        self.deletes.append(list(ids))


@pytest.fixture
def repo():
    with ProductStateRepository(":memory:") as r:
        yield r


def _snapshot(pid=1):
    return ProductSnapshot(
        product_id=pid, title="Voice Pack", description="desc",
        variants=[ProductVariant(1, "Standard", "2000", "SKU")],
        html_details={"Features": "five tracks"},
    )


def test_format_product_document_includes_title_and_price_metadata():
    name, text, meta = _format_product_document(_snapshot(), "hololive", "https://x/p/1")
    assert name.startswith("hololive:1 - ")
    assert "Voice Pack" in text
    assert meta["store_id"] == "hololive"
    assert meta["product_id"] == "1"
    assert meta["title"] == "Voice Pack"
    assert meta["price_jpy"] == 2000


def test_create_embeds_upserts_and_persists_state(repo):
    emb, vs = FakeEmbedder(), FakeVectorStore()
    result = sync_products([("https://x/products/p1", _snapshot())], "hololive", repo, emb, vs)

    assert result.created == 1
    assert vs.upserts[0][0] == "hololive:1"
    state = repo.get_by_external_key("hololive:1")
    assert state is not None
    assert state.last_indexed_at is not None
    assert state.last_fetch_success_at is not None
    assert state.consecutive_failures == 0


def test_unchanged_content_skips_reindex_but_stamps_fetch(repo):
    emb, vs = FakeEmbedder(), FakeVectorStore()
    sync_products([("https://x/products/p1", _snapshot())], "hololive", repo, emb, vs)
    before = repo.get_by_external_key("hololive:1")

    emb2, vs2 = FakeEmbedder(), FakeVectorStore()
    result = sync_products([("https://x/products/p1", _snapshot())], "hololive", repo, emb2, vs2)

    assert result.skipped == 1
    assert vs2.upserts == []  # not re-indexed
    after = repo.get_by_external_key("hololive:1")
    assert after.last_indexed_at == before.last_indexed_at  # preserved


def test_changed_content_updates_and_reindexes(repo):
    emb, vs = FakeEmbedder(), FakeVectorStore()
    sync_products([("https://x/products/p1", _snapshot())], "hololive", repo, emb, vs)

    changed = ProductSnapshot(
        product_id=1, title="Voice Pack v2", description="new",
        variants=[ProductVariant(1, "Standard", "2500", "SKU")], html_details={},
    )
    emb2, vs2 = FakeEmbedder(), FakeVectorStore()
    result = sync_products([("https://x/products/p1", changed)], "hololive", repo, emb2, vs2)

    assert result.updated == 1
    assert vs2.upserts[0][0] == "hololive:1"


def test_carries_sitemap_state_forward(repo):
    emb, vs = FakeEmbedder(), FakeVectorStore()
    sync_products([("https://x/products/p1", _snapshot())], "hololive", repo, emb, vs)
    repo.record_sitemap_seen("hololive:1")
    seen_before = repo.get_by_external_key("hololive:1").last_seen_in_sitemap_at
    assert seen_before is not None

    changed = ProductSnapshot(product_id=1, title="T2", description="d",
                              variants=[ProductVariant(1, "S", "2000")], html_details={})
    sync_products([("https://x/products/p1", changed)], "hololive", repo, FakeEmbedder(), FakeVectorStore())

    after = repo.get_by_external_key("hololive:1")
    assert after.last_seen_in_sitemap_at == seen_before  # not wiped


def test_embedding_error_counts_failed_and_does_not_advance_index(repo):
    class Boom:
        def embed_documents(self, texts):
            raise RuntimeError("embed down")

    result = sync_products([("https://x/products/p1", _snapshot())], "hololive", repo, Boom(), FakeVectorStore())

    assert result.failed == 1
    state = repo.get_by_external_key("hololive:1")
    assert state is not None
    assert state.last_indexed_at is None
    assert state.last_fetch_success_at is not None  # fetch succeeded


def test_stored_product_url_is_passed_url_not_fabricated_from_id(repo):
    emb, vs = FakeEmbedder(), FakeVectorStore()
    handle_url = "https://shop.hololivepro.com/products/voice-pack-001"
    snap = _snapshot(pid=8087824892124)

    sync_products([(handle_url, snap)], "hololive", repo, emb, vs)

    state = repo.get_by_external_key("hololive:8087824892124")
    assert state is not None
    assert state.product_url == handle_url
    assert state.product_url != "https://shop.hololivepro.com/products/8087824892124"
