"""Bot construction: assemble a fully-configured (unstarted) Discord client.

The crawl scheduler and process lifecycle live in ``estimator_king.runtime``;
this module only knows how to build the bot (Estimator + commands + on_ready).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import discord

from estimator_king.config_schema import AppConfig
from estimator_king.bot.commands import setup_commands

if TYPE_CHECKING:
    from estimator_king.llm.embeddings import EmbeddingProvider
    from estimator_king.llm.chat import ChatProvider
    from estimator_king.llm.typing_provider import TypingProvider
    from estimator_king.vectorstore.store import VectorStore

logger = logging.getLogger(__name__)


def create_bot() -> discord.Client:
    """Create and configure the Discord client with the required intents."""
    intents = discord.Intents.default()
    intents.guilds = True
    return discord.Client(intents=intents)


def build_bot(
    config: AppConfig,
    *,
    embedder: "EmbeddingProvider",
    chat: "ChatProvider",
    vector_store: "VectorStore",
    typing_provider: "TypingProvider",
    guild_id: Optional[int],
) -> discord.Client:
    """Construct a fully-configured (but not yet started) Discord client: build
    the Estimator from injected providers, register commands, and wire the
    on_ready command-sync handler. The caller starts it via bot.start()."""
    from estimator_king.bot.estimator import Estimator

    estimator = Estimator(
        embedder, chat, vector_store, typing_provider,
        item_types=config.item_types,
        item_types_version=config.item_types_version,
    )
    bot = create_bot()
    tree = setup_commands(bot, config, estimator)

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

    return bot
