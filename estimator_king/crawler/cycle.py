"""One full crawl cycle: per-store sitemap + budget enqueue + drain, then a
single cross-store inactive sweep. Shared by the CLI and the bot scheduler."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from estimator_king.crawler.async_http_client import AsyncHTTPClient
from estimator_king.crawler.async_pipeline import async_process_queue
from estimator_king.crawler.pipeline import enqueue_oldest_products, populate_queue_from_sitemap
from estimator_king.crawler.sitemap import SitemapEnumerator
from estimator_king.database.repository import ProductStateRepository
from estimator_king.sync.inactive import mark_inactive_products

if TYPE_CHECKING:
    from estimator_king.config_schema import AppConfig
    from estimator_king.llm.embeddings import EmbeddingProvider
    from estimator_king.llm.typing_provider import TypingProvider
    from estimator_king.vectorstore.store import VectorStore

logger = logging.getLogger(__name__)


async def run_crawl_cycle(
    config: "AppConfig",
    db_path: str,
    embedder: "EmbeddingProvider",
    vector_store: "VectorStore",
    typing_provider: "TypingProvider",
    *,
    force_refetch: bool = False,
) -> dict[str, int]:
    counters = {"discovered": 0, "fetched_ok": 0, "created": 0, "updated": 0,
                "skipped": 0, "inactive": 0, "errors": 0}

    with ProductStateRepository(db_path) as repo:
        async with AsyncHTTPClient(config.crawler, proxy=config.proxy) as sitemap_client:
            enumerator = SitemapEnumerator(http_client=sitemap_client)

            for store in config.stores:
                logger.info("Processing store %s", store.id)
                try:
                    new_count = await populate_queue_from_sitemap(store, repo, enumerator)
                    counters["discovered"] += new_count
                except Exception:
                    logger.exception("Sitemap failed for %s", store.id)
                    counters["errors"] += 1
                    continue

                if force_refetch:
                    for state in repo.list_active(store.id):
                        repo.enqueue_url(store.id, state.product_url)
                else:
                    remaining = max(0, config.crawler.max_products_per_run - new_count)
                    enqueue_oldest_products(store, repo, limit=remaining)

                try:
                    result = await async_process_queue(
                        store.id, config.crawler, repo, embedder, vector_store,
                        typing_provider=typing_provider, talents=config.talents,
                        item_types=config.item_types,
                        item_types_version=config.item_types_version,
                        proxy=config.proxy)
                    counters["fetched_ok"] += result.processed
                    counters["created"] += result.created
                    counters["updated"] += result.updated
                    counters["skipped"] += result.sync_skipped
                    counters["errors"] += result.failed
                except Exception:
                    logger.exception("Queue processing failed for %s", store.id)
                    counters["errors"] += 1

        try:
            inactive_result = mark_inactive_products(
                repo, vector_store,
                failure_threshold=config.crawler.inactive_failure_threshold,
                miss_threshold=config.crawler.inactive_sitemap_miss_threshold,
            )
            counters["inactive"] += inactive_result.marked_inactive
        except Exception:
            logger.exception("Inactive sweep failed")
            counters["errors"] += 1

    return counters
