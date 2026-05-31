"""Composition root: shared provider construction and the long-lived service.

``build_providers`` is the single place that constructs the embedding / chat /
vector-store providers, shared by both the ``run`` and ``crawl`` commands. The
``serve`` composition root wires the bot and crawl scheduler as
two independent components over one shared VectorStore.
"""

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from typing import Callable, Optional

import discord

from estimator_king.config_schema import AppConfig
from estimator_king.llm.chat import ChatProvider
from estimator_king.llm.embeddings import EmbeddingProvider
from estimator_king.llm.typing_provider import TypingProvider
from estimator_king.vectorstore.store import VectorStore
from estimator_king.crawler.scheduler import CrawlScheduler
from estimator_king.bot.runner import build_bot

logger = logging.getLogger(__name__)


class MissingEmbeddingKey(Exception):
    """Raised by build_providers when no embedding API key is configured.

    The caller maps this to its own exit code (crawl -> 2, run -> 1) so the
    validation lives in one place while CLI exit semantics stay per-command.
    """


@dataclass
class Providers:
    embedder: EmbeddingProvider
    vector_store: VectorStore
    typing_provider: TypingProvider
    chat: Optional[ChatProvider] = None


def build_providers(config: AppConfig, *, with_chat: bool = False) -> Providers:
    """Construct the shared providers; raise MissingEmbeddingKey if no key.

    chat is only built when with_chat=True (the bot needs it; crawl does not).
    Building ChatProvider with an empty chat_api_key raises OpenAIError under
    openai>=2, so crawl must never request it.
    """
    provider_config = config.build_provider_config()
    if not provider_config.embedding_api_key:
        raise MissingEmbeddingKey()
    embedder = EmbeddingProvider(provider_config)
    vector_store = VectorStore(config.chroma_path)
    typing_provider = TypingProvider(provider_config)
    chat = ChatProvider(provider_config) if with_chat else None
    return Providers(embedder=embedder, vector_store=vector_store,
                     typing_provider=typing_provider, chat=chat)


# Strong references to background tasks: asyncio only keeps a weak reference, so
# an unreferenced create_task() result can be garbage-collected mid-run.
_background_tasks: set["asyncio.Task[None]"] = set()


def _force_exit(code: int) -> None:  # pragma: no cover - replaced via injection in tests
    os._exit(code)


_default_force_exit: Callable[[int], None] = _force_exit


class _Shutdowner:
    """Two-stage shutdown: first signal cancels the scheduler and closes the
    bot gracefully; a second signal forces an immediate exit (escape hatch for
    in-flight blocking work that cannot be cancelled cooperatively)."""

    _scheduler_task: "asyncio.Task[None]"
    _bot: discord.Client
    _force_exit: Callable[[int], None]
    _requested: bool

    def __init__(
        self,
        scheduler_task: "asyncio.Task[None]",
        bot: discord.Client,
        *,
        force_exit: Callable[[int], None] = _default_force_exit,
    ) -> None:
        self._scheduler_task = scheduler_task
        self._bot = bot
        self._force_exit = force_exit
        self._requested = False

    def handle_signal(self) -> None:
        if self._requested:
            logger.warning("Forced shutdown (second interrupt)")
            self._force_exit(130)
            return
        self._requested = True
        logger.info("Shutdown requested; press Ctrl+C again to force quit")
        task = asyncio.create_task(self.shutdown())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    async def shutdown(self) -> None:
        logger.info("Shutting down bot...")
        self._scheduler_task.cancel()
        try:
            await self._scheduler_task
        except asyncio.CancelledError:
            pass
        await self._bot.close()


async def serve(config: AppConfig, *, guild_id: Optional[int]) -> None:
    """Composition root for ``run``: build shared providers once, then run the
    Discord bot and the crawl scheduler as two independent components over one
    shared VectorStore, with coordinated two-stage graceful shutdown."""
    providers = build_providers(config, with_chat=True)
    assert providers.chat is not None

    scheduler = CrawlScheduler(
        config, config.database_path, providers.embedder, providers.vector_store,
        providers.typing_provider)
    scheduler_task = asyncio.create_task(scheduler.run_forever())
    _background_tasks.add(scheduler_task)
    scheduler_task.add_done_callback(_background_tasks.discard)

    bot = build_bot(
        config,
        embedder=providers.embedder,
        chat=providers.chat,
        vector_store=providers.vector_store,
        typing_provider=providers.typing_provider,
        guild_id=guild_id,
    )

    shutdowner = _Shutdowner(scheduler_task, bot)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdowner.handle_signal)

    assert config.discord_token is not None
    await bot.start(config.discord_token)
