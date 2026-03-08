from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, cast

from estimator_king.crawler.async_http_client import AsyncHTTPClient
from estimator_king.crawler.shopify import fetch_product

if TYPE_CHECKING:
    from estimator_king.config_schema import CrawlerPolicy
    from estimator_king.database.repository import ProductState, ProductStateRepository


logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    processed: int = 0
    failed: int = 0
    skipped: int = 0


class _AsyncToSyncHTTPAdapter:
    def __init__(self, client: AsyncHTTPClient, loop: asyncio.AbstractEventLoop):
        self._client: AsyncHTTPClient = client
        self._loop: asyncio.AbstractEventLoop = loop

    def get(self, url: str):
        text = asyncio.run_coroutine_threadsafe(
            self._client.get(url), self._loop
        ).result()
        return type("_Resp", (), {"status_code": 200, "text": text})()


async def async_process_queue(
    store_id: str,
    policy: CrawlerPolicy,
    state_repo: ProductStateRepository,
    normalizer: Callable[[Any, str, str, ProductState | None], ProductState | None],
) -> PipelineResult:
    entries = state_repo.peek_all(store_id)
    if not entries:
        return PipelineResult()

    loop = asyncio.get_running_loop()
    result = PipelineResult()
    lock = asyncio.Lock()

    async with AsyncHTTPClient(policy) as client:
        adapter = _AsyncToSyncHTTPAdapter(client, loop)
        fetch_with_adapter = cast(Callable[[str, Any], Any], fetch_product)

        async def _handle(entry: dict[str, int | str]) -> None:
            entry_id = int(entry["id"])
            product_url = str(entry["product_url"])
            try:
                snapshot = await asyncio.to_thread(
                    fetch_with_adapter,
                    product_url,
                    adapter,
                )
                external_key = f"{store_id}:{snapshot.product_id}"
                existing_state = state_repo.get_by_external_key(external_key)
                normalized = normalizer(
                    snapshot,
                    store_id,
                    product_url,
                    existing_state,
                )

                if normalized is None:
                    state_repo.delete_queue_entry(entry_id)
                    async with lock:
                        result.skipped += 1
                    return

                state_repo.upsert(normalized)
                state_repo.delete_queue_entry(entry_id)
                async with lock:
                    result.processed += 1
            except Exception:
                logger.exception(
                    "Error processing async queue entry %s (url=%s)",
                    entry_id,
                    product_url,
                )
                async with lock:
                    result.failed += 1

        pipeline_sem = asyncio.Semaphore(policy.concurrency_per_domain)
        async def _bounded_handle(entry: tuple) -> None:
            async with pipeline_sem:
                await _handle(entry)
        await asyncio.gather(*[_bounded_handle(entry) for entry in entries])

    return result
