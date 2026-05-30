"""Maintenance script: purge the crawl queue.

``crawl_queue`` is a work-to-do queue, not authoritative state — clearing it
loses no data. Product rows self-heal on the next normal crawl (the stored
``product_url`` is rewritten when the default-locale URL is fetched again). Use
this to clear a queue that has been flooded (e.g. by a sitemap-locale-filter
bug) before re-crawling.

Run with the bot stopped (single DB writer).

Usage (either form works):
    .venv/bin/python -m scripts.clean_crawl_queue [--db PATH] [--store STORE_ID] [--dry-run]
    .venv/bin/python scripts/clean_crawl_queue.py [--db PATH] [--store STORE_ID] [--dry-run]

--db falls back to $DATABASE_PATH, then ./estimator_king.db.
"""

from __future__ import annotations

import argparse
import os
import sys

# Make `estimator_king` importable when run as a plain script: `python
# scripts/x.py` puts scripts/ on sys.path[0], not the repo root. Running via
# `-m` already adds the cwd, so this insert is a harmless no-op there.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from estimator_king.database.repository import ProductStateRepository  # noqa: E402


def clean(
    db_path: str, *, store_id: str | None = None, dry_run: bool = False
) -> tuple[int, int]:
    """Clear crawl_queue (optionally scoped to one store).

    Returns (queue_size_before, rows_deleted). On dry-run, rows_deleted is 0 and
    the queue is left untouched.
    """
    with ProductStateRepository(db_path) as repo:
        before = repo.queue_size(store_id)
        if dry_run:
            return before, 0
        deleted = repo.clear_queue(store_id)
        return before, deleted


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="clean_crawl_queue",
        description="Purge the crawl_queue (run with the bot stopped).",
    )
    parser.add_argument(
        "--db", default=None,
        help="SQLite path (default: $DATABASE_PATH, then ./estimator_king.db)",
    )
    parser.add_argument(
        "--store", default=None,
        help="Only clear this store_id (default: all stores)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report counts without deleting",
    )
    args = parser.parse_args(argv[1:])

    db_path = args.db or os.environ.get("DATABASE_PATH", "./estimator_king.db")
    before, deleted = clean(db_path, store_id=args.store, dry_run=args.dry_run)

    scope = f"store={args.store}" if args.store else "all stores"
    if args.dry_run:
        print(f"crawl_queue rows ({scope}): {before} (dry-run, nothing deleted)")
    else:
        print(f"crawl_queue rows ({scope}) before: {before}")
        print(f"crawl_queue rows deleted: {deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
