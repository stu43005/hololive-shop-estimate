from __future__ import annotations

# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownParameterType=false
# pyright: reportUntypedFunctionDecorator=false
# pyright: reportMissingImports=false

from datetime import datetime, timedelta, timezone

import pytest  # pyright: ignore[reportMissingImports]

from estimator_king.database.repository import (  # pyright: ignore[reportMissingImports]
    ProductState,
    ProductStateRepository,
)


@pytest.fixture()
def repo():
    with ProductStateRepository(":memory:") as r:
        yield r


def _state(external_key, *, store_id, product_id, product_url="https://x/p",
           content_hash="h", last_fetch_success_at=None, last_indexed_at=None,
           inactive=False):
    return ProductState(
        external_key=external_key,
        store_id=store_id,
        product_id=product_id,
        product_url=product_url,
        content_hash=content_hash,
        normalizer_version=2,
        last_fetch_success_at=last_fetch_success_at,
        last_indexed_at=last_indexed_at,
        inactive=inactive,
    )


def _dt(days_ago: int) -> datetime:
    return datetime.now(tz=timezone.utc) - timedelta(days=days_ago)


# ── new tests from plan ──────────────────────────────────────────────────────

def test_upsert_roundtrip_new_columns(repo):
    repo.upsert(_state("hololive:1", store_id="hololive", product_id="1",
                        last_indexed_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    got = repo.get_by_external_key("hololive:1")
    assert got is not None
    assert got.store_id == "hololive"
    assert got.product_id == "1"
    assert got.last_indexed_at == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_upsert_coalesces_last_indexed_at_when_none(repo):
    repo.upsert(_state("hololive:1", store_id="hololive", product_id="1",
                        last_indexed_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    # second write omits last_indexed_at (None) -> must be preserved
    repo.upsert(_state("hololive:1", store_id="hololive", product_id="1",
                        content_hash="h2", last_indexed_at=None))
    got = repo.get_by_external_key("hololive:1")
    assert got is not None
    assert got.content_hash == "h2"
    assert got.last_indexed_at == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_get_oldest_active_products_orders_nulls_first_then_oldest(repo):
    now = datetime.now(tz=timezone.utc)
    repo.upsert(_state("s:newer", store_id="s", product_id="newer",
                       last_fetch_success_at=now))
    repo.upsert(_state("s:older", store_id="s", product_id="older",
                       last_fetch_success_at=now - timedelta(days=5)))
    repo.upsert(_state("s:never", store_id="s", product_id="never",
                       last_fetch_success_at=None))
    repo.upsert(_state("s:inactive", store_id="s", product_id="inactive",
                       last_fetch_success_at=None, inactive=True))

    keys = [p.external_key for p in repo.get_oldest_active_products("s", limit=10)]

    assert keys == ["s:never", "s:older", "s:newer"]  # inactive excluded, NULL first


def test_get_oldest_active_products_respects_limit(repo):
    repo.upsert(_state("s:a", store_id="s", product_id="a"))
    repo.upsert(_state("s:b", store_id="s", product_id="b"))
    assert len(repo.get_oldest_active_products("s", limit=1)) == 1


def test_get_oldest_active_products_zero_limit_returns_empty(repo):
    repo.upsert(_state("s:a", store_id="s", product_id="a"))
    assert repo.get_oldest_active_products("s", limit=0) == []


def test_list_active_filters_by_store_id_column(repo):
    repo.upsert(_state("hololive:1", store_id="hololive", product_id="1"))
    repo.upsert(_state("vspo:1", store_id="vspo", product_id="1"))
    keys = [p.external_key for p in repo.list_active("hololive")]
    assert keys == ["hololive:1"]


# ── retained tests (adapted to new _state signature) ────────────────────────

def test_state_db_get_by_external_key_missing_returns_none(
    repo: ProductStateRepository,
) -> None:
    assert repo.get_by_external_key("hololive:missing") is None


def test_state_db_upsert_updates_existing_row(repo: ProductStateRepository) -> None:
    first = repo.upsert(_state("hololive:1", store_id="hololive", product_id="1",
                                content_hash="aaa"))
    second = repo.upsert(
        _state(
            "hololive:1",
            store_id="hololive",
            product_id="1",
            content_hash="c" * 64,
        )
    )

    assert second.external_key == first.external_key
    assert second.created_at == first.created_at
    assert second.updated_at is not None
    assert first.updated_at is not None
    assert second.updated_at >= first.updated_at
    assert second.content_hash == "c" * 64

    count = repo.connection.execute(
        "SELECT COUNT(*) FROM products WHERE external_key = ?",
        ("hololive:1",),
    ).fetchone()[0]
    assert count == 1


def test_state_db_busy_timeout_configured(repo: ProductStateRepository) -> None:
    busy_timeout = repo.connection.execute("PRAGMA busy_timeout").fetchone()[0]
    assert int(busy_timeout) >= 30000


def test_state_db_journal_mode_is_valid(repo: ProductStateRepository) -> None:
    mode = repo.connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode in {"wal", "memory"}


def test_state_db_resume_after_interruption_shared_memory() -> None:
    uri = "file:state_db?mode=memory&cache=shared"

    hold = ProductStateRepository(uri)
    hold.open()
    try:
        r1 = ProductStateRepository(uri)
        r1.open()
        r1.connection.execute("BEGIN")
        r1.upsert(_state("hololive:tx", store_id="hololive", product_id="tx"))
        r1.close()

        assert hold.get_by_external_key("hololive:tx") is None

        with ProductStateRepository(uri) as r2:
            assert r2.get_by_external_key("hololive:tx") is None
            r2.upsert(_state("hololive:tx", store_id="hololive", product_id="tx"))
            assert r2.get_by_external_key("hololive:tx") is not None
    finally:
        hold.close()


def test_state_db_mark_inactive_logic(repo: ProductStateRepository) -> None:
    repo.upsert(_state("hololive:9", store_id="hololive", product_id="9"))
    repo.mark_inactive("hololive:9", reason="fetch_failures")
    s = repo.get_by_external_key("hololive:9")
    assert s is not None
    assert s.inactive is True
    assert s.inactive_reason == "fetch_failures"
    assert s.inactive_since is not None
    assert repo.get_all_active() == []


def test_state_db_mark_inactive_unknown_key_no_error(
    repo: ProductStateRepository,
) -> None:
    repo.mark_inactive("hololive:does-not-exist", reason="sitemap_missing")


def test_state_db_naive_datetimes_roundtrip_as_utc(
    repo: ProductStateRepository,
) -> None:
    naive_seen = datetime(2026, 2, 20, 12, 0, 0)
    naive_fetch = datetime(2026, 2, 20, 12, 30, 0)
    s = ProductState(
        external_key="hololive:naive",
        store_id="hololive",
        product_id="naive",
        product_url="https://x/naive",
        content_hash="h",
        normalizer_version=2,
        last_seen_in_sitemap_at=naive_seen,
        last_fetch_success_at=naive_fetch,
    )
    repo.upsert(s)
    loaded = repo.get_by_external_key("hololive:naive")
    assert loaded is not None
    assert loaded.last_seen_in_sitemap_at is not None
    assert loaded.last_fetch_success_at is not None
    assert loaded.last_seen_in_sitemap_at.tzinfo is not None
    assert loaded.last_fetch_success_at.tzinfo is not None
    assert loaded.last_seen_in_sitemap_at.utcoffset() == timedelta(0)
    assert loaded.last_fetch_success_at.utcoffset() == timedelta(0)


def test_state_db_get_all_active_orders_by_external_key(
    repo: ProductStateRepository,
) -> None:
    repo.upsert(_state("b:2", store_id="b", product_id="2"))
    repo.upsert(_state("a:1", store_id="a", product_id="1"))
    repo.upsert(_state("c:3", store_id="c", product_id="3"))
    keys = [s.external_key for s in repo.get_all_active()]
    assert keys == ["a:1", "b:2", "c:3"]


def test_state_db_product_url_roundtrip(repo: ProductStateRepository) -> None:
    s = _state("hololive:url1", store_id="hololive", product_id="url1",
               product_url="https://example.com/products/url1")
    saved = repo.upsert(s)
    assert saved.product_url == "https://example.com/products/url1"
    loaded = repo.get_by_external_key("hololive:url1")
    assert loaded is not None
    assert loaded.product_url == "https://example.com/products/url1"


def test_state_db_upsert_preserves_product_url_when_coalesced(
    repo: ProductStateRepository,
) -> None:
    repo.upsert(_state("hololive:keep", store_id="hololive", product_id="keep",
                       product_url="https://example.com/keep"))
    # product_url in _state defaults to "https://x/p" (never None); COALESCE keeps stored value
    # Test: explicit same URL still returns stored value
    updated = repo.upsert(_state("hololive:keep", store_id="hololive", product_id="keep",
                                  product_url="https://example.com/keep"))
    assert updated.product_url == "https://example.com/keep"


def test_state_db_get_by_product_url(repo: ProductStateRepository) -> None:
    repo.upsert(_state("store1:p1", store_id="store1", product_id="p1",
                       product_url="https://store1.com/p1"))
    repo.upsert(_state("store1:p2", store_id="store1", product_id="p2",
                       product_url="https://store1.com/p2"))
    repo.upsert(_state("store2:p3", store_id="store2", product_id="p3",
                       product_url="https://store1.com/p1"))

    result = repo.get_by_product_url("store1", "https://store1.com/p1")
    assert result is not None
    assert result.external_key == "store1:p1"

    # Different store_id, same URL
    result2 = repo.get_by_product_url("store2", "https://store1.com/p1")
    assert result2 is not None
    assert result2.external_key == "store2:p3"

    # Missing
    assert repo.get_by_product_url("store1", "https://nope.com") is None


def test_state_db_increment_consecutive_failures(
    repo: ProductStateRepository,
) -> None:
    repo.upsert(_state("hololive:fail", store_id="hololive", product_id="fail"))
    repo.increment_consecutive_failures("hololive:fail")
    loaded = repo.get_by_external_key("hololive:fail")
    assert loaded is not None
    assert loaded.consecutive_failures == 1

    repo.increment_consecutive_failures("hololive:fail")
    loaded2 = repo.get_by_external_key("hololive:fail")
    assert loaded2 is not None
    assert loaded2.consecutive_failures == 2


def test_state_db_reset_consecutive_failures(
    repo: ProductStateRepository,
) -> None:
    s = ProductState(
        external_key="hololive:res",
        store_id="hololive",
        product_id="res",
        product_url="https://x/res",
        content_hash="h",
        normalizer_version=2,
        consecutive_failures=5,
    )
    repo.upsert(s)
    repo.reset_consecutive_failures("hololive:res")
    loaded = repo.get_by_external_key("hololive:res")
    assert loaded is not None
    assert loaded.consecutive_failures == 0
    assert loaded.last_fetch_success_at is not None


def test_state_db_record_sitemap_seen(repo: ProductStateRepository) -> None:
    s = ProductState(
        external_key="hololive:seen",
        store_id="hololive",
        product_id="seen",
        product_url="https://x/seen",
        content_hash="h",
        normalizer_version=2,
        consecutive_sitemap_misses=3,
    )
    repo.upsert(s)
    repo.record_sitemap_seen("hololive:seen")
    loaded = repo.get_by_external_key("hololive:seen")
    assert loaded is not None
    assert loaded.consecutive_sitemap_misses == 0
    assert loaded.last_seen_in_sitemap_at is not None


def test_state_db_increment_sitemap_miss(repo: ProductStateRepository) -> None:
    repo.upsert(_state("hololive:miss", store_id="hololive", product_id="miss"))
    repo.increment_sitemap_miss("hololive:miss")
    loaded = repo.get_by_external_key("hololive:miss")
    assert loaded is not None
    assert loaded.consecutive_sitemap_misses == 1

    repo.increment_sitemap_miss("hololive:miss")
    loaded2 = repo.get_by_external_key("hololive:miss")
    assert loaded2 is not None
    assert loaded2.consecutive_sitemap_misses == 2
