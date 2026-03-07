from __future__ import annotations

# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownParameterType=false
# pyright: reportUntypedFunctionDecorator=false
# pyright: reportMissingImports=false

import sqlite3

import pytest  # pyright: ignore[reportMissingImports]

from estimator_king.database.repository import (  # pyright: ignore[reportMissingImports]
    ProductStateRepository,
)


def _build_v1_db(uri: str) -> sqlite3.Connection:
    """Create a v1-schema database (no product_url col, no crawl_queue table)."""
    conn = sqlite3.connect(uri, uri=True)
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE IF NOT EXISTS schema_version (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS products (
            external_key TEXT PRIMARY KEY,
            dify_document_id TEXT,
            content_hash TEXT NOT NULL,
            normalizer_version INTEGER NOT NULL,
            last_seen_in_sitemap_at TEXT,
            last_fetch_success_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            consecutive_sitemap_misses INTEGER NOT NULL DEFAULT 0,
            inactive INTEGER NOT NULL DEFAULT 0,
            inactive_reason TEXT,
            inactive_since TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_products_dify_document_id ON products(dify_document_id);
        CREATE INDEX IF NOT EXISTS idx_products_inactive ON products(inactive);
        CREATE INDEX IF NOT EXISTS idx_products_last_seen_in_sitemap_at ON products(last_seen_in_sitemap_at);
        INSERT INTO schema_version(id, version) VALUES (1, 1);
        COMMIT;
        """
    )
    return conn


def test_migration_v1_to_v2() -> None:
    """Start from v1 DB (no product_url col, no crawl_queue), run migration, assert both exist."""
    uri = "file:test_mig_v1v2?mode=memory&cache=shared"

    # Set up a v1 database
    hold = _build_v1_db(uri)
    try:
        # Opening ProductStateRepository should trigger migration to v2
        with ProductStateRepository(uri) as repo:
            # Verify schema_version is now 2
            v = repo.connection.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()[0]
            assert int(v) == 2

            # Verify product_url column exists in products
            cols = [
                row[1] for row in repo.connection.execute("PRAGMA table_info(products)")
            ]
            assert "product_url" in cols

            # Verify crawl_queue table exists with correct columns
            cq_cols = [
                row[1]
                for row in repo.connection.execute("PRAGMA table_info(crawl_queue)")
            ]
            assert "id" in cq_cols
            assert "store_id" in cq_cols
            assert "product_url" in cq_cols
            assert "created_at" in cq_cols
    finally:
        hold.close()


def test_migration_idempotent_on_v2() -> None:
    """Apply migration on a v2 DB — assert no crash."""
    uri = "file:test_mig_idempotent?mode=memory&cache=shared"

    # First open creates a fresh v2 DB
    hold = ProductStateRepository(uri)
    hold.open()
    try:
        # Verify it's v2
        v = hold.connection.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()[0]
        assert int(v) == 2

        # Open again — should not crash (no migration needed)
        with ProductStateRepository(uri) as repo:
            v2 = repo.connection.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()[0]
            assert int(v2) == 2
    finally:
        hold.close()


def test_fresh_db_has_schema_v2_and_new_structures() -> None:
    """Open a brand-new DB, assert schema_version=2 and both new structures exist."""
    with ProductStateRepository(":memory:") as repo:
        # Verify schema version
        v = repo.connection.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()[0]
        assert int(v) == 2

        # Verify product_url column in products
        cols = [
            row[1] for row in repo.connection.execute("PRAGMA table_info(products)")
        ]
        assert "product_url" in cols

        # Verify crawl_queue table exists
        cq_cols = [
            row[1] for row in repo.connection.execute("PRAGMA table_info(crawl_queue)")
        ]
        assert "id" in cq_cols
        assert "store_id" in cq_cols
        assert "product_url" in cq_cols
        assert "created_at" in cq_cols

        # Verify UNIQUE constraint works on crawl_queue
        repo.connection.execute(
            "INSERT INTO crawl_queue(store_id, product_url) VALUES ('s1', 'http://a.com/p1')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            repo.connection.execute(
                "INSERT INTO crawl_queue(store_id, product_url) VALUES ('s1', 'http://a.com/p1')"
            )
