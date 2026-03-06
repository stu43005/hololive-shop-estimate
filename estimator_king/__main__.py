"""CLI argument parser and orchestration for Estimator King."""

import argparse
import json
import logging
import sys
from typing import Optional, Sequence

from estimator_king.config_schema import AppConfig
from estimator_king.crawler.http_client import HTTPClient
from estimator_king.crawler.shopify import fetch_product
from estimator_king.crawler.sitemap import SitemapEnumerator
from estimator_king.database.repository import ProductStateRepository
from estimator_king.sync.dify_client import DifyKBClient
from estimator_king.sync.engine import sync_products
from estimator_king.sync.inactive import mark_inactive_products


def parse_args(args: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments with environment variable fallbacks.

    CLI args are used to override values loaded from config/env.
    Validation of required credentials is done in main() after
    merging CLI args into AppConfig.

    Args:
        args: Optional list of arguments to parse (for testing).
              If None, uses sys.argv[1:].

    Returns:
        argparse.Namespace: Parsed arguments with attributes:
            - config: Path to stores configuration YAML
            - db: SQLite database file path (or None)
            - dify_base_url: Dify API base URL (or None)
            - dify_api_key: Dify API key for dataset (or None)
            - dify_dataset_id: Dify dataset UUID (or None)
    """
    parser = argparse.ArgumentParser(
        prog="estimator_king",
        description="Estimator King Shopify Crawler - Sync products to Dify KB",
    )

    _ = parser.add_argument(
        "--config",
        default="stores_config.yaml",
        help="Path to stores configuration YAML (default: stores_config.yaml)",
    )

    _ = parser.add_argument(
        "--db",
        default=None,
        help="SQLite database path (overrides DATABASE_PATH env / config default)",
    )

    _ = parser.add_argument(
        "--dify-base-url",
        default=None,
        help="Dify API base URL (overrides DIFY_BASE_URL env)",
    )

    _ = parser.add_argument(
        "--dify-api-key",
        default=None,
        help="Dify API key for dataset (overrides DIFY_API_KEY env)",
    )

    _ = parser.add_argument(
        "--dify-dataset-id",
        default=None,
        help="Dify dataset UUID (overrides DIFY_DATASET_ID env)",
    )

    _ = parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set logging level (default: INFO)",
    )

    return parser.parse_args(args)


def run_crawler(config: AppConfig, db_path: str, dify_client: DifyKBClient) -> dict:
    """Orchestrate full crawler pipeline: sitemap → fetch → sync → inactive.

    Processes all stores in sequence:
    1. Enumerate products from sitemap
    2. Fetch product details (JSON + HTML)
    3. Sync to Dify Knowledge Base
    4. Mark inactive products based on failure thresholds

    Args:
        config: Parsed stores configuration with store list and crawler policy
        db_path: SQLite database file path
        dify_client: Initialized Dify Knowledge Base client

    Returns:
        dict with aggregated counters:
            - discovered: Total products found in sitemaps
            - fetched_ok: Successfully fetched products
            - created: New documents created in Dify
            - updated: Existing documents updated in Dify
            - skipped: Unchanged documents (no update needed)
            - inactive: Products marked inactive
            - errors: Failed operations
    """
    counters = {
        "discovered": 0,
        "fetched_ok": 0,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "inactive": 0,
        "errors": 0,
    }

    with ProductStateRepository(db_path) as repo:
        http_client = HTTPClient(
            crawler_policy=config.crawler,
            proxy=config.proxy,
        )
        enumerator = SitemapEnumerator(http_client=http_client)

        for store in config.stores:
            logging.info(f"Processing store: {store.id} ({store.base_url})")

            try:
                product_urls = enumerator.enumerate_products(store.base_url)
                counters["discovered"] += len(product_urls)
                logging.info(f"Discovered {len(product_urls)} products from {store.id}")
            except Exception as e:
                logging.error(f"Failed to enumerate sitemap for {store.id}: {e}")
                counters["errors"] += 1
                continue

            snapshots = []
            for url in product_urls:
                try:
                    snapshot = fetch_product(url, http_client)
                    snapshots.append(snapshot)
                    counters["fetched_ok"] += 1
                except Exception as e:
                    logging.error(f"Failed to fetch {url}: {e}")
                    counters["errors"] += 1

            try:
                sync_result = sync_products(snapshots, store.id, repo, dify_client)
                counters["created"] += sync_result.created
                counters["updated"] += sync_result.updated
                counters["skipped"] += sync_result.skipped
                counters["errors"] += sync_result.failed
                logging.info(
                    f"Sync completed for {store.id}: "
                    f"+{sync_result.created} created, "
                    f"~{sync_result.updated} updated, "
                    f"={sync_result.skipped} skipped"
                )
            except Exception as e:
                logging.error(f"Failed to sync {store.id}: {e}")
                counters["errors"] += 1

            try:
                inactive_result = mark_inactive_products(repo)
                counters["inactive"] += inactive_result.marked_inactive
                logging.info(
                    f"Inactive check for {store.id}: "
                    f"{inactive_result.marked_inactive} marked inactive"
                )
            except Exception as e:
                logging.error(f"Failed inactive check for {store.id}: {e}")
                counters["errors"] += 1

    return counters


def main() -> None:
    """Main entrypoint: parse args, load config, run crawler, output JSON.

    Workflow:
    1. Configure logging (INFO level to stderr)
    2. Parse command-line arguments
    3. Load AppConfig from YAML file + environment variables
    4. Override config with CLI arguments (if provided)
    5. Validate crawler-required credentials
    6. Initialize DifyKBClient from config
    7. Run crawler pipeline (orchestrate all stores)
    8. Output JSON results to stdout
    9. Exit with appropriate code (0=success, 1=error)
    """
    # 1. Parse arguments
    args = parse_args()

    # 2. Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
    )

    # 3. Load config from YAML + env
    try:
        config = AppConfig.from_yaml(args.config)
        logging.info(f"Loaded config from {args.config}: {len(config.stores)} stores")
    except Exception as e:
        logging.error(f"Failed to load config from {args.config}: {e}")
        sys.exit(1)

    # 4. Override config with CLI arguments (if provided)
    if args.dify_api_key is not None:
        config.dify_api_key = args.dify_api_key
    if args.dify_base_url is not None:
        config.dify_base_url = args.dify_base_url
    if args.dify_dataset_id is not None:
        config.dify_dataset_id = args.dify_dataset_id
    if args.db is not None:
        config.database_path = args.db

    # 5. Validate crawler-required credentials
    if not config.dify_api_key:
        logging.error("DIFY_API_KEY is required (via env, config, or --dify-api-key)")
        sys.exit(2)
    if not config.dify_base_url:
        logging.error("DIFY_BASE_URL is required (via env, config, or --dify-base-url)")
        sys.exit(2)
    if not config.dify_dataset_id:
        logging.error("DIFY_DATASET_ID is required (via env, config, or --dify-dataset-id)")
        sys.exit(2)

    # 6. Initialize Dify client from config
    dify_client = DifyKBClient(
        api_key=config.dify_api_key,
        base_url=config.dify_base_url,
        dataset_id=config.dify_dataset_id,
    )

    # 7. Run crawler
    try:
        counters = run_crawler(config, config.database_path, dify_client)
        logging.info(f"Crawler completed: {counters}")
    except Exception as e:
        logging.error(f"Crawler failed: {e}")
        sys.exit(1)

    # 8. Output JSON to stdout
    print(json.dumps(counters, indent=2))

    # 9. Exit success
    sys.exit(0)


def _main() -> None:
    """Entry point for python -m estimator_king."""
    main()


if __name__ == "__main__":
    _main()
