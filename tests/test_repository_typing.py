from datetime import datetime, timezone

from estimator_king.database.repository import ProductState, ProductStateRepository


def _repo() -> ProductStateRepository:
    repo = ProductStateRepository(":memory:")
    repo.open()
    return repo


def test_product_state_carries_item_types_version():
    repo = _repo()
    now = datetime.now(tz=timezone.utc)
    repo.upsert(ProductState(
        external_key="s:1", store_id="s", product_id="1", product_url="u",
        content_hash="h", normalizer_version=2, item_types_version=3,
        last_seen_in_sitemap_at=now, last_fetch_success_at=now,
    ))
    got = repo.get_by_external_key("s:1")
    assert got is not None and got.item_types_version == 3
    repo.close()


def test_type_cache_roundtrip_and_list_other():
    repo = _repo()
    assert repo.get_cached_type("hash-a") is None
    repo.put_cached_type("hash-a", "ぬいぐるみ", 1, text_sample="もちもちぬいぐるみ")
    repo.put_cached_type("hash-b", "その他", 1, text_sample="謎の物体")
    assert repo.get_cached_type("hash-a") == "ぬいぐるみ"
    # list_other_typed returns readable text samples (not hashes) for vocab review.
    assert repo.list_other_typed(10) == ["謎の物体"]
    repo.close()
