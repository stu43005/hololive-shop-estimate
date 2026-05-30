"""One-time migration (2026-05-30): purge the stuck crawl queue and reset the
failure / sitemap-miss counters that the product_url fabrication bug inflated.

Run with the bot stopped (single DB writer). This script does NOT touch
product_url — the handle is not stored and cannot be reconstructed; the stored
URLs self-heal on the next normal crawl.

Usage (either form works):
    .venv/bin/python -m scripts.migrate_2026_05_30_fix_product_urls [db_path]
    .venv/bin/python scripts/migrate_2026_05_30_fix_product_urls.py [db_path]

db_path falls back to $DATABASE_PATH, then ./estimator_king.db.
"""

from __future__ import annotations

import os
import sys

# Make `estimator_king` importable when run as a plain script: `python
# scripts/x.py` puts scripts/ on sys.path[0], not the repo root, so the package
# at the repo root would otherwise be invisible. Running via `-m` already adds
# the cwd, so this insert is a harmless no-op there.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from estimator_king.database.repository import ProductStateRepository  # noqa: E402


def migrate(db_path: str) -> tuple[int, int]:
    """Purge crawl_queue and zero out inflated counters.

    Returns (queue_rows_deleted, product_rows_reset). Idempotent. The DELETE and
    UPDATE run in a single transaction so an interruption cannot leave the queue
    purged while counters stay inflated.
    """
    with ProductStateRepository(db_path) as repo:
        conn = repo.connection
        _ = conn.execute("BEGIN")
        try:
            queue_deleted = conn.execute("DELETE FROM crawl_queue").rowcount
            rows_reset = conn.execute(
                """
                UPDATE products
                SET consecutive_failures = 0,
                    consecutive_sitemap_misses = 0,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                WHERE consecutive_failures > 0 OR consecutive_sitemap_misses > 0
                """
            ).rowcount
            _ = conn.execute("COMMIT")
        except Exception:
            _ = conn.execute("ROLLBACK")
            raise
    return queue_deleted, rows_reset


def main(argv: list[str]) -> int:
    db_path = (
        argv[1] if len(argv) > 1
        else os.environ.get("DATABASE_PATH", "./estimator_king.db")
    )
    queue_deleted, rows_reset = migrate(db_path)
    print(f"crawl_queue rows deleted: {queue_deleted}")
    print(f"product counter rows reset: {rows_reset}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
