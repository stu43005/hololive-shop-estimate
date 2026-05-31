from estimator_king.vectorstore.store import VectorStore


def test_get_by_product_filters_by_store_and_product(tmp_path):
    vs = VectorStore(str(tmp_path / "chroma"))
    emb = [0.1, 0.2, 0.3]
    vs.upsert("s:1:a", "doc a", emb, {"store_id": "s", "product_id": "1", "item_hash": "ha"})
    vs.upsert("s:1:b", "doc b", emb, {"store_id": "s", "product_id": "1", "item_hash": "hb"})
    vs.upsert("s:2:c", "doc c", emb, {"store_id": "s", "product_id": "2", "item_hash": "hc"})
    hits = vs.get_by_product("s", "1")
    assert sorted(h.id for h in hits) == ["s:1:a", "s:1:b"]
    assert {h.metadata["item_hash"] for h in hits} == {"ha", "hb"}
