from __future__ import annotations

# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownParameterType=false
# pyright: reportUntypedFunctionDecorator=false
# pyright: reportMissingImports=false

from datetime import datetime, timedelta, timezone

import pytest  # pyright: ignore[reportMissingImports]

from estimator_king.database.repository import (  # pyright: ignore[reportMissingImports]
    ProductState,
    ProductStateRepository,
)


def _dt(days_ago: int) -> datetime:
    return datetime.now(tz=timezone.utc) - timedelta(days=days_ago)


def _state(
    external_key: str,
    *,
    content_hash: str = "a" * 64,
    normalizer_version: int = 1,
    dify_document_id: str | None = None,
    last_seen_in_sitemap_at: datetime | None = None,
    last_fetch_success_at: datetime | None = None,
    consecutive_failures: int = 0,
    consecutive_sitemap_misses: int = 0,
    inactive: bool = False,
    inactive_reason: str | None = None,
    inactive_since: datetime | None = None,
) -> ProductState:
    return ProductState(
        external_key=external_key,
        dify_document_id=dify_document_id,
        content_hash=content_hash,
        normalizer_version=normalizer_version,
        last_seen_in_sitemap_at=last_seen_in_sitemap_at,
        last_fetch_success_at=last_fetch_success_at,
        consecutive_failures=consecutive_failures,
        consecutive_sitemap_misses=consecutive_sitemap_misses,
        inactive=inactive,
        inactive_reason=inactive_reason,
        inactive_since=inactive_since,
    )


@pytest.fixture()
def repo():
    with ProductStateRepository(":memory:") as r:
        yield r


def test_state_db_roundtrip_insert_query(repo: ProductStateRepository) -> None:
    s = _state(
        "hololive:123",
        content_hash="b" * 64,
        normalizer_version=1,
        dify_document_id="doc-1",
        last_seen_in_sitemap_at=_dt(1),
        last_fetch_success_at=_dt(2),
        consecutive_failures=2,
        consecutive_sitemap_misses=3,
        inactive=False,
    )
    saved = repo.upsert(s)

    loaded = repo.get_by_external_key("hololive:123")
    assert loaded is not None
    assert loaded.external_key == saved.external_key
    assert loaded.dify_document_id == "doc-1"
    assert loaded.content_hash == "b" * 64
    assert loaded.normalizer_version == 1
    assert loaded.consecutive_failures == 2
    assert loaded.consecutive_sitemap_misses == 3
    assert loaded.inactive is False
    assert loaded.created_at is not None
    assert loaded.updated_at is not None
    assert loaded.last_seen_in_sitemap_at is not None
    assert loaded.last_fetch_success_at is not None
    assert loaded.updated_at == saved.updated_at


def test_state_db_get_by_external_key_missing_returns_none(
    repo: ProductStateRepository,
) -> None:
    assert repo.get_by_external_key("hololive:missing") is None


def test_state_db_upsert_updates_existing_row(repo: ProductStateRepository) -> None:
    first = repo.upsert(_state("hololive:1", dify_document_id="doc-a"))
    second = repo.upsert(
        _state(
            "hololive:1",
            content_hash="c" * 64,
            dify_document_id=None,
            consecutive_failures=1,
        )
    )

    assert second.external_key == first.external_key
    assert second.created_at == first.created_at
    assert second.updated_at is not None
    assert first.updated_at is not None
    assert second.updated_at >= first.updated_at
    assert second.content_hash == "c" * 64
    assert second.consecutive_failures == 1
    assert second.dify_document_id == "doc-a"

    count = repo.connection.execute(
        "SELECT COUNT(*) FROM products WHERE external_key = ?",
        ("hololive:1",),
    ).fetchone()[0]
    assert count == 1


def test_state_db_upsert_updates_dify_document_id_when_provided(
    repo: ProductStateRepository,
) -> None:
    first = repo.upsert(_state("hololive:2", dify_document_id=None))
    second = repo.upsert(_state("hololive:2", dify_document_id="doc-2"))
    assert first.dify_document_id is None
    assert second.dify_document_id == "doc-2"


def test_state_db_busy_timeout_configured(repo: ProductStateRepository) -> None:
    busy_timeout = repo.connection.execute("PRAGMA busy_timeout").fetchone()[0]
    assert int(busy_timeout) >= 30000


