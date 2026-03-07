"""Sitemap enumeration → crawl-queue population → fetch-sync pipeline."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from estimator_king.config_schema import Store
    from estimator_king.crawler.http_client import HTTPClient
    from estimator_king.crawler.sitemap import SitemapEnumerator
    from estimator_king.database.repository import ProductStateRepository
    from estimator_king.sync.dify_client import DifyKBClient

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


def enqueue_stale_products(
    store: Store,
    repo: ProductStateRepository,
    *,
    force_refetch: bool = False,
) -> int:
    """Enqueue products that need re-fetching into the crawl queue.

    If *force_refetch* is True, enqueue ALL active products for the store.
    Otherwise, enqueue only products whose last fetch is older than the
    store's configured ``fetch_interval_hours``.

    Returns:
        Number of URLs newly enqueued (where ``enqueue_url()`` returned True).
    """
    if force_refetch:
        products = repo.list_active(store.id)
    else:
        products = repo.get_products_needing_fetch(
            store.id, store.fetch_interval_hours
        )

    enqueued = 0
    for state in products:
        if state.product_url is None:
            continue
        if repo.enqueue_url(store.id, state.product_url):
            enqueued += 1

    return enqueued


def process_queue(
    store: Store,
    repo: ProductStateRepository,
    http_client: HTTPClient,
    dify_client: DifyKBClient,
) -> dict[str, int]:
    """Drain the crawl queue: fetch each product and sync to Dify.

    Uses ``peek_next()`` / ``delete_queue_entry()`` for crash-safe processing.
    On :class:`CircuitBreakerOpenError` the loop breaks immediately, leaving
    remaining entries in the queue for the next run.

    Returns:
        ``{"fetched_ok": N, "created": N, "updated": N, "skipped": N, "errors": N}``
    """
    from estimator_king.crawler.http_client import CircuitBreakerOpenError
    from estimator_king.crawler.shopify import fetch_product
    from estimator_king.sync.engine import sync_products

    fetched_ok = 0
    errors = 0
    created = 0
    updated = 0
    skipped = 0

    while (entry := repo.peek_next(store.id)) is not None:
        entry_id, _, product_url = entry
        external_key: str | None = None
        try:
            snapshot = fetch_product(product_url, http_client)
            product_id = snapshot.product_id
            external_key = f"{store.id}:{product_id}"
            sync_result = sync_products(
                [snapshot], store.id, store.base_url, repo, dify_client
            )
            created += sync_result.created
            updated += sync_result.updated
            skipped += sync_result.skipped
            repo.reset_consecutive_failures(external_key)
            repo.delete_queue_entry(entry_id)
            fetched_ok += 1
        except CircuitBreakerOpenError:
            logger.info(
                "Circuit breaker open for %s — pausing queue processing",
                store.id,
            )
            break
        except Exception:
            logger.exception(
                "Error processing queue entry %s (url=%s)",
                entry_id,
                product_url,
            )
            if external_key is not None:
                repo.increment_consecutive_failures(external_key)
            # Do NOT delete queue entry on error — leave it for resumability.
            errors += 1

    return {"fetched_ok": fetched_ok, "created": created, "updated": updated, "skipped": skipped, "errors": errors}
