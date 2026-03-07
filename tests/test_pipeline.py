"""Tests for estimator_king.crawler.pipeline."""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

from estimator_king.crawler.pipeline import (
    enqueue_stale_products,
    populate_queue_from_sitemap,
    process_queue,
)
from estimator_king.database.repository import ProductState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(
    store_id: str = "holo", base_url: str = "https://shop.example.com"
) -> MagicMock:
    store = MagicMock()
    store.id = store_id
    store.base_url = base_url
    return store


def _make_product(
    external_key: str,
    product_url: str | None = None,
) -> ProductState:
    return ProductState(
        external_key=external_key,
        content_hash="a" * 64,
        normalizer_version=1,
        product_url=product_url,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEmptySitemap:
    """Empty sitemap: return 0, no products marked missed."""

    def test_returns_zero(self) -> None:
        store = _make_store()
        repo = MagicMock()
        enumerator = MagicMock()
        enumerator.enumerate_products.return_value = []

        result = populate_queue_from_sitemap(store, repo, enumerator)

        assert result == 0
        enumerator.enumerate_products.assert_called_once_with(store.base_url)

    def test_no_side_effects(self) -> None:
        store = _make_store()
        repo = MagicMock()
        enumerator = MagicMock()
        enumerator.enumerate_products.return_value = []

        populate_queue_from_sitemap(store, repo, enumerator)

        repo.enqueue_url.assert_not_called()
        repo.record_sitemap_seen.assert_not_called()
        repo.increment_sitemap_miss.assert_not_called()
        repo.list_active.assert_not_called()


class TestNormalOperation:
    """New URLs enqueued, existing get sitemap_seen, absent get sitemap_miss."""

    def test_new_urls_enqueued(self) -> None:
        store = _make_store()
        repo = MagicMock()
        enumerator = MagicMock()

        urls = [
            "https://shop.example.com/products/new-a",
            "https://shop.example.com/products/new-b",
        ]
        enumerator.enumerate_products.return_value = urls
        repo.get_by_product_url.return_value = None  # not in DB
        repo.enqueue_url.return_value = True
        repo.list_active.return_value = []

        result = populate_queue_from_sitemap(store, repo, enumerator)

        assert result == 2
        assert repo.enqueue_url.call_count == 2
        repo.enqueue_url.assert_any_call("holo", urls[0])
        repo.enqueue_url.assert_any_call("holo", urls[1])
        repo.record_sitemap_seen.assert_not_called()

    def test_existing_urls_get_sitemap_seen(self) -> None:
        store = _make_store()
        repo = MagicMock()
        enumerator = MagicMock()

        url = "https://shop.example.com/products/existing"
        enumerator.enumerate_products.return_value = [url]
        existing_product = _make_product("holo:existing", product_url=url)
        repo.get_by_product_url.return_value = existing_product
        repo.list_active.return_value = [existing_product]

        result = populate_queue_from_sitemap(store, repo, enumerator)

        assert result == 0
        repo.enqueue_url.assert_not_called()
        repo.record_sitemap_seen.assert_called_once_with("holo:existing")

    def test_absent_products_get_sitemap_miss(self) -> None:
        store = _make_store()
        repo = MagicMock()
        enumerator = MagicMock()

        sitemap_url = "https://shop.example.com/products/in-sitemap"
        enumerator.enumerate_products.return_value = [sitemap_url]
        repo.get_by_product_url.return_value = None
        repo.enqueue_url.return_value = True

        # Active product whose URL is NOT in the sitemap
        absent_product = _make_product(
            "holo:gone", product_url="https://shop.example.com/products/gone"
        )
        repo.list_active.return_value = [absent_product]

        populate_queue_from_sitemap(store, repo, enumerator)

        repo.increment_sitemap_miss.assert_called_once_with("holo:gone")

    def test_mixed_scenario(self) -> None:
        store = _make_store()
        repo = MagicMock()
        enumerator = MagicMock()

        new_url = "https://shop.example.com/products/new"
        existing_url = "https://shop.example.com/products/existing"
        enumerator.enumerate_products.return_value = [new_url, existing_url]

        existing_product = _make_product("holo:existing", product_url=existing_url)

        def lookup(store_id: str, url: str) -> ProductState | None:
            if url == existing_url:
                return existing_product
            return None

        repo.get_by_product_url.side_effect = lookup
        repo.enqueue_url.return_value = True

        # One active product in sitemap, one absent
        absent_product = _make_product(
            "holo:gone", product_url="https://shop.example.com/products/gone"
        )
        repo.list_active.return_value = [existing_product, absent_product]

        result = populate_queue_from_sitemap(store, repo, enumerator)

        assert result == 1
        repo.enqueue_url.assert_called_once_with("holo", new_url)
        repo.record_sitemap_seen.assert_called_once_with("holo:existing")
        repo.increment_sitemap_miss.assert_called_once_with("holo:gone")


class TestProductUrlNone:
    """Products with no URL stored are skipped for sitemap-miss tracking."""

    def test_product_url_none_skipped(self) -> None:
        store = _make_store()
        repo = MagicMock()
        enumerator = MagicMock()

        enumerator.enumerate_products.return_value = [
            "https://shop.example.com/products/a",
        ]
        repo.get_by_product_url.return_value = None
        repo.enqueue_url.return_value = True

        # Active product with no URL
        no_url_product = _make_product("holo:nurl", product_url=None)
        repo.list_active.return_value = [no_url_product]

        populate_queue_from_sitemap(store, repo, enumerator)

        repo.increment_sitemap_miss.assert_not_called()


class TestSitemapError:
    """Sitemap errors bubble up to caller."""

    def test_exception_propagates(self) -> None:
        store = _make_store()
        repo = MagicMock()
        enumerator = MagicMock()
        enumerator.enumerate_products.side_effect = RuntimeError("sitemap broken")

        with pytest.raises(RuntimeError, match="sitemap broken"):
            populate_queue_from_sitemap(store, repo, enumerator)

    def test_no_repo_calls_on_error(self) -> None:
        store = _make_store()
        repo = MagicMock()
        enumerator = MagicMock()
        enumerator.enumerate_products.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError):
            populate_queue_from_sitemap(store, repo, enumerator)

        repo.enqueue_url.assert_not_called()
        repo.record_sitemap_seen.assert_not_called()
        repo.increment_sitemap_miss.assert_not_called()
        repo.list_active.assert_not_called()


