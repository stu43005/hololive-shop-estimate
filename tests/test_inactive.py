from __future__ import annotations

# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownParameterType=false
# pyright: reportUntypedFunctionDecorator=false
# pyright: reportMissingImports=false

from datetime import datetime, timezone

import pytest  # pyright: ignore[reportMissingImports]

from estimator_king.database.repository import (  # pyright: ignore[reportMissingImports]
    ProductState,
    ProductStateRepository,
)
from estimator_king.sync.inactive import mark_inactive_products  # pyright: ignore[reportMissingImports]


def _state(
    external_key,
    *,
    content_hash="a" * 64,
    normalizer_version=2,
    consecutive_failures=0,
    consecutive_sitemap_misses=0,
    inactive=False,
    inactive_reason=None,
    inactive_since=None,
):
    store_id, _, product_id = external_key.partition(":")
    return ProductState(
        external_key=external_key,
        store_id=store_id,
        product_id=product_id,
        product_url=f"https://x/products/{product_id}",
        content_hash=content_hash,
        normalizer_version=normalizer_version,
        consecutive_failures=consecutive_failures,
        consecutive_sitemap_misses=consecutive_sitemap_misses,
        inactive=inactive,
        inactive_reason=inactive_reason,
        inactive_since=inactive_since,
    )


class FakeVectorStore:
    def __init__(self):
        self.deleted = []

    def delete(self, ids):
        self.deleted.append(list(ids))


@pytest.fixture()
def repo() -> ProductStateRepository:
    with ProductStateRepository(":memory:") as r:
        yield r


def test_marks_inactive_and_deletes_vectors(repo):
    repo.upsert(_state("hololive:1", consecutive_failures=3))
    vs = FakeVectorStore()

    result = mark_inactive_products(repo, vs, failure_threshold=3, miss_threshold=4)

    assert result.marked_inactive == 1
    assert vs.deleted == [["hololive:1"]]


def test_mark_inactive_fetch_failures(repo: ProductStateRepository) -> None:
    repo.upsert(_state("store:123", consecutive_failures=3))
    repo.upsert(_state("store:456", consecutive_failures=5))

    result = mark_inactive_products(repo, FakeVectorStore())

    assert result.marked_inactive == 2
    assert result.already_inactive == 0
    assert len(result.failure_reasons) == 2
    assert "store:123" in result.failure_reasons
    assert "store:456" in result.failure_reasons
    assert len(result.sitemap_reasons) == 0

    state_123 = repo.get_by_external_key("store:123")
    assert state_123 is not None
    assert state_123.inactive is True
    assert state_123.inactive_reason == "fetch_failure_threshold_exceeded"
    assert state_123.inactive_since is not None


def test_mark_inactive_sitemap_misses(repo: ProductStateRepository) -> None:
    repo.upsert(_state("store:789", consecutive_sitemap_misses=4))
    repo.upsert(_state("store:999", consecutive_sitemap_misses=10))

    result = mark_inactive_products(repo, FakeVectorStore())

    assert result.marked_inactive == 2
    assert result.already_inactive == 0
    assert len(result.failure_reasons) == 0
    assert len(result.sitemap_reasons) == 2
    assert "store:789" in result.sitemap_reasons
    assert "store:999" in result.sitemap_reasons

    state_789 = repo.get_by_external_key("store:789")
    assert state_789 is not None
    assert state_789.inactive is True
    assert state_789.inactive_reason == "sitemap_miss_threshold_exceeded"
    assert state_789.inactive_since is not None


def test_mark_inactive_both_thresholds(repo: ProductStateRepository) -> None:
    repo.upsert(
        _state("store:dual", consecutive_failures=3, consecutive_sitemap_misses=4)
    )

    result = mark_inactive_products(repo, FakeVectorStore())

    assert result.marked_inactive == 1
    assert result.already_inactive == 0
    assert len(result.failure_reasons) == 1
    assert "store:dual" in result.failure_reasons
    assert len(result.sitemap_reasons) == 0

    state = repo.get_by_external_key("store:dual")
    assert state is not None
    assert state.inactive_reason == "fetch_failure_threshold_exceeded"


