-- Estimator King state database schema (SQLite).
--
-- products: persistent per-product sync state
-- - external_key: "{store_id}:{shopify_product_id}" (stable)
-- - dify_document_id: Dify knowledge base document id (nullable)
-- - content_hash: canonical snapshot hash for change detection
-- - normalizer_version: snapshot normalizer version used for hashing
-- - last_seen_in_sitemap_at: last time present in sitemap
-- - last_fetch_success_at: last successful fetch timestamp
-- - consecutive_failures / consecutive_sitemap_misses: deactivation counters
-- - inactive + inactive_reason + inactive_since: soft deactivation (never delete)

BEGIN;

-- Schema versioning (single row, id=1).
CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);

-- Product state records.
CREATE TABLE IF NOT EXISTS products (
    external_key TEXT PRIMARY KEY,          -- Stable external id: "{store_id}:{product_id}"
    dify_document_id TEXT,                  -- Dify document id (string/UUID), nullable until created

    -- Change detection
    content_hash TEXT NOT NULL,             -- SHA-256 hex (64 chars) of canonical snapshot
    normalizer_version INTEGER NOT NULL,    -- estimator_king.crawler.snapshot.NORMALIZER_VERSION

    -- Crawl timestamps (UTC ISO8601 strings)
    last_seen_in_sitemap_at TEXT,           -- When found in sitemap (UTC ISO8601)
    last_fetch_success_at TEXT,             -- When product fetch succeeded (UTC ISO8601)
    created_at TEXT NOT NULL,               -- Row creation time (UTC ISO8601)
    updated_at TEXT NOT NULL,               -- Last update time (UTC ISO8601)

    -- Failure tracking
    consecutive_failures INTEGER NOT NULL DEFAULT 0,          -- Fetch failures in a row
    consecutive_sitemap_misses INTEGER NOT NULL DEFAULT 0,    -- Sitemap misses in a row

    -- Soft deactivation
    inactive INTEGER NOT NULL DEFAULT 0,    -- 0/1 boolean
    inactive_reason TEXT,                  -- "fetch_failures" | "sitemap_missing" | etc.
    inactive_since TEXT                    -- When marked inactive (UTC ISO8601)
);

-- Indexes.
CREATE INDEX IF NOT EXISTS idx_products_dify_document_id ON products(dify_document_id);
CREATE INDEX IF NOT EXISTS idx_products_inactive ON products(inactive);
CREATE INDEX IF NOT EXISTS idx_products_last_seen_in_sitemap_at ON products(last_seen_in_sitemap_at);

COMMIT;
