"""CLI entrypoint: run one crawl cycle (sitemap → fetch → embed → upsert)."""

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


def parse_args(args: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="estimator_king",
        description="Estimator King crawler — sync products to the local vector store",
    )
    parser.add_argument("--config", default="stores_config.yaml")
    parser.add_argument("--db", default=None)
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    parser.add_argument("--force-refetch", action="store_true", default=False)
    return parser.parse_args(args)


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s - %(levelname)s - %(message)s", stream=sys.stderr)
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


def _main() -> None:
    main()


if __name__ == "__main__":
    _main()
