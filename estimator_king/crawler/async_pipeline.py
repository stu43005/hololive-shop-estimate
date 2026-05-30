from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, cast

from estimator_king.crawler.async_http_client import AsyncHTTPClient
from estimator_king.crawler.shopify import fetch_product
from estimator_king.sync.engine import sync_products

if TYPE_CHECKING:
    from estimator_king.config_schema import CrawlerPolicy, ProxyConfig
    from estimator_king.database.repository import ProductStateRepository
    from estimator_king.llm.embeddings import EmbeddingProvider
    from estimator_king.vectorstore.store import VectorStore

logger = logging.getLogger(__name__)

_PROGRESS_LOG_EVERY = 20


@dataclass
class PipelineResult:
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    created: int = 0
    updated: int = 0
    sync_skipped: int = 0


class _AsyncToSyncHTTPAdapter:
    def __init__(self, client: AsyncHTTPClient, loop: asyncio.AbstractEventLoop):
        self._client = client
        self._loop = loop

    def get(self, url: str):
        text = asyncio.run_coroutine_threadsafe(self._client.get(url), self._loop).result()
        return type("_Resp", (), {"status_code": 200, "text": text})()


async def async_process_queue(
    store_id: str,
    store_base_url: str,
    policy: CrawlerPolicy,
    state_repo: ProductStateRepository,
    embedder: EmbeddingProvider,
    vector_store: VectorStore,
    *,
    proxy: ProxyConfig | None = None,
) -> PipelineResult:
    entries = state_repo.peek_all(store_id)
    if not entries:
        return PipelineResult()

    logger.info("store=%s queue: %d entries to process", store_id, len(entries))

    loop = asyncio.get_running_loop()
    result = PipelineResult()
    lock = asyncio.Lock()

    async with AsyncHTTPClient(policy, proxy=proxy) as client:
        adapter = _AsyncToSyncHTTPAdapter(client, loop)
        fetch_with_adapter = cast(Callable[[str, Any], Any], fetch_product)

        async def _handle(entry: dict[str, int | str]) -> None:
            entry_id = int(entry["id"])
            product_url = str(entry["product_url"])
            try:
                snapshot = await asyncio.to_thread(fetch_with_adapter, product_url, adapter)
                sync_result = await asyncio.to_thread(
                    sync_products, [snapshot], store_id, store_base_url,
                    state_repo, embedder, vector_store,
                )
                state_repo.delete_queue_entry(entry_id)
                async with lock:
                    result.created += sync_result.created
                    result.updated += sync_result.updated
                    result.sync_skipped += sync_result.skipped
                    result.processed += 1
                    if result.processed % _PROGRESS_LOG_EVERY == 0:
                        logger.info(
                            "store=%s progress: %d/%d processed",
                            store_id, result.processed, len(entries),
                        )
            except Exception:
                logger.exception("Error processing %s (url=%s)", entry_id, product_url)
                existing = state_repo.get_by_product_url(store_id, product_url)
                if existing is not None:
                    state_repo.increment_consecutive_failures(existing.external_key)
                async with lock:
                    result.failed += 1
                # queue entry intentionally kept for retry

        sem = asyncio.Semaphore(policy.concurrency_per_domain)

        async def _bounded(entry: dict[str, int | str]) -> None:
            async with sem:
                await _handle(entry)

        await asyncio.gather(*[_bounded(entry) for entry in entries])

    logger.info(
        "store=%s done: created=%d updated=%d skipped=%d failed=%d",
        store_id, result.created, result.updated, result.sync_skipped, result.failed,
    )
    return result
