import pytest

from estimator_king.vectorstore.store import QueryHit, VectorStore


@pytest.fixture
def store(tmp_path):
    return VectorStore(str(tmp_path / "chroma"))


def test_upsert_then_query_returns_nearest_first(store):
    store.upsert("hololive:1", "red shirt", [1.0, 0.0], {"store_id": "hololive", "price_jpy": 100})
    store.upsert("hololive:2", "blue shirt", [0.0, 1.0], {"store_id": "hololive", "price_jpy": 200})

    hits = store.query([0.9, 0.1], n_results=2)

    assert [h.id for h in hits] == ["hololive:1", "hololive:2"]
    assert isinstance(hits[0], QueryHit)
    assert hits[0].metadata["price_jpy"] == 100
    assert hits[0].document == "red shirt"


def test_upsert_updates_existing_id(store):
    store.upsert("hololive:1", "v1", [1.0, 0.0], {"store_id": "hololive"})
    store.upsert("hololive:1", "v2", [1.0, 0.0], {"store_id": "hololive"})

    hits = store.query([1.0, 0.0], n_results=5)

    assert len(hits) == 1
    assert hits[0].document == "v2"


def test_where_filters_by_metadata(store):
    store.upsert("hololive:1", "a", [1.0, 0.0], {"store_id": "hololive"})
    store.upsert("vspo:1", "b", [1.0, 0.0], {"store_id": "vspo"})

    hits = store.query([1.0, 0.0], n_results=5, where={"store_id": "vspo"})

    assert [h.id for h in hits] == ["vspo:1"]


def test_delete_removes_ids(store):
    store.upsert("hololive:1", "a", [1.0, 0.0], {"store_id": "hololive"})
    store.delete(["hololive:1"])

    assert store.query([1.0, 0.0], n_results=5) == []


def test_delete_empty_list_is_noop(store):
    store.delete([])  # must not raise


def test_persistence_across_instances(tmp_path):
    path = str(tmp_path / "chroma")
    VectorStore(path).upsert("hololive:1", "a", [1.0, 0.0], {"store_id": "hololive"})

    hits = VectorStore(path).query([1.0, 0.0], n_results=5)

    assert [h.id for h in hits] == ["hololive:1"]
