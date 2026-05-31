"""In-process daily crawl scheduler. No external dependency — a guarded asyncio loop."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from estimator_king.crawler.cycle import run_crawl_cycle

if TYPE_CHECKING:
    from estimator_king.config_schema import AppConfig
    from estimator_king.llm.embeddings import EmbeddingProvider
    from estimator_king.llm.typing_provider import TypingProvider
    from estimator_king.vectorstore.store import VectorStore

logger = logging.getLogger(__name__)


class CrawlScheduler:
    def __init__(self, config: "AppConfig", db_path: str,
                 embedder: "EmbeddingProvider", vector_store: "VectorStore",
                 typing_provider: "TypingProvider") -> None:
        self._config = config
        self._db_path = db_path
        self._embedder = embedder
        self._vector_store = vector_store
        self._typing_provider = typing_provider
        self._running = False

    async def run_once(self) -> None:
        if self._running:
            logger.info("Crawl cycle already running — skipping this trigger")
            return
        self._running = True
        try:
            counters = await run_crawl_cycle(
                self._config, self._db_path, self._embedder, self._vector_store,
                self._typing_provider)
            logger.info("Crawl cycle complete: %s", counters)
        except Exception:
            logger.exception("Crawl cycle raised")
        finally:
            self._running = False

    async def run_forever(self, *, run_on_start: bool = True) -> None:
        interval = self._config.crawler.crawl_schedule_hours * 3600.0
        if run_on_start:
            await self.run_once()
        while True:
            await asyncio.sleep(interval)
            await self.run_once()
