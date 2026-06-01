from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from estimator_king.crawler.async_http_client import AsyncHTTPClient, ClientError
from estimator_king.crawler.shopify import fetch_product
from estimator_king.sync.engine import sync_products

if TYPE_CHECKING:
    from estimator_king.config_schema import CrawlerPolicy, ProxyConfig
    from estimator_king.database.repository import ProductStateRepository
    from estimator_king.llm.embeddings import EmbeddingProvider
    from estimator_king.llm.typing_provider import TypingProvider
    from estimator_king.vectorstore.store import VectorStore

logger = logging.getLogger(__name__)

_PROGRESS_LOG_EVERY = 20


def _aggregate_lines(result: "PipelineResult") -> str:
    # Store-level summary always shows all five lines (zero values included),
    # unlike the per-product tree which omits a zero "excluded" clause.
    return (
        f"\n  {result.items} items"
        f"\n  {result.excluded} excluded"
        f"\n  detail hit: {result.detail_hits}"
        f"\n  typing: {result.typing_vocab}(vocab) {result.typing_cache}(cache) {result.typing_llm}(llm)"
        f"\n  embed indexed: {result.embed_indexed}"
    )


@dataclass
class PipelineResult:
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    created: int = 0
    updated: int = 0
    sync_skipped: int = 0
    items: int = 0
    excluded: int = 0
    detail_hits: int = 0
    typing_vocab: int = 0
    typing_cache: int = 0
    typing_llm: int = 0
    embed_indexed: int = 0


async def async_process_queue(
    store_id: str,
    policy: CrawlerPolicy,
    state_repo: ProductStateRepository,
    embedder: EmbeddingProvider,
    vector_store: VectorStore,
    *,
    typing_provider: TypingProvider,
    talents: frozenset[str],
    item_types: list[str],
    item_types_version: int,
    log_item_trees: bool = False,
    proxy: ProxyConfig | None = None,
) -> PipelineResult:
    entries = state_repo.peek_all(store_id)
    if not entries:
        return PipelineResult()

    logger.info("store=%s queue: %d entries to process", store_id, len(entries))

    result = PipelineResult()

    async with AsyncHTTPClient(policy, proxy=proxy) as client:

        async def _handle(entry: dict[str, int | str]) -> None:
            entry_id = int(entry["id"])
            product_url = str(entry["product_url"])
            try:
                snapshot = await fetch_product(product_url, client)
                sync_result = await asyncio.to_thread(
                    sync_products, [(product_url, snapshot)], store_id,
                    state_repo, embedder, vector_store,
                    typing_provider=typing_provider, talents=talents,
                    item_types=item_types, item_types_version=item_types_version,
                    log_item_trees=log_item_trees,
                )
                state_repo.delete_queue_entry(entry_id)
                result.created += sync_result.created
                result.updated += sync_result.updated
                result.sync_skipped += sync_result.skipped
                result.items += sync_result.items
                result.excluded += sync_result.excluded
                result.detail_hits += sync_result.detail_hits
                result.typing_vocab += sync_result.typing_vocab
                result.typing_cache += sync_result.typing_cache
                result.typing_llm += sync_result.typing_llm
                result.embed_indexed += sync_result.embed_indexed
                result.processed += 1
                if result.processed % _PROGRESS_LOG_EVERY == 0:
                    logger.info(
                        "store=%s progress: %d/%d processed%s",
                        store_id, result.processed, len(entries),
                        _aggregate_lines(result),
                    )
            except Exception as exc:
                logger.exception("Error processing %s (url=%s)", entry_id, product_url)
                existing = state_repo.get_by_product_url(store_id, product_url)
                if existing is not None:
                    state_repo.increment_consecutive_failures(existing.external_key)
                if isinstance(exc, ClientError) and exc.status_code in (404, 410):
                    # Definitively gone (HTTP 404/410): drop from queue so it is
                    # not re-fetched every cycle. Transient errors keep retrying.
                    state_repo.delete_queue_entry(entry_id)
                result.failed += 1

        queue: asyncio.Queue[dict[str, int | str]] = asyncio.Queue()
        for entry in entries:
            queue.put_nowait(entry)

        async def _worker() -> None:
            while True:
                try:
                    entry = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await _handle(entry)

        worker_count = max(1, policy.concurrency_per_domain)
        workers = [asyncio.create_task(_worker()) for _ in range(worker_count)]
        await asyncio.gather(*workers)

    logger.info(
        "store=%s done: created=%d updated=%d skipped=%d failed=%d%s",
        store_id, result.created, result.updated, result.sync_skipped, result.failed,
        _aggregate_lines(result),
    )
    return result