def test_mark_inactive_below_threshold(repo: ProductStateRepository) -> None:
    repo.upsert(_state("store:low_fail", consecutive_failures=2))
    repo.upsert(_state("store:low_miss", consecutive_sitemap_misses=3))
    repo.upsert(
        _state("store:active", consecutive_failures=0, consecutive_sitemap_misses=0)
    )

    result = mark_inactive_products(repo, FakeVectorStore())

    assert result.marked_inactive == 0
    assert result.already_inactive == 0
    assert len(result.failure_reasons) == 0
    assert len(result.sitemap_reasons) == 0

    for key in ["store:low_fail", "store:low_miss", "store:active"]:
        state = repo.get_by_external_key(key)
        assert state is not None
        assert state.inactive is False
        assert state.inactive_reason is None
        assert state.inactive_since is None


def test_mark_inactive_already_inactive(repo: ProductStateRepository) -> None:
    now = datetime.now(tz=timezone.utc)
    repo.upsert(
        _state(
            "store:already",
            inactive=True,
            inactive_reason="fetch_failure_threshold_exceeded",
            inactive_since=now,
        )
    )
    repo.upsert(_state("store:active", consecutive_failures=3))

    result = mark_inactive_products(repo, FakeVectorStore())

    assert result.marked_inactive == 1
    assert "store:active" in result.failure_reasons
    assert result.already_inactive == 1


def test_mark_inactive_empty_db(repo: ProductStateRepository) -> None:
    result = mark_inactive_products(repo, FakeVectorStore())

    assert result.marked_inactive == 0
    assert result.already_inactive == 0
    assert len(result.failure_reasons) == 0
    assert len(result.sitemap_reasons) == 0


def test_mark_inactive_idempotent(repo: ProductStateRepository) -> None:
    repo.upsert(_state("store:idempotent", consecutive_failures=3))

    result1 = mark_inactive_products(repo, FakeVectorStore())
    assert result1.marked_inactive == 1
    assert "store:idempotent" in result1.failure_reasons

    result2 = mark_inactive_products(repo, FakeVectorStore())
    assert result2.marked_inactive == 0
    assert len(result2.failure_reasons) == 0
    assert result2.already_inactive == 1


def test_mark_inactive_custom_failure_threshold(repo: ProductStateRepository) -> None:
    """Custom failure_threshold=5 means 3 failures is NOT enough."""
    repo.upsert(_state("store:low", consecutive_failures=3))
    repo.upsert(_state("store:high", consecutive_failures=5))

    result = mark_inactive_products(repo, FakeVectorStore(), failure_threshold=5)

    assert result.marked_inactive == 1
    assert "store:high" in result.failure_reasons
    assert "store:low" not in result.failure_reasons

    state_low = repo.get_by_external_key("store:low")
    assert state_low is not None
    assert state_low.inactive is False

    state_high = repo.get_by_external_key("store:high")
    assert state_high is not None
    assert state_high.inactive is True


def test_mark_inactive_custom_miss_threshold(repo: ProductStateRepository) -> None:
    """Custom miss_threshold=2 means 2 misses triggers inactive."""
    repo.upsert(_state("store:miss2", consecutive_sitemap_misses=2))
    repo.upsert(_state("store:miss3", consecutive_sitemap_misses=3))

    result = mark_inactive_products(repo, FakeVectorStore(), miss_threshold=2)

    assert result.marked_inactive == 2
    assert "store:miss2" in result.sitemap_reasons
    assert "store:miss3" in result.sitemap_reasons


def test_mark_inactive_custom_both_thresholds(repo: ProductStateRepository) -> None:
    """Both thresholds customized; failure takes precedence."""
    repo.upsert(
        _state("store:both", consecutive_failures=2, consecutive_sitemap_misses=2)
    )
    repo.upsert(_state("store:fail_only", consecutive_failures=2))
    repo.upsert(_state("store:miss_only", consecutive_sitemap_misses=2))
    repo.upsert(_state("store:below", consecutive_failures=1, consecutive_sitemap_misses=1))

    result = mark_inactive_products(repo, FakeVectorStore(), failure_threshold=2, miss_threshold=2)

    assert result.marked_inactive == 3
    assert "store:both" in result.failure_reasons
    assert "store:fail_only" in result.failure_reasons
    assert "store:miss_only" in result.sitemap_reasons
    assert "store:below" not in result.failure_reasons
    assert "store:below" not in result.sitemap_reasons

    state_below = repo.get_by_external_key("store:below")
    assert state_below is not None
    assert state_below.inactive is False
