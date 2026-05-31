"""Bot runtime: build providers, register commands, start the scheduler and bot.

Extracted from the former ``estimator_king.bot.__main__`` so the unified
``python -m estimator_king`` dispatcher can start the bot via ``run_bot()``.
"""

import asyncio
import logging
import os
import signal
import sys
from typing import Callable, Optional

import discord

from estimator_king.config_schema import AppConfig
from estimator_king.bot.commands import setup_commands

logger = logging.getLogger(__name__)

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

    _scheduler_task: asyncio.Task[None]
    _bot: discord.Client
    _force_exit: Callable[[int], None]
    _requested: bool

    def __init__(
        self,
        scheduler_task: asyncio.Task[None],
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


def create_bot() -> discord.Client:
    """Create and configure the Discord client with the required intents."""
    intents = discord.Intents.default()
    intents.guilds = True
    return discord.Client(intents=intents)


async def run_bot(config: AppConfig, *, guild_id: Optional[int]) -> None:
    """Build providers, register commands, start the crawl scheduler and the bot.

    The caller is responsible for loading ``config`` and applying any token
    override before calling this; here we only receive a ready ``config`` and
    the optional ``guild_id`` for command sync.
    """
    from estimator_king.llm.embeddings import EmbeddingProvider
    from estimator_king.llm.chat import ChatProvider
    from estimator_king.vectorstore.store import VectorStore
    from estimator_king.bot.estimator import Estimator
    from estimator_king.crawler.scheduler import CrawlScheduler

    provider_config = config.build_provider_config()
    if not provider_config.embedding_api_key:
        sys.stderr.write("Error: OPENAI_API_KEY (or EMBEDDING_API_KEY) is required\n")
        sys.exit(1)

    embedder = EmbeddingProvider(provider_config)
    chat = ChatProvider(provider_config)
    vector_store = VectorStore(config.chroma_path)
    estimator = Estimator(embedder, chat, vector_store)

    bot = create_bot()
    tree = setup_commands(bot, config, estimator)

    scheduler = CrawlScheduler(config, config.database_path, embedder, vector_store)
    scheduler_task = asyncio.create_task(scheduler.run_forever())
    _background_tasks.add(scheduler_task)
    scheduler_task.add_done_callback(_background_tasks.discard)

    @bot.event
    async def on_ready() -> None:
        assert bot.user is not None
        logger.info(f"Logged in as {bot.user}")
        if guild_id:
            guild = discord.Object(id=guild_id)
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
            logger.info(f"Synced commands to guild {guild_id}")
        else:
            await tree.sync()
            logger.info("Synced commands globally")
        logger.info("Bot ready and commands synchronized")

    shutdowner = _Shutdowner(scheduler_task, bot)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdowner.handle_signal)

    assert config.discord_token is not None
    await bot.start(config.discord_token)
