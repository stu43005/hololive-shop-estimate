from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import cast


@dataclass(frozen=True)
class ProductState:
    external_key: str
    store_id: str
    product_id: str
    product_url: str
    content_hash: str
    normalizer_version: int
    item_types_version: int | None = None
    last_seen_in_sitemap_at: datetime | None = None
    last_fetch_success_at: datetime | None = None
    last_indexed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    consecutive_failures: int = 0
    consecutive_sitemap_misses: int = 0
    inactive: bool = False
    inactive_reason: str | None = None
    inactive_since: datetime | None = None

    def with_updated_timestamps(
        self, *, created_at: datetime, updated_at: datetime
    ) -> "ProductState":
        return replace(self, created_at=created_at, updated_at=updated_at)


class ProductStateRepository:
    def __init__(self, db_path: str, *, timeout_seconds: float = 30.0):
        self._db_path: str = db_path
        self._timeout_seconds: float = timeout_seconds
        self._conn: sqlite3.Connection | None = None
        self._lock: threading.RLock = threading.RLock()

    def __enter__(self) -> "ProductStateRepository":
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def open(self) -> None:
        with self._lock:
            if self._conn is not None:
                return

            use_uri: bool = self._db_path.startswith("file:")
            if self._db_path != ":memory:" and not use_uri:
                Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

            conn = sqlite3.connect(
                self._db_path,
                uri=use_uri,
                timeout=self._timeout_seconds,
                isolation_level=None,
                detect_types=sqlite3.PARSE_DECLTYPES,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            try:
                self._apply_pragmas(conn)
                self._ensure_schema(conn)
            except Exception:
                conn.close()
                raise

            self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            self._conn.close()
            self._conn = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Repository is not open")
        return self._conn

    def get_by_external_key(self, external_key: str) -> ProductState | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM products WHERE external_key = ?",
                (external_key,),
            ).fetchone()
            if row is None:
                return None
            return _row_to_state(cast(sqlite3.Row, row))

    def upsert(self, state: ProductState) -> ProductState:
        with self._lock:
            now = _utc_now()
            existing = self.get_by_external_key(state.external_key)
            created_at = existing.created_at if existing and existing.created_at else now
            state = state.with_updated_timestamps(created_at=created_at, updated_at=now)
            _ = self.connection.execute(
                """
                INSERT INTO products (
                    external_key, store_id, product_id, product_url,
                    content_hash, normalizer_version, item_types_version,
                    last_seen_in_sitemap_at, last_fetch_success_at, last_indexed_at,
                    created_at, updated_at,
                    consecutive_failures, consecutive_sitemap_misses,
                    inactive, inactive_reason, inactive_since
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(external_key) DO UPDATE SET
                    store_id=excluded.store_id,
                    product_id=excluded.product_id,
                    product_url=COALESCE(excluded.product_url, products.product_url),
                    content_hash=excluded.content_hash,
                    normalizer_version=excluded.normalizer_version,
                    item_types_version=excluded.item_types_version,
                    last_seen_in_sitemap_at=COALESCE(excluded.last_seen_in_sitemap_at, products.last_seen_in_sitemap_at),
                    last_fetch_success_at=COALESCE(excluded.last_fetch_success_at, products.last_fetch_success_at),
                    last_indexed_at=COALESCE(excluded.last_indexed_at, products.last_indexed_at),
                    updated_at=excluded.updated_at,
                    consecutive_failures=excluded.consecutive_failures,
                    consecutive_sitemap_misses=excluded.consecutive_sitemap_misses,
                    inactive=excluded.inactive,
                    inactive_reason=excluded.inactive_reason,
                    inactive_since=excluded.inactive_since
                """,
                (
                    state.external_key, state.store_id, state.product_id, state.product_url,
                    state.content_hash, state.normalizer_version,
                    int(state.item_types_version) if state.item_types_version is not None else None,
                    _dt_to_iso(state.last_seen_in_sitemap_at),
                    _dt_to_iso(state.last_fetch_success_at),
                    _dt_to_iso(state.last_indexed_at),
                    _dt_to_iso(state.created_at), _dt_to_iso(state.updated_at),
                    int(state.consecutive_failures), int(state.consecutive_sitemap_misses),
                    1 if state.inactive else 0, state.inactive_reason,
                    _dt_to_iso(state.inactive_since),
                ),
            )
            refreshed = self.get_by_external_key(state.external_key)
            if refreshed is None:
                raise RuntimeError("upsert failed to persist record")
            return refreshed

    def mark_inactive(self, external_key: str, reason: str) -> None:
        with self._lock:
            now = _utc_now()
            _ = self.connection.execute(
                """
                UPDATE products
                SET inactive = 1,
                    inactive_reason = ?,
                    inactive_since = ?,
                    updated_at = ?
                WHERE external_key = ?
                """,
                (reason, _dt_to_iso(now), _dt_to_iso(now), external_key),
            )

    def get_all_active(self) -> list[ProductState]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM products WHERE inactive = 0 ORDER BY external_key"
            ).fetchall()
            return [_row_to_state(cast(sqlite3.Row, r)) for r in rows]

    def list_active(self, store_id: str) -> list[ProductState]:
        """Return all active products for a given store."""
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM products WHERE store_id = ? AND inactive = 0 ORDER BY external_key",
                (store_id,),
            ).fetchall()
            return [_row_to_state(cast(sqlite3.Row, r)) for r in rows]

    def get_oldest_active_products(self, store_id: str, limit: int) -> list[ProductState]:
        if limit <= 0:
            return []
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM products
                WHERE store_id = ? AND inactive = 0
                ORDER BY last_fetch_success_at ASC
                LIMIT ?
                """,
                (store_id, limit),
            ).fetchall()
            return [_row_to_state(cast(sqlite3.Row, r)) for r in rows]

    # ── crawl_queue helpers ──────────────────────────────────────────

    def enqueue_url(self, store_id: str, product_url: str) -> bool:
        """Insert a URL into the crawl queue. Returns True if new, False if duplicate."""
        with self._lock:
            cur = self.connection.execute(
                "INSERT OR IGNORE INTO crawl_queue (store_id, product_url) VALUES (?, ?)",
                (store_id, product_url),
            )
            return cur.rowcount == 1

    def peek_next(self, store_id: str | None = None) -> tuple[int, str, str] | None:
        """Return (id, store_id, product_url) for the oldest queue entry, or None."""
        with self._lock:
            if store_id is not None:
                row = self.connection.execute(
                    "SELECT id, store_id, product_url FROM crawl_queue"
                    " WHERE store_id = ? ORDER BY id ASC LIMIT 1",
                    (store_id,),
                ).fetchone()
            else:
                row = self.connection.execute(
                    "SELECT id, store_id, product_url FROM crawl_queue"
                    " ORDER BY id ASC LIMIT 1"
                ).fetchone()
            if row is None:
                return None
            return (int(row["id"]), str(row["store_id"]), str(row["product_url"]))

    def peek_all(self, store_id: str) -> list[dict[str, int | str]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT id, store_id, product_url FROM crawl_queue WHERE store_id = ? ORDER BY id ASC",
                (store_id,),
            ).fetchall()
            return [
                {
                    "id": int(row["id"]),
                    "store_id": str(row["store_id"]),
                    "product_url": str(row["product_url"]),
                }
                for row in rows
            ]

    def delete_queue_entry(self, entry_id: int) -> None:
        """Delete a single queue entry by id."""
        with self._lock:
            _ = self.connection.execute(
                "DELETE FROM crawl_queue WHERE id = ?",
                (entry_id,),
            )

    def queue_size(self, store_id: str | None = None) -> int:
        """Return the number of entries in the crawl queue."""
        with self._lock:
            if store_id is not None:
                row = self.connection.execute(
                    "SELECT COUNT(*) FROM crawl_queue WHERE store_id = ?",
                    (store_id,),
                ).fetchone()
            else:
                row = self.connection.execute("SELECT COUNT(*) FROM crawl_queue").fetchone()
            return int(row[0]) if row else 0

    def clear_queue(self, store_id: str | None = None) -> int:
        """Delete all entries from the crawl queue. Returns rows deleted."""
        with self._lock:
            if store_id is not None:
                cur = self.connection.execute(
                    "DELETE FROM crawl_queue WHERE store_id = ?",
                    (store_id,),
                )
            else:
                cur = self.connection.execute("DELETE FROM crawl_queue")
            return cur.rowcount

    def get_by_product_url(
        self, store_id: str, product_url: str
    ) -> ProductState | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM products WHERE store_id = ? AND product_url = ?",
                (store_id, product_url),
            ).fetchone()
            if row is None:
                return None
            return _row_to_state(cast(sqlite3.Row, row))

    def increment_consecutive_failures(self, external_key: str) -> None:
        with self._lock:
            now = _utc_now()
            _ = self.connection.execute(
                """
                UPDATE products
                SET consecutive_failures = consecutive_failures + 1,
                    updated_at = ?
                WHERE external_key = ?
                """,
                (_dt_to_iso(now), external_key),
            )

    def reset_consecutive_failures(self, external_key: str) -> None:
        with self._lock:
            now = _utc_now()
            _ = self.connection.execute(
                """
                UPDATE products
                SET consecutive_failures = 0,
                    last_fetch_success_at = ?,
                    updated_at = ?
                WHERE external_key = ?
                """,
                (_dt_to_iso(now), _dt_to_iso(now), external_key),
            )

    def record_sitemap_seen(self, external_key: str) -> None:
        with self._lock:
            now = _utc_now()
            _ = self.connection.execute(
                """
                UPDATE products
                SET last_seen_in_sitemap_at = ?,
                    consecutive_sitemap_misses = 0,
                    updated_at = ?
                WHERE external_key = ?
                """,
                (_dt_to_iso(now), _dt_to_iso(now), external_key),
            )

    def increment_sitemap_miss(self, external_key: str) -> None:
        with self._lock:
            now = _utc_now()
            _ = self.connection.execute(
                """
                UPDATE products
                SET consecutive_sitemap_misses = consecutive_sitemap_misses + 1,
                    updated_at = ?
                WHERE external_key = ?
                """,
                (_dt_to_iso(now), external_key),
            )

    def get_cached_type(self, text_hash: str) -> str | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT item_type FROM item_type_cache WHERE text_hash = ?",
                (text_hash,),
            ).fetchone()
            return None if row is None else str(row["item_type"])

    def put_cached_type(self, text_hash: str, item_type: str, version: int,
                        text_sample: str) -> None:
        with self._lock:
            _ = self.connection.execute(
                "INSERT INTO item_type_cache (text_hash, text_sample, item_type, item_types_version, created_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(text_hash) DO UPDATE SET"
                " text_sample=excluded.text_sample, item_type=excluded.item_type,"
                " item_types_version=excluded.item_types_version",
                (text_hash, text_sample, item_type, int(version), _dt_to_iso(_utc_now())),
            )

    def list_other_typed(self, limit: int) -> list[str]:
        """Distinct readable item texts classified as 'その他', for vocabulary review."""
        with self._lock:
            rows = self.connection.execute(
                "SELECT DISTINCT text_sample FROM item_type_cache WHERE item_type = 'その他'"
                " ORDER BY text_sample LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [str(r["text_sample"]) for r in rows]

    def _apply_pragmas(self, conn: sqlite3.Connection) -> None:
        _ = conn.execute("PRAGMA journal_mode=WAL")
        _ = conn.execute("PRAGMA synchronous=NORMAL")
        _ = conn.execute("PRAGMA busy_timeout=30000")

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_read_schema_sql())
        # Idempotent additive migration for pre-existing databases: schema.sql uses
        # CREATE TABLE IF NOT EXISTS and will not add columns to an existing table.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(products)").fetchall()}
        if "item_types_version" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN item_types_version INTEGER")


def _read_schema_sql() -> str:
    here = Path(__file__).resolve().parent
    schema_path = here / "schema.sql"
    return schema_path.read_text(encoding="utf-8")


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _dt_to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_to_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _row_to_state(row: sqlite3.Row) -> ProductState:
    return ProductState(
        external_key=str(row["external_key"]),
        store_id=str(row["store_id"]),
        product_id=str(row["product_id"]),
        product_url=str(row["product_url"]),
        content_hash=str(row["content_hash"]),
        normalizer_version=int(cast(int, row["normalizer_version"])),
        item_types_version=(
            int(cast(int, row["item_types_version"]))
            if row["item_types_version"] is not None else None
        ),
        last_seen_in_sitemap_at=_iso_to_dt(cast("str | None", row["last_seen_in_sitemap_at"])),
        last_fetch_success_at=_iso_to_dt(cast("str | None", row["last_fetch_success_at"])),
        last_indexed_at=_iso_to_dt(cast("str | None", row["last_indexed_at"])),
        created_at=_iso_to_dt(cast("str | None", row["created_at"])),
        updated_at=_iso_to_dt(cast("str | None", row["updated_at"])),
        consecutive_failures=int(cast(int, row["consecutive_failures"])),
        consecutive_sitemap_misses=int(cast(int, row["consecutive_sitemap_misses"])),
        inactive=bool(int(cast(int, row["inactive"]))),
        inactive_reason=cast("str | None", row["inactive_reason"]),
        inactive_since=_iso_to_dt(cast("str | None", row["inactive_since"])),
    )