class TestDuplicateEnqueue:
    """enqueue_url returns False for duplicate — don't count it."""

    def test_duplicate_not_counted(self) -> None:
        store = _make_store()
        repo = MagicMock()
        enumerator = MagicMock()

        enumerator.enumerate_products.return_value = [
            "https://shop.example.com/products/dup",
        ]
        repo.get_by_product_url.return_value = None
        repo.enqueue_url.return_value = False  # already in queue
        repo.list_active.return_value = []

        result = populate_queue_from_sitemap(store, repo, enumerator)

        assert result == 0


# ---------------------------------------------------------------------------
# Tests for enqueue_stale_products
# ---------------------------------------------------------------------------


class TestEnqueueStaleProductsForceRefetch:
    """force_refetch=True enqueues ALL active products."""

    def test_enqueues_all_active(self) -> None:
        store = _make_store()
        store.fetch_interval_hours = 24.0
        repo = MagicMock()
        products = [
            _make_product("holo:1", product_url="https://shop.example.com/products/1"),
            _make_product("holo:2", product_url="https://shop.example.com/products/2"),
        ]
        repo.list_active.return_value = products
        repo.enqueue_url.return_value = True

        result = enqueue_stale_products(store, repo, force_refetch=True)

        assert result == 2
        repo.list_active.assert_called_once_with("holo")
        repo.get_products_needing_fetch.assert_not_called()

    def test_skips_products_without_url(self) -> None:
        store = _make_store()
        store.fetch_interval_hours = 24.0
        repo = MagicMock()
        products = [
            _make_product("holo:1", product_url=None),
            _make_product("holo:2", product_url="https://shop.example.com/products/2"),
        ]
        repo.list_active.return_value = products
        repo.enqueue_url.return_value = True

        result = enqueue_stale_products(store, repo, force_refetch=True)

        assert result == 1
        repo.enqueue_url.assert_called_once_with(
            "holo", "https://shop.example.com/products/2"
        )

    def test_duplicate_not_counted(self) -> None:
        store = _make_store()
        store.fetch_interval_hours = 24.0
        repo = MagicMock()
        products = [
            _make_product("holo:1", product_url="https://shop.example.com/products/1"),
        ]
        repo.list_active.return_value = products
        repo.enqueue_url.return_value = False  # already in queue

        result = enqueue_stale_products(store, repo, force_refetch=True)

        assert result == 0


