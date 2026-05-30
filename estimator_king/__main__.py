"""Unified entry point: ``python -m estimator_king {run,crawl}``.

- ``run``   starts the Discord bot with the in-process crawl scheduler.
- ``crawl`` runs one crawl cycle (sitemap -> fetch -> embed -> upsert) and exits.
"""

import argparse
import asyncio
import json
import logging
import sys
from typing import Optional, Sequence

from estimator_king.config_schema import AppConfig
from estimator_king.crawler.cycle import run_crawl_cycle
from estimator_king.llm.embeddings import EmbeddingProvider
from estimator_king.vectorstore.store import VectorStore
from estimator_king.bot import runner as bot_runner


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="estimator_king",
        description="Estimator King — Discord bot and product crawler",
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="stores_config.yaml",
                        help="Path to stores configuration YAML (default: stores_config.yaml)")
    common.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                        help="Set logging level (default: INFO)")

    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", parents=[common],
                           help="Start the Discord bot with the in-process crawl scheduler")
    p_run.add_argument("--token", type=str, default=None,
                       help="Discord bot token (overrides DISCORD_TOKEN / DISCORD_BOT_TOKEN env)")
    p_run.add_argument("--guild-id", type=int, default=None,
                       help="Guild ID for command sync (optional, omit for global sync)")

    p_crawl = sub.add_parser("crawl", parents=[common],
                             help="Run one crawl cycle and exit")
    p_crawl.add_argument("--db", default=None,
                         help="Override database path from config")
    p_crawl.add_argument("--force-refetch", action="store_true", default=False,
                         help="Re-fetch all active products regardless of age")

    return parser.parse_args(argv)


def run_crawl(args: argparse.Namespace) -> None:
    """Run one crawl cycle; print JSON counters to stdout and exit.

    Exit codes: config load failure -> 1; missing embedding key -> 2;
    cycle exception -> 1; success -> 0.
    """
    try:
        config = AppConfig.from_yaml(args.config)
    except Exception as e:
        logging.error("Failed to load config from %s: %s", args.config, e)
        sys.exit(1)

    if args.db is not None:
        config.database_path = args.db
    provider_config = config.build_provider_config()
    if not provider_config.embedding_api_key:
        logging.error("OPENAI_API_KEY (or EMBEDDING_API_KEY) is required")
        sys.exit(2)

    embedder = EmbeddingProvider(provider_config)
    vector_store = VectorStore(config.chroma_path)
    try:
        counters = asyncio.run(
            run_crawl_cycle(config, config.database_path, embedder, vector_store,
                            force_refetch=args.force_refetch))
    except Exception as e:
        logging.error("Crawler failed: %s", e)
        sys.exit(1)

    print(json.dumps(counters, indent=2))
    sys.exit(0)


def run_bot(args: argparse.Namespace) -> None:
    """Load config, apply token override, then start the bot via bot_runner.run_bot.

    Exit codes: config load failure -> 1; missing discord token -> 1.
    KeyboardInterrupt exits quietly.
    """
    try:
        config = AppConfig.from_yaml(args.config)
    except Exception as e:
        sys.stderr.write(f"Error: Failed to load config: {e}\n")
        sys.exit(1)

    if args.token is not None:
        config.discord_token = args.token

    if not config.discord_token:
        sys.stderr.write("Error: --token required or set DISCORD_BOT_TOKEN / DISCORD_TOKEN\n")
        sys.exit(1)

    try:
        asyncio.run(bot_runner.run_bot(config, guild_id=args.guild_id))
    except KeyboardInterrupt:
        logging.info("Bot stopped by user")


def _main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stderr,
    )
    if args.command == "crawl":
        run_crawl(args)
    elif args.command == "run":
        run_bot(args)


if __name__ == "__main__":
    _main()
