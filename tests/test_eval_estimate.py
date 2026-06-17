"""Regression test: build_context must over-fetch so a candidate that the
self item would push past the fetch_n boundary is still retrieved.

When the query's own product (self) is the closest hit, production with that
product absent from the index would still see the next candidate. A naive
"fetch fetch_n then drop self" hides it; the over-fetch fix recovers it.
"""

import pytest

from estimator_king.bot.estimator import Estimator
from estimator_king.vectorstore.store import QueryHit
from scripts.analysis.eval_estimate import build_context, InvalidRun


class _Embedder:
    def embed_query(self, text):
        return [1.0, 0.0, 0.0]


class _Typing:
    def classify_via_llm(self, text, item_types):
        return "その他"


class _TruncatingStore:
    """Honors n_results (returns the closest n hits), like a real vector store."""

    def __init__(self, hits):
        self._hits = hits

    def query(self, embedding, n_results, where=None):
        return list(self._hits[:n_results])


def _hit(id, price, dist):
    return QueryHit(id=id, document="", distance=dist, metadata={
        "item_name": id, "item_type": "その他", "price_jpy": price,
        "published_at": 0, "store_id": "s", "detail_snippet": "",
        "product_title": id})


def _estimator(vs):
    return Estimator(_Embedder(), None, vs, typing_provider=_Typing(),
                     item_types=["ぬいぐるみ"], item_types_version=1,
                     top_k=2, recency_weight=0.0, diversity_weight=0.0,
                     fetch_multiplier=1)


def test_build_context_overfetches_past_self_truncation():
    official = 1000
    query = "セルフ商品"
    # distance-sorted; fetch_n = top_k*fetch_multiplier = 2. A plain fetch_n
    # query returns [self, c1]; dropping self hides c2. Over-fetch must keep c2.
    hits = [
        _hit(query, official, 0.00),   # self: price==official, sim=1.0
        _hit("c1", 500, 0.10),
        _hit("c2", 600, 0.11),         # hidden without over-fetch
        _hit("c3", 700, 0.12),         # beyond top_k after rerank
    ]
    est = _estimator(_TruncatingStore(hits))
    block, selves = build_context(est, query, official)
    _, _, refs = block.partition("\n")
    assert "c1" in refs
    assert "c2" in refs                       # recovered by over-fetch
    assert query not in refs                  # self excluded from references
    assert any("sim=1.000" in s for s in selves)


def test_build_context_retains_same_price_but_different_name_comparable():
    official = 1000
    query = "セルフ商品"
    hits = [
        _hit(query, official, 0.00),        # self: exact name + price
        _hit("別の商品", official, 0.20),    # SAME price, different name, sim 0.80
        _hit("c2", 500, 0.30),
    ]
    est = _estimator(_TruncatingStore(hits))
    block, selves = build_context(est, query, official)
    _, _, refs = block.partition("\n")
    assert "別の商品" in refs          # legitimate same-price different-name comparable kept
    assert query not in refs           # only the exact-name self is excluded
    assert len(selves) == 1


def test_build_context_fails_closed_on_sim_only_exclusion():
    official = 1000
    query = "セルフ商品"
    hits = [
        _hit("紛らわしい別商品", official, 0.02),  # sim 0.98 >= 0.95, price==official, name != query
        _hit("c1", 500, 0.30),
    ]
    est = _estimator(_TruncatingStore(hits))
    with pytest.raises(InvalidRun):
        build_context(est, query, official)


def test_build_context_fails_closed_on_multiple_self():
    official = 1000
    query = "セルフ商品"
    hits = [
        _hit(query, official, 0.10),       # exact name + price (id == query)
        QueryHit(id="dup", document="", distance=0.12, metadata={
            "item_name": query, "item_type": "その他", "price_jpy": official,
            "published_at": 0, "store_id": "s2", "detail_snippet": "",
            "product_title": query}),       # different id, same name+price -> 2nd self
        _hit("c1", 500, 0.30),
    ]
    est = _estimator(_TruncatingStore(hits))
    with pytest.raises(InvalidRun):
        build_context(est, query, official)
