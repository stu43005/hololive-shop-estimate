from datetime import datetime, timezone

from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.database.repository import ProductState, ProductStateRepository
from estimator_king.sync.engine import sync_products

TALENTS = frozenset({"さくらみこ"})
ITEM_TYPES = ["アクリルスタンド", "ポーチ"]


class FakeEmbedder:
    def embed_documents(self, texts):
        return [[float(len(t)), 0.0, 0.0] for t in texts]


class FakeVectorStore:
    def __init__(self):
        self.docs = {}  # id -> (document, metadata)

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
    return ProductSnapshot(
        product_id=10, title="P", description="",
        variants=[
            ProductVariant(variant_id=1, title="グッズ / アクリルスタンド", price="500"),
            ProductVariant(variant_id=2, title="グッズ / 旅のポーチ", price="800"),
        ],
        html_details={},
    )


def _repo():
    repo = ProductStateRepository(":memory:")
    repo.open()
    return repo


def _sync(repo, vs, snap):
    return sync_products(
        [("http://x/products/10", snap)], "hololive", repo,
        FakeEmbedder(), vs,
        typing_provider=FakeTypingProvider(), talents=TALENTS,
        item_types=ITEM_TYPES, item_types_version=1,
    )


def test_creates_one_vector_per_item_with_own_price():
    repo, vs = _repo(), FakeVectorStore()
    _sync(repo, vs, _snap())
    prices = sorted(m["price_jpy"] for _, m in vs.docs.values())
    assert prices == [500, 800]
    assert all("item_type" in m for _, m in vs.docs.values())
    repo.close()


def test_unchanged_product_skips_reembed():
    repo, vs = _repo(), FakeVectorStore()
    _sync(repo, vs, _snap())
    first = {i: d for i, (d, _) in vs.docs.items()}
    for i in vs.docs:
        vs.docs[i] = (vs.docs[i][0] + "_orig", vs.docs[i][1])
    res = _sync(repo, vs, _snap())
    assert res.skipped >= 1
    assert all(d.endswith("_orig") for d, _ in vs.docs.values())
    repo.close()


def test_item_types_version_bump_forces_rebuild():
    repo, vs = _repo(), FakeVectorStore()
    first = _sync(repo, vs, _snap())
    assert first.created == 1 and first.skipped == 0
    same = _sync(repo, vs, _snap())
    assert same.skipped == 1
    bumped = sync_products(
        [("http://x/products/10", _snap())], "hololive", repo,
        FakeEmbedder(), vs, typing_provider=FakeTypingProvider(), talents=TALENTS,
        item_types=ITEM_TYPES, item_types_version=2)
    assert bumped.skipped == 0 and bumped.updated == 1
    repo.close()


def test_stale_item_vector_deleted_when_variant_removed():
    repo, vs = _repo(), FakeVectorStore()
    _sync(repo, vs, _snap())
    assert len(vs.docs) == 2
    snap2 = ProductSnapshot(
        product_id=10, title="P2", description="",
        variants=[ProductVariant(variant_id=1, title="グッズ / アクリルスタンド", price="500")],
        html_details={},
    )
    _sync(repo, vs, snap2)
    assert len(vs.docs) == 1
    repo.close()
