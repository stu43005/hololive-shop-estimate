"""Tests for estimator_king.crawler.pipeline."""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

from estimator_king.crawler.pipeline import populate_queue_from_sitemap
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
