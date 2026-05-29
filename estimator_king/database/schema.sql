-- Estimator King state database (SQLite). Greenfield — created fresh, no migrations.
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS products (
    external_key   TEXT PRIMARY KEY,            -- "{store_id}:{product_id}" — also the ChromaDB id
    store_id       TEXT NOT NULL,
    product_id     TEXT NOT NULL,
    product_url    TEXT NOT NULL,

    content_hash   TEXT NOT NULL,
    normalizer_version INTEGER NOT NULL,

    last_seen_in_sitemap_at TEXT,
    last_fetch_success_at   TEXT,
    last_indexed_at         TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,

    consecutive_failures       INTEGER NOT NULL DEFAULT 0,
    consecutive_sitemap_misses INTEGER NOT NULL DEFAULT 0,

    inactive        INTEGER NOT NULL DEFAULT 0 CHECK (inactive IN (0,1)),
    inactive_reason TEXT,
    inactive_since  TEXT
);

CREATE INDEX IF NOT EXISTS idx_products_store_active_fetch
    ON products(store_id, inactive, last_fetch_success_at);

CREATE TABLE IF NOT EXISTS crawl_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id    TEXT NOT NULL,
    product_url TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE(store_id, product_url)
);
CREATE INDEX IF NOT EXISTS idx_crawl_queue_store_id ON crawl_queue(store_id, id);