def test_state_db_journal_mode_is_valid(repo: ProductStateRepository) -> None:
    mode = repo.connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode in {"wal", "memory"}


def test_state_db_resume_after_interruption_shared_memory() -> None:
    uri = "file:state_db?mode=memory&cache=shared"

    hold = ProductStateRepository(uri)
    hold.open()
    try:
        r1 = ProductStateRepository(uri)
        r1.open()
        r1.connection.execute("BEGIN")
        r1.upsert(_state("hololive:tx", dify_document_id="doc-tx"))
        r1.close()

        assert hold.get_by_external_key("hololive:tx") is None

        with ProductStateRepository(uri) as r2:
            assert r2.get_by_external_key("hololive:tx") is None
            r2.upsert(_state("hololive:tx", dify_document_id="doc-ok"))
            assert r2.get_by_external_key("hololive:tx") is not None
    finally:
        hold.close()


def test_state_db_mark_inactive_logic(repo: ProductStateRepository) -> None:
    repo.upsert(_state("hololive:9"))
    repo.mark_inactive("hololive:9", reason="fetch_failures")
    s = repo.get_by_external_key("hololive:9")
    assert s is not None
    assert s.inactive is True
    assert s.inactive_reason == "fetch_failures"
    assert s.inactive_since is not None
    assert repo.get_all_active() == []


def test_state_db_mark_inactive_unknown_key_no_error(
    repo: ProductStateRepository,
) -> None:
    repo.mark_inactive("hololive:does-not-exist", reason="sitemap_missing")


def test_state_db_get_stale_products(repo: ProductStateRepository) -> None:
    repo.upsert(_state("hololive:recent", last_seen_in_sitemap_at=_dt(1)))
    repo.upsert(_state("hololive:stale", last_seen_in_sitemap_at=_dt(10)))
    repo.upsert(_state("hololive:null", last_seen_in_sitemap_at=None))
    repo.upsert(
        _state("hololive:inactive", last_seen_in_sitemap_at=_dt(10), inactive=True)
    )

    stale = repo.get_stale_products(days=7)
    keys = [s.external_key for s in stale]
    assert keys == ["hololive:null", "hololive:stale"]


def test_state_db_get_stale_products_invalid_days_raises(
    repo: ProductStateRepository,
) -> None:
    with pytest.raises(ValueError):
        repo.get_stale_products(days=0)


def test_state_db_naive_datetimes_roundtrip_as_utc(
    repo: ProductStateRepository,
) -> None:
    naive_seen = datetime(2026, 2, 20, 12, 0, 0)
    naive_fetch = datetime(2026, 2, 20, 12, 30, 0)
    repo.upsert(
        _state(
            "hololive:naive",
            last_seen_in_sitemap_at=naive_seen,
            last_fetch_success_at=naive_fetch,
        )
    )
    loaded = repo.get_by_external_key("hololive:naive")
    assert loaded is not None
    assert loaded.last_seen_in_sitemap_at is not None
    assert loaded.last_fetch_success_at is not None
    assert loaded.last_seen_in_sitemap_at.tzinfo is not None
    assert loaded.last_fetch_success_at.tzinfo is not None
    assert loaded.last_seen_in_sitemap_at.utcoffset() == timedelta(0)
    assert loaded.last_fetch_success_at.utcoffset() == timedelta(0)


def test_state_db_schema_version_initialized(repo: ProductStateRepository) -> None:
    v = repo.connection.execute(
        "SELECT version FROM schema_version WHERE id = 1"
    ).fetchone()[0]
    assert int(v) == 1


def test_state_db_schema_newer_than_supported_is_rejected() -> None:
    uri = "file:state_db_ver?mode=memory&cache=shared"
    hold = ProductStateRepository(uri)
    hold.open()
    try:
        hold.connection.execute("UPDATE schema_version SET version = 999 WHERE id = 1")
        with pytest.raises(RuntimeError):
            with ProductStateRepository(uri) as _:
                pass
    finally:
        hold.close()


def test_state_db_get_all_active_orders_by_external_key(
    repo: ProductStateRepository,
) -> None:
    repo.upsert(_state("b:2"))
    repo.upsert(_state("a:1"))
    repo.upsert(_state("c:3"))
    keys = [s.external_key for s in repo.get_all_active()]
    assert keys == ["a:1", "b:2", "c:3"]
