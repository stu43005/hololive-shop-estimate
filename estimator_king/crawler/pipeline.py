"""Sitemap enumeration → crawl-queue population → fetch-sync pipeline."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from estimator_king.config_schema import Store
    from estimator_king.crawler.sitemap import SitemapEnumerator
    from estimator_king.database.repository import ProductStateRepository

logger = logging.getLogger(__name__)


def populate_queue_from_sitemap(
    store: Store,
    repo: ProductStateRepository,
    enumerator: SitemapEnumerator,
) -> int:
    """Enumerate sitemap URLs for a store and populate crawl_queue.

    For each URL from the sitemap:
      - If product NOT in DB (no state): enqueue_url()
      - If product IS in DB: record_sitemap_seen()

    For each existing active product NOT in sitemap:
      - increment_sitemap_miss()

    Edge cases:
      - 0 URLs from sitemap: log warning, return 0 (do NOT mark any products as missed)
      - Sitemap error: exception bubbles up to caller

    Returns:
        Number of URLs newly enqueued.
    """
    # Step 1: enumerate product URLs from sitemap
    sitemap_urls = enumerator.enumerate_products(store.base_url)

    # Step 2: empty sitemap → early return
    if not sitemap_urls:
        logger.warning("Sitemap for %s returned 0 URLs — skipping", store.id)
        return 0

    sitemap_url_set = set(sitemap_urls)
    enqueued = 0

    # Step 3: process each sitemap URL
    for url in sitemap_urls:
        existing = repo.get_by_product_url(store.id, url)
        if existing is None:
            if repo.enqueue_url(store.id, url):
                enqueued += 1
        else:
            repo.record_sitemap_seen(existing.external_key)

    # Step 4: detect products missing from sitemap
    active_products = repo.list_active(store.id)
    for product in active_products:
        if product.product_url is None:
            continue
        if product.product_url not in sitemap_url_set:
            repo.increment_sitemap_miss(product.external_key)

    return enqueued


def enqueue_oldest_products(store: Store, repo: ProductStateRepository, *, limit: int) -> int:
    """Enqueue up to `limit` existing active products, oldest last_fetch first."""
    if limit <= 0:
        return 0
    enqueued = 0
    for state in repo.get_oldest_active_products(store.id, limit):
        if repo.enqueue_url(store.id, state.product_url):
            enqueued += 1
    return enqueued
