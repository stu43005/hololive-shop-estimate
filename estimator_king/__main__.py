"""CLI argument parser and orchestration for Estimator King."""

import argparse
import asyncio
import json
import logging
import sys
from typing import Any, Optional, Sequence

from estimator_king.config_schema import AppConfig
from estimator_king.crawler.http_client import HTTPClient
from estimator_king.crawler.pipeline import (
    populate_queue_from_sitemap,
    enqueue_stale_products,
    process_queue,
)
from estimator_king.crawler.sitemap import SitemapEnumerator
from estimator_king.database.repository import ProductState, ProductStateRepository
from estimator_king.sync.dify_client import DifyKBClient
from estimator_king.sync.inactive import mark_inactive_products

# Try to import async pipeline; fall back to sync if aiohttp is unavailable
try:
    import aiohttp  # noqa: F401
    from estimator_king.crawler.async_pipeline import async_process_queue
    USE_ASYNC = True
except ImportError:
    USE_ASYNC = False
    async_process_queue = None  # type: ignore


def _product_state_normalizer(
    snapshot: Any,
    store_id: str,
    product_url: str,
    existing_state: ProductState | None,
) -> ProductState | None:
    """Create or return ProductState from a product snapshot.
    
    This normalizer is used by async_process_queue to transform fetched
    snapshots into database-storable ProductState objects.
    
    Args:
        snapshot: ProductSnapshot from fetch_product()
        store_id: Store identifier
        product_url: Product URL
        existing_state: Existing ProductState if product already in DB
        
    Returns:
        ProductState object to be upserted, or None to skip.
    """
    from estimator_king.crawler.snapshot import compute_content_hash, NORMALIZER_VERSION
    
    if snapshot is None:
        return None
    
    external_key = f"{store_id}:{snapshot.product_id}"
    content_hash = compute_content_hash(snapshot)
    
    return ProductState(
        external_key=external_key,
        content_hash=content_hash,
        normalizer_version=NORMALIZER_VERSION,
        product_url=product_url,
    )


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

    _ = parser.add_argument(
        "--force-refetch",
        action="store_true",
        default=False,
        help="Re-fetch all products regardless of last fetch time",
    )

    return parser.parse_args(args)


def run_crawler(config: AppConfig, db_path: str, dify_client: DifyKBClient, force_refetch: bool = False) -> dict:
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
        force_refetch: If True, re-fetch all products regardless of last fetch time

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

            # Phase 1: Populate queue from sitemap
            try:
                discovered = populate_queue_from_sitemap(store, repo, enumerator)
                counters["discovered"] += discovered
                logging.info(f"Discovered {discovered} new URLs for {store.id}")
            except Exception as e:
                logging.error(f"Failed to populate queue for {store.id}: {e}")
                counters["errors"] += 1
                continue

            # Phase 2: Enqueue stale products
            enqueue_stale_products(store, repo, force_refetch=force_refetch)

            # Phase 3: Fetch + Sync queue
            if USE_ASYNC:
                # Use async pipeline (converted to dict format)
                async_result = asyncio.run(
                    async_process_queue(store.id, config.crawler, repo, _product_state_normalizer)
                )
                result = {
                    "fetched_ok": async_result.processed,
                    "created": 0,
                    "updated": 0,
                    "skipped": async_result.skipped,
                    "errors": async_result.failed,
                }
            else:
                # Fall back to sync pipeline
                result = process_queue(store, repo, http_client, dify_client)
            counters["fetched_ok"] += result.get("fetched_ok", 0)
            counters["created"] += result.get("created", 0)
            counters["updated"] += result.get("updated", 0)
            counters["skipped"] += result.get("skipped", 0)
            counters["errors"] += result.get("errors", 0)

        # Phase 4: Mark inactive — called ONCE after ALL stores
        # TODO: mark_inactive_products() currently has no store-ID filter, so it evaluates
        # products from ALL stores when determining inactivity. This is a known cross-store
        # coupling issue. Fixing it requires per-store filtering in the repository — out of scope.
        try:
            inactive_result = mark_inactive_products(
                repo,
                failure_threshold=config.crawler.inactive_failure_threshold,
                miss_threshold=config.crawler.inactive_sitemap_miss_threshold,
            )
            counters["inactive"] += inactive_result.marked_inactive
            logging.info(f"Inactive check: {inactive_result.marked_inactive} marked inactive")
        except Exception as e:
            logging.error(f"Failed inactive check: {e}")
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
        counters = run_crawler(config, config.database_path, dify_client, force_refetch=args.force_refetch)
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
