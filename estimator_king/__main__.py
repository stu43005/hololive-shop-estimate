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
from estimator_king.runtime import serve, build_providers, MissingEmbeddingKey

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


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
    common.add_argument("--db", default=None,
                        help="Override database path from config")

    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", parents=[common],
                           help="Start the Discord bot with the in-process crawl scheduler")
    p_run.add_argument("--token", type=str, default=None,
                       help="Discord bot token (overrides DISCORD_TOKEN / DISCORD_BOT_TOKEN env)")
    p_run.add_argument("--guild-id", type=int, default=None,
                       help="Guild ID for command sync (optional, omit for global sync)")

    p_crawl = sub.add_parser("crawl", parents=[common],
                             help="Run one crawl cycle and exit")
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
        logger.error("Failed to load config from %s: %s", args.config, e)
        sys.exit(1)

    if args.db is not None:
        config.database_path = args.db
    try:
        providers = build_providers(config)
    except MissingEmbeddingKey:
        logger.error("OPENAI_API_KEY (or EMBEDDING_API_KEY) is required")
        sys.exit(2)
    try:
        counters = asyncio.run(
            run_crawl_cycle(config, config.database_path,
                            providers.embedder, providers.vector_store,
                            force_refetch=args.force_refetch))
    except Exception as e:
        logger.error("Crawler failed: %s", e)
        sys.exit(1)

    print(json.dumps(counters, indent=2))
    sys.exit(0)


def run_service(args: argparse.Namespace) -> None:
    """Run the long-lived service (bot + in-process crawl scheduler).

    Exit codes: config load failure -> 1; missing discord token -> 1;
    missing embedding key (from serve/build_providers) -> 1.
    KeyboardInterrupt exits quietly.
    """
    try:
        config = AppConfig.from_yaml(args.config)
    except Exception as e:
        sys.stderr.write(f"Error: Failed to load config: {e}\n")
        sys.exit(1)
    if args.db is not None:
        config.database_path = args.db
    if args.token is not None:
        config.discord_token = args.token
    if not config.discord_token:
        sys.stderr.write("Error: --token required or set DISCORD_BOT_TOKEN / DISCORD_TOKEN\n")
        sys.exit(1)
    try:
        asyncio.run(serve(config, guild_id=args.guild_id))
    except MissingEmbeddingKey:
        sys.stderr.write("Error: OPENAI_API_KEY (or EMBEDDING_API_KEY) is required\n")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")


def _quiet_third_party_loggers(level: int) -> None:
    """Suppress httpx's per-request INFO line unless DEBUG is requested.

    httpx (used by the OpenAI SDK underneath) logs one INFO line per request
    ("HTTP Request: ..."). Outbound requests are recorded only at DEBUG by our
    own embedding/chat logs, so keep httpx quiet at INFO and above; let it
    through when the operator opts into DEBUG.
    """
    if level > logging.DEBUG:
        logging.getLogger("httpx").setLevel(logging.WARNING)


def _main() -> None:
    args = parse_args()
    level = getattr(logging, args.log_level)
    logging.basicConfig(
        level=level,
        format=_LOG_FORMAT,
        stream=sys.stderr,
    )
    _quiet_third_party_loggers(level)
    if args.command == "crawl":
        run_crawl(args)
    elif args.command == "run":
        run_service(args)


if __name__ == "__main__":
    _main()