class TestEnqueueStaleProductsNormal:
    """force_refetch=False uses get_products_needing_fetch."""

    def test_uses_get_products_needing_fetch(self) -> None:
        store = _make_store()
        store.fetch_interval_hours = 12.0
        repo = MagicMock()
        products = [
            _make_product("holo:1", product_url="https://shop.example.com/products/1"),
        ]
        repo.get_products_needing_fetch.return_value = products
        repo.enqueue_url.return_value = True

        result = enqueue_stale_products(store, repo, force_refetch=False)

        assert result == 1
        repo.get_products_needing_fetch.assert_called_once_with("holo", 12.0)
        repo.list_active.assert_not_called()

    def test_empty_returns_zero(self) -> None:
        store = _make_store()
        store.fetch_interval_hours = 24.0
        repo = MagicMock()
        repo.get_products_needing_fetch.return_value = []

        result = enqueue_stale_products(store, repo)

        assert result == 0
        repo.enqueue_url.assert_not_called()


# ---------------------------------------------------------------------------
# Tests for process_queue
# ---------------------------------------------------------------------------


def _make_snapshot(product_id: int = 42) -> MagicMock:
    """Create a mock ProductSnapshot returned by fetch_product."""
    snap = MagicMock()
    snap.product_id = product_id
    return snap


class TestProcessQueueSuccess:
    """Successful fetch-sync cycle."""

    def test_single_entry_ok(self) -> None:
        store = _make_store()
        repo = MagicMock()
        http_client = MagicMock()
        dify_client = MagicMock()
        snap = _make_snapshot(42)

        # peek_next returns entry once, then None
        repo.peek_next.side_effect = [
            (1, "holo", "https://shop.example.com/products/42"),
            None,
        ]

        with _patch_imports(fetch_return=snap):
            result = process_queue(store, repo, http_client, dify_client)

        assert result == {"fetched_ok": 1, "errors": 0}
        repo.reset_consecutive_failures.assert_called_once_with("holo:42")
        repo.delete_queue_entry.assert_called_once_with(1)

    def test_multiple_entries(self) -> None:
        store = _make_store()
        repo = MagicMock()
        http_client = MagicMock()
        dify_client = MagicMock()
        snap_a = _make_snapshot(10)
        snap_b = _make_snapshot(20)

        repo.peek_next.side_effect = [
            (1, "holo", "https://shop.example.com/products/10"),
            (2, "holo", "https://shop.example.com/products/20"),
            None,
        ]

        with _patch_imports(fetch_side_effect=[snap_a, snap_b]):
            result = process_queue(store, repo, http_client, dify_client)

        assert result == {"fetched_ok": 2, "errors": 0}
        assert repo.delete_queue_entry.call_count == 2

    def test_empty_queue(self) -> None:
        store = _make_store()
        repo = MagicMock()
        http_client = MagicMock()
        dify_client = MagicMock()
        repo.peek_next.return_value = None

        result = process_queue(store, repo, http_client, dify_client)

        assert result == {"fetched_ok": 0, "errors": 0}


