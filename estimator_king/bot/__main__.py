"""CLI entrypoint for Estimator King Discord bot.

Provides argument parsing, bot initialization, command registration, and
graceful shutdown handling for running the Discord bot as a module.
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import Optional

import discord
from discord import app_commands

from estimator_king.bot.commands import setup_commands


def parse_args(args: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments for bot configuration.

    Supports:
    - Command-line arguments (highest priority)
    - Environment variables (fallback)
    - Default values (lowest priority)

    Args:
        args: Optional list of arguments to parse (for testing).
              If None, uses sys.argv[1:].

    Returns:
        argparse.Namespace: Parsed arguments with attributes:
            - token: Discord bot token
            - guild_id: Optional guild ID for command sync

    Raises:
        SystemExit: If required arguments are missing.
    """
    parser = argparse.ArgumentParser(
        prog="estimator_king.bot",
        description="Estimator King Discord Bot - Price estimation and interactions",
    )

    parser.add_argument(
        "--token",
        type=str,
        default=os.environ.get("DISCORD_BOT_TOKEN"),
        help="Discord bot token (or set DISCORD_BOT_TOKEN env var)",
    )

    parser.add_argument(
        "--guild-id",
        type=int,
        default=None,
        help="Guild ID for command sync (optional, omit for global sync)",
    )

    return parser.parse_args(args)


def create_bot() -> discord.Client:
    """Create and configure Discord bot client with required intents.

    Intents enabled:
    - message_content: Allows reading message content from DMs/guilds
    - guilds: Allows access to guild information

    Returns:
        discord.Client: Configured bot instance ready for command registration
    """
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    return discord.Client(intents=intents)


async def main() -> None:
    """Main async entrypoint: parse args, create bot, register commands, sync.

    Workflow:
    1. Parse command-line arguments and validate token
    2. Create Discord bot client with required intents
    3. Initialize and register commands via setup_commands()
    4. Register on_ready event for command synchronization
    5. Set up graceful shutdown handlers for SIGINT/SIGTERM
    6. Start bot connection to Discord
    7. On ready: Sync commands (guild-specific or global)

    Guild vs Global Sync:
    - With --guild-id: Fast sync to specific guild (instant, for dev)
    - Without --guild-id: Global sync (up to 1 hour propagation, for prod)
    """
    # Parse arguments
    args = parse_args()

    # Validate token
    if not args.token:
        sys.stderr.write("Error: --token required or set DISCORD_BOT_TOKEN\n")
        sys.exit(1)

    # Create bot and register commands
    bot = create_bot()
    tree = setup_commands(bot)

    # on_ready event: sync commands after bot connects
    @bot.event
    async def on_ready() -> None:
        """Sync commands to Discord after bot is ready.

        Behavior depends on --guild-id:
        - If guild_id specified: Copy global commands to guild and sync
        - If no guild_id: Sync to global scope (slower propagation)
        """
        assert bot.user is not None
        logging.info(f"Logged in as {bot.user}")

        if args.guild_id:
            # Guild sync: fast (instant) but only for specific guild
            guild = discord.Object(id=args.guild_id)
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
            logging.info(f"Synced commands to guild {args.guild_id}")
        else:
            # Global sync: slower (up to 1 hour) but available everywhere
            await tree.sync()
            logging.info("Synced commands globally")

        logging.info("Bot ready and commands synchronized")

    # Graceful shutdown handler
    async def shutdown() -> None:
        """Gracefully shutdown bot on signal.

        Closes bot connection cleanly without abrupt termination.
        """
        logging.info("Shutting down bot...")
        await bot.close()

    # Register signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))

    # Start bot connection
    await bot.start(args.token)


def _main() -> None:
    """Entry point for python -m estimator_king.bot."""
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Run async main
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot stopped by user")


if __name__ == "__main__":
    _main()
