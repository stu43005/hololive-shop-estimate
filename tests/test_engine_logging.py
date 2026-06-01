import logging

from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.database.repository import ProductStateRepository
from estimator_king.sync.engine import sync_products

TALENTS = frozenset({"さくらみこ"})
ITEM_TYPES = ["アクリルスタンド", "ポーチ"]


class FakeEmbedder:
    def embed_documents(self, texts):
        return [[float(len(t)), 0.0, 0.0] for t in texts]


class FakeVectorStore:
    def __init__(self):
        self.docs = {}

    def upsert(self, id, document, embedding, metadata):
        self.docs[id] = (document, dict(metadata))

    def delete(self, ids):
        for i in ids:
            self.docs.pop(i, None)

    def get_by_product(self, store_id, product_id):
        from estimator_king.vectorstore.store import QueryHit
        return [
            QueryHit(id=i, document=d, metadata=m, distance=0.0)
            for i, (d, m) in self.docs.items()
            if m.get("store_id") == store_id and m.get("product_id") == product_id
        ]


class FakeTypingProvider:
    def classify_via_llm(self, text, item_types):
        return "その他"


def _snap():
    # 2 priceable items (アクリルスタンド / ポーチ, both vocab hits),
    # 1 SET excluded, 1 ¥0 excluded.
    return ProductSnapshot(
        product_id=10, title="P", description="",
        variants=[
            ProductVariant(variant_id=1, title="グッズ / アクリルスタンド", price="500"),
            ProductVariant(variant_id=2, title="グッズ / 旅のポーチ", price="800"),
            ProductVariant(variant_id=3, title="セット / フルセット", price="2000"),
            ProductVariant(variant_id=4, title="グッズ / 特典", price="0"),
        ],
        html_details={},
    )


def _repo():
    repo = ProductStateRepository(":memory:")
    repo.open()
    return repo


def _sync(repo, vs, snap, *, log_item_trees):
    return sync_products(
        [("http://x/products/10", snap)], "hololive", repo,
        FakeEmbedder(), vs,
        typing_provider=FakeTypingProvider(), talents=TALENTS,
        item_types=ITEM_TYPES, item_types_version=1,
        log_item_trees=log_item_trees,
    )


def _engine_msgs(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "estimator_king.sync.engine"]


def test_crawl_entry_emits_single_tree_record(caplog):
    repo, vs = _repo(), FakeVectorStore()
    with caplog.at_level(logging.INFO, logger="estimator_king.sync.engine"):
        _sync(repo, vs, _snap(), log_item_trees=True)
    trees = [r.getMessage() for r in caplog.records
             if r.name == "estimator_king.sync.engine"
             and r.getMessage().startswith("product ")]
    assert len(trees) == 1
    msg = trees[0]
    assert "\n" in msg  # single multi-line record, not split across records
    assert 'product hololive:10 "P" (created):' in msg
    assert "2 items" in msg
    assert "2 excluded (SET×1, ¥0×1)" in msg
    assert "typing=アクリルスタンド(vocab)" in msg
    assert "detail=miss" in msg
    assert "embed=indexed" in msg
    repo.close()


def test_crawl_entry_skipped_single_line(caplog):
    repo, vs = _repo(), FakeVectorStore()
    _sync(repo, vs, _snap(), log_item_trees=True)
    with caplog.at_level(logging.INFO, logger="estimator_king.sync.engine"):
        _sync(repo, vs, _snap(), log_item_trees=True)
    msgs = _engine_msgs(caplog)
    skipped = [m for m in msgs if "skipped (unchanged)" in m]
    assert any('product hololive:10 "P" skipped (unchanged)' in m for m in skipped)
    assert all("├─" not in m and "└─" not in m for m in skipped)
    repo.close()


def test_run_entry_emits_no_tree(caplog):
    repo, vs = _repo(), FakeVectorStore()
    with caplog.at_level(logging.INFO, logger="estimator_king.sync.engine"):
        _sync(repo, vs, _snap(), log_item_trees=False)  # created
        _sync(repo, vs, _snap(), log_item_trees=False)  # unchanged
    msgs = _engine_msgs(caplog)
    assert not any(m.startswith("product ") for m in msgs)
    repo.close()


def test_crawl_entry_tree_shows_embed_skipped(caplog):
    """Second sync that re-indexes the product (content_hash mismatch) but
    finds unchanged item embeddings must show embed=skipped(unchanged) in tree."""
    repo, vs = _repo(), FakeVectorStore()
    # First sync: creates the product row and indexes items into the vector store.
    _sync(repo, vs, _snap(), log_item_trees=False)

    # Corrupt the stored content_hash so the unchanged guard fails on the next
    # sync, forcing _rebuild_product_items to run even though the snapshot is
    # identical.  The vector store already has the items with the correct
    # item_hash, so every item will hit the existing.get(item_id)==item_hash
    # branch → embed_status = "skipped(unchanged)".
    repo.connection.execute(
        "UPDATE products SET content_hash = 'stale' WHERE external_key = 'hololive:10'"
    )

    with caplog.at_level(logging.INFO, logger="estimator_king.sync.engine"):
        _sync(repo, vs, _snap(), log_item_trees=True)

    trees = [r.getMessage() for r in caplog.records
             if r.name == "estimator_king.sync.engine"
             and r.getMessage().startswith("product ")]
    assert len(trees) == 1
    msg = trees[0]
    assert "(updated)" in msg
    assert "embed=skipped(unchanged)" in msg
    repo.close()