class TestProcessQueueCircuitBreaker:
    """CircuitBreakerOpenError breaks loop, entry stays in queue."""

    def test_circuit_breaker_breaks_loop(self) -> None:
        from estimator_king.crawler.http_client import CircuitBreakerOpenError

        store = _make_store()
        repo = MagicMock()
        http_client = MagicMock()
        dify_client = MagicMock()

        repo.peek_next.side_effect = [
            (1, "holo", "https://shop.example.com/products/42"),
            (2, "holo", "https://shop.example.com/products/43"),
            None,
        ]

        cb_err = CircuitBreakerOpenError("shop.example.com", retry_in_seconds=60.0)
        with _patch_imports(fetch_side_effect=cb_err):
            result = process_queue(store, repo, http_client, dify_client)

        assert result == {"fetched_ok": 0, "errors": 0}
        repo.delete_queue_entry.assert_not_called()
        repo.increment_consecutive_failures.assert_not_called()


class TestProcessQueueErrors:
    """Non-circuit-breaker errors: delete entry, increment failures if possible."""

    def test_fetch_error_deletes_entry_no_increment(self) -> None:
        """Fetch fails before product_id is known → no increment_consecutive_failures."""
        store = _make_store()
        repo = MagicMock()
        http_client = MagicMock()
        dify_client = MagicMock()

        repo.peek_next.side_effect = [
            (1, "holo", "https://shop.example.com/products/42"),
            None,
        ]

        with _patch_imports(fetch_side_effect=RuntimeError("network fail")):
            result = process_queue(store, repo, http_client, dify_client)

        assert result == {"fetched_ok": 0, "errors": 1}
        repo.delete_queue_entry.assert_called_once_with(1)
        repo.increment_consecutive_failures.assert_not_called()

    def test_sync_error_increments_failures(self) -> None:
        """Fetch OK but sync fails → external_key known → increment failures."""
        store = _make_store()
        repo = MagicMock()
        http_client = MagicMock()
        dify_client = MagicMock()
        snap = _make_snapshot(42)

        repo.peek_next.side_effect = [
            (1, "holo", "https://shop.example.com/products/42"),
            None,
        ]

        def sync_boom(*args, **kwargs):
            raise RuntimeError("dify down")

        with _patch_imports(fetch_return=snap, sync_side_effect=sync_boom):
            result = process_queue(store, repo, http_client, dify_client)

        assert result == {"fetched_ok": 0, "errors": 1}
        repo.increment_consecutive_failures.assert_called_once_with("holo:42")
        repo.delete_queue_entry.assert_called_once_with(1)

    def test_error_then_success(self) -> None:
        """First entry errors, second succeeds."""
        store = _make_store()
        repo = MagicMock()
        http_client = MagicMock()
        dify_client = MagicMock()
        snap = _make_snapshot(20)

        repo.peek_next.side_effect = [
            (1, "holo", "https://shop.example.com/products/10"),
            (2, "holo", "https://shop.example.com/products/20"),
            None,
        ]

        call_count = 0

        def fetch_alternating(url, client):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first fails")
            return snap

        with _patch_imports(fetch_side_effect=fetch_alternating):
            result = process_queue(store, repo, http_client, dify_client)

        assert result == {"fetched_ok": 1, "errors": 1}
        assert repo.delete_queue_entry.call_args_list == [call(1), call(2)]


# ---------------------------------------------------------------------------
# Patch helper for process_queue imports
# ---------------------------------------------------------------------------


import contextlib
from unittest.mock import patch


@contextlib.contextmanager
def _patch_imports(
    *,
    fetch_return=None,
    fetch_side_effect=None,
    sync_return=None,
    sync_side_effect=None,
):
    """Patch the lazy imports inside process_queue."""
    with (
        patch(
            "estimator_king.crawler.shopify.fetch_product",
        ) as mock_fetch,
        patch(
            "estimator_king.sync.engine.sync_products",
        ) as mock_sync,
    ):
        if fetch_side_effect is not None:
            mock_fetch.side_effect = fetch_side_effect
        elif fetch_return is not None:
            mock_fetch.return_value = fetch_return

        if sync_side_effect is not None:
            mock_sync.side_effect = sync_side_effect
        elif sync_return is not None:
            mock_sync.return_value = sync_return

        yield mock_fetch, mock_sync
