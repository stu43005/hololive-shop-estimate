from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import TracebackType
from typing import cast


@dataclass(frozen=True)
class ProductState:
    external_key: str
    content_hash: str
    normalizer_version: int
    dify_document_id: str | None = None
    last_seen_in_sitemap_at: datetime | None = None
    last_fetch_success_at: datetime | None = None
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
        return ProductState(
            external_key=self.external_key,
            dify_document_id=self.dify_document_id,
            content_hash=self.content_hash,
            normalizer_version=self.normalizer_version,
            last_seen_in_sitemap_at=self.last_seen_in_sitemap_at,
            last_fetch_success_at=self.last_fetch_success_at,
            created_at=created_at,
            updated_at=updated_at,
            consecutive_failures=self.consecutive_failures,
            consecutive_sitemap_misses=self.consecutive_sitemap_misses,
            inactive=self.inactive,
            inactive_reason=self.inactive_reason,
            inactive_since=self.inactive_since,
        )


class ProductStateRepository:
    _SCHEMA_VERSION: int = 1

    def __init__(self, db_path: str, *, timeout_seconds: float = 30.0):
        self._db_path: str = db_path
        self._timeout_seconds: float = timeout_seconds
        self._conn: sqlite3.Connection | None = None

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
        row = self.connection.execute(
            "SELECT * FROM products WHERE external_key = ?",
            (external_key,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_state(cast(sqlite3.Row, row))

    def upsert(self, state: ProductState) -> ProductState:
        now = _utc_now()

        existing = self.get_by_external_key(state.external_key)
        created_at = existing.created_at if existing and existing.created_at else now
        state = state.with_updated_timestamps(created_at=created_at, updated_at=now)
        _ = self.connection.execute(
            """
            INSERT INTO products (
                external_key,
                dify_document_id,
                content_hash,
                normalizer_version,
                last_seen_in_sitemap_at,
                last_fetch_success_at,
                created_at,
                updated_at,
                consecutive_failures,
                consecutive_sitemap_misses,
                inactive,
                inactive_reason,
                inactive_since
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(external_key) DO UPDATE SET
                dify_document_id=COALESCE(excluded.dify_document_id, products.dify_document_id),
                content_hash=excluded.content_hash,
                normalizer_version=excluded.normalizer_version,
                last_seen_in_sitemap_at=excluded.last_seen_in_sitemap_at,
                last_fetch_success_at=excluded.last_fetch_success_at,
                updated_at=excluded.updated_at,
                consecutive_failures=excluded.consecutive_failures,
                consecutive_sitemap_misses=excluded.consecutive_sitemap_misses,
                inactive=excluded.inactive,
                inactive_reason=excluded.inactive_reason,
                inactive_since=excluded.inactive_since
            """,
            (
                state.external_key,
                state.dify_document_id,
                state.content_hash,
                state.normalizer_version,
                _dt_to_iso(state.last_seen_in_sitemap_at),
                _dt_to_iso(state.last_fetch_success_at),
                _dt_to_iso(state.created_at),
                _dt_to_iso(state.updated_at),
                int(state.consecutive_failures),
                int(state.consecutive_sitemap_misses),
                1 if state.inactive else 0,
                state.inactive_reason,
                _dt_to_iso(state.inactive_since),
            ),
        )
        refreshed = self.get_by_external_key(state.external_key)
        if refreshed is None:
            raise RuntimeError("upsert failed to persist record")
        return refreshed

    def mark_inactive(self, external_key: str, reason: str) -> None:
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
        rows = self.connection.execute(
            "SELECT * FROM products WHERE inactive = 0 ORDER BY external_key"
        ).fetchall()
        return [_row_to_state(cast(sqlite3.Row, r)) for r in rows]

    def get_stale_products(self, days: int) -> list[ProductState]:
        if days <= 0:
            raise ValueError("days must be > 0")
        threshold_dt = _utc_now() - timedelta(days=days)
        threshold_iso = _dt_to_iso(threshold_dt)

        rows = self.connection.execute(
            """
            SELECT * FROM products
            WHERE inactive = 0
              AND (last_seen_in_sitemap_at IS NULL OR last_seen_in_sitemap_at < ?)
            ORDER BY external_key
            """,
            (threshold_iso,),
        ).fetchall()
        return [_row_to_state(cast(sqlite3.Row, r)) for r in rows]

    def _apply_pragmas(self, conn: sqlite3.Connection) -> None:
        _ = conn.execute("PRAGMA journal_mode=WAL")
        _ = conn.execute("PRAGMA synchronous=NORMAL")
        _ = conn.execute("PRAGMA busy_timeout=30000")

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        schema_sql = _read_schema_sql()
        conn.executescript(schema_sql)
        _ = conn.execute(
            "INSERT OR IGNORE INTO schema_version(id, version) VALUES (1, 0)"
        )
        version_row = conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()
        if version_row is None:
            raise RuntimeError("schema_version row missing")
        current = int(cast(int, version_row[0]))
        if current > self._SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {current} is newer than supported {self._SCHEMA_VERSION}"
            )
        if current < self._SCHEMA_VERSION:
            self._migrate(conn, current, self._SCHEMA_VERSION)

    def _migrate(
        self, conn: sqlite3.Connection, from_version: int, to_version: int
    ) -> None:
        if from_version == 0 and to_version >= 1:
            _ = conn.execute("UPDATE schema_version SET version = 1 WHERE id = 1")
            from_version = 1
        if from_version != to_version:
            raise RuntimeError(
                f"Unsupported migration path {from_version} -> {to_version}"
            )


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
    dify_document_id_str = cast(str | None, row["dify_document_id"])
    inactive_reason_str = cast(str | None, row["inactive_reason"])
    last_seen = cast(str | None, row["last_seen_in_sitemap_at"])
    last_fetch = cast(str | None, row["last_fetch_success_at"])
    created_at = cast(str | None, row["created_at"])
    updated_at = cast(str | None, row["updated_at"])
    inactive_since = cast(str | None, row["inactive_since"])
    return ProductState(
        external_key=str(row["external_key"]),
        dify_document_id=dify_document_id_str,
        content_hash=str(row["content_hash"]),
        normalizer_version=int(cast(int, row["normalizer_version"])),
        last_seen_in_sitemap_at=_iso_to_dt(last_seen),
        last_fetch_success_at=_iso_to_dt(last_fetch),
        created_at=_iso_to_dt(created_at),
        updated_at=_iso_to_dt(updated_at),
        consecutive_failures=int(cast(int, row["consecutive_failures"])),
        consecutive_sitemap_misses=int(cast(int, row["consecutive_sitemap_misses"])),
        inactive=bool(int(cast(int, row["inactive"]))),
        inactive_reason=inactive_reason_str,
        inactive_since=_iso_to_dt(inactive_since),
    )
