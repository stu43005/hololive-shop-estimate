# Fix product_url Fabrication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop fabricating `product_url` from the numeric Shopify product id, retire stuck 404/410 queue entries, and provide a one-time migration that purges the stuck queue and resets bug-inflated counters.

**Architecture:** `sync_products` becomes a pass-through writer — it stores the URL that was actually fetched (carried alongside each snapshot as a `(url, snapshot)` tuple) instead of rebuilding one from `product_id`. The async pipeline drops definitively-gone (HTTP 404/410) entries from the crawl queue instead of retrying them forever. A standalone migration script clears the queue and zeroes the counters so the first post-fix crawl self-heals the stored URLs without false-marking products inactive.

**Tech Stack:** Python 3.14, asyncio, aiohttp, SQLite (`sqlite3`), tenacity, pytest, basedpyright, ruff.

---

## File Structure

- Modify `estimator_king/sync/engine.py` — `sync_products` signature + drop fabrication.
- Modify `estimator_king/crawler/async_pipeline.py` — pass `(url, snapshot)`, drop `store_base_url` param, add 404/410 queue-drop.
- Modify `estimator_king/crawler/cycle.py` — drop `store.base_url` argument to `async_process_queue`.
- Create `scripts/__init__.py` — make `scripts` importable under `pythonpath = .`.
- Create `scripts/migrate_2026_05_30_fix_product_urls.py` — one-time migration.
- Modify tests: `tests/test_sync_engine.py`, `tests/test_sync_engine_logging.py`, `tests/test_async_pipeline.py`, `tests/test_async_pipeline_logging.py`, `tests/test_integration_async_pipeline.py`.
- Create `tests/test_migrate_fix_product_urls.py`.

---

## Task 1: Pass-through URL — `sync_products` stores the fetched URL

This is one atomic signature refactor across `engine.py`, `async_pipeline.py`, and `cycle.py` (splitting would break compilation). It includes all affected test updates and the integration regression that proves the fix.

**Files:**
- Modify: `estimator_king/sync/engine.py` (signature + loop preamble lines 77-90, loop body)
- Modify: `estimator_king/crawler/async_pipeline.py` (signature lines 43-52, call lines 72-75)
- Modify: `estimator_king/crawler/cycle.py` (call lines 56-59)
- Test: `tests/test_sync_engine.py`, `tests/test_sync_engine_logging.py`, `tests/test_async_pipeline.py`, `tests/test_async_pipeline_logging.py`, `tests/test_integration_async_pipeline.py`

- [ ] **Step 1: Update `tests/test_sync_engine.py` to the new tuple signature + add the pass-through regression test**

Replace each `sync_products([...], "hololive", "https://x", repo, ...)` call so the first argument is a list of `(url, snapshot)` tuples and the `"https://x"` base-url argument is removed. Apply these exact edits:

In `test_create_embeds_upserts_and_persists_state` (line 55):
```python
    result = sync_products([("https://x/products/p1", _snapshot())], "hololive", repo, emb, vs)
```

In `test_unchanged_content_skips_reindex_but_stamps_fetch` (lines 68 and 72):
```python
    sync_products([("https://x/products/p1", _snapshot())], "hololive", repo, emb, vs)
```
```python
    result = sync_products([("https://x/products/p1", _snapshot())], "hololive", repo, emb2, vs2)
```

In `test_changed_content_updates_and_reindexes` (lines 82 and 89):
```python
    sync_products([("https://x/products/p1", _snapshot())], "hololive", repo, emb, vs)
```
```python
    result = sync_products([("https://x/products/p1", changed)], "hololive", repo, emb2, vs2)
```

In `test_carries_sitemap_state_forward` (lines 97 and 104):
```python
    sync_products([("https://x/products/p1", _snapshot())], "hololive", repo, emb, vs)
```
```python
    sync_products([("https://x/products/p1", changed)], "hololive", repo, FakeEmbedder(), FakeVectorStore())
```

In `test_embedding_error_counts_failed_and_does_not_advance_index` (line 115):
```python
    result = sync_products([("https://x/products/p1", _snapshot())], "hololive", repo, Boom(), FakeVectorStore())
```

Then add this new regression test at the end of the file:
```python
def test_stored_product_url_is_passed_url_not_fabricated_from_id(repo):
    emb, vs = FakeEmbedder(), FakeVectorStore()
    handle_url = "https://shop.hololivepro.com/products/voice-pack-001"
    snap = _snapshot(pid=8087824892124)

    sync_products([(handle_url, snap)], "hololive", repo, emb, vs)

    state = repo.get_by_external_key("hololive:8087824892124")
    assert state is not None
    assert state.product_url == handle_url
    assert state.product_url != "https://shop.hololivepro.com/products/8087824892124"
```

- [ ] **Step 2: Run the engine tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_sync_engine.py -v -o addopts=""`
Expected: FAIL — `TypeError` (old `sync_products` still expects `base_url` positional / iterates bare snapshots), e.g. `'tuple' object has no attribute 'product_id'` or a too-many-arguments error.

- [ ] **Step 3: Rewrite `sync_products` in `estimator_king/sync/engine.py`**

Replace the function definition header and loop preamble (lines 77-90). The new version drops the `base_url` parameter, iterates `(product_url, snapshot)` tuples, and removes the fabrication line. Replace:
```python
def sync_products(
    snapshots: Iterable[ProductSnapshot],
    store_id: str,
    base_url: str,
    repository: ProductStateRepository,
    embedder: _Embedder,
    vector_store: _VectorStore,
) -> SyncResult:
    result = SyncResult()
    for snapshot in snapshots:
        now = datetime.now(tz=timezone.utc)
        external_key = f"{store_id}:{snapshot.product_id}"
        product_url = f"{base_url}/products/{snapshot.product_id}"
        content_hash = compute_content_hash(snapshot)
```
with:
```python
def sync_products(
    items: Iterable[tuple[str, ProductSnapshot]],
    store_id: str,
    repository: ProductStateRepository,
    embedder: _Embedder,
    vector_store: _VectorStore,
) -> SyncResult:
    result = SyncResult()
    for product_url, snapshot in items:
        now = datetime.now(tz=timezone.utc)
        external_key = f"{store_id}:{snapshot.product_id}"
        content_hash = compute_content_hash(snapshot)
```
Everything below (the `state = repository.get_by_external_key(...)` line onward, including `_format_product_document(snapshot, store_id, product_url)` and `product_url=product_url` in the `ProductState(...)` upsert) stays unchanged — it already reads the `product_url` local variable.

- [ ] **Step 4: Run the engine tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_sync_engine.py -v -o addopts=""`
Expected: PASS (all tests including the new regression).

- [ ] **Step 5: Update `tests/test_sync_engine_logging.py` to the new signature**

Replace the call at lines 28-31:
```python
            result = sync_products(
                [("https://x/products/p1", _snap())], "hololive", repo,
                BoomEmbedder(), FakeVectorStore(),
            )
```

Run: `.venv/bin/python -m pytest tests/test_sync_engine_logging.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 6: Update the call site in `estimator_king/crawler/async_pipeline.py`**

Replace lines 72-75 (the `sync_products` invocation inside `_handle`):
```python
                sync_result = await asyncio.to_thread(
                    sync_products, [(product_url, snapshot)], store_id,
                    state_repo, embedder, vector_store,
                )
```
Then remove the now-unused `store_base_url` parameter from `async_process_queue`. Replace the signature (lines 43-52):
```python
async def async_process_queue(
    store_id: str,
    policy: CrawlerPolicy,
    state_repo: ProductStateRepository,
    embedder: EmbeddingProvider,
    vector_store: VectorStore,
    *,
    proxy: ProxyConfig | None = None,
) -> PipelineResult:
```
(No other line in `async_process_queue` references `store_base_url`.)

- [ ] **Step 7: Update the call site in `estimator_king/crawler/cycle.py`**

Replace lines 56-59:
```python
                try:
                    result = await async_process_queue(
                        store.id, config.crawler, repo, embedder, vector_store,
                        proxy=config.proxy)
```

- [ ] **Step 8: Update `tests/test_async_pipeline.py` and `tests/test_async_pipeline_logging.py` calls**

In `tests/test_async_pipeline.py`, remove the `"https://x"` second positional argument from all three `async_process_queue(...)` calls:

Lines 46-47:
```python
        result = asyncio.run(async_process_queue(
            "hololive", policy, repo, FakeEmbedder(), vs))
```
Lines 60-61:
```python
        asyncio.run(async_process_queue("hololive", CrawlerPolicy(), repo,
                                        FakeEmbedder(), FakeVectorStore()))
```
Lines 68-69:
```python
        result = asyncio.run(async_process_queue("hololive", CrawlerPolicy(), repo,
                                                 FakeEmbedder(), FakeVectorStore()))
```
Lines 93-95:
```python
        asyncio.run(async_process_queue(
            "hololive", CrawlerPolicy(), repo,
            FakeEmbedder(), FakeVectorStore(), proxy=proxy_cfg))
```

In `tests/test_async_pipeline_logging.py`, lines 54-56:
```python
            result = asyncio.run(async_process_queue(
                "hololive", CrawlerPolicy(), repo,
                FakeEmbedder(), FakeVectorStore()))
```

- [ ] **Step 9: Rewrite the integration regression `test_run_cycle_indexes_pre_seeded_urls`**

In `tests/test_integration_async_pipeline.py`, replace the whole `test_run_cycle_indexes_pre_seeded_urls` function (lines 88-131) with a version that seeds handle-style URLs mapped to distinct numeric ids and asserts the stored URL is the fetched handle URL:
```python
def test_run_cycle_indexes_pre_seeded_urls(db_path: str) -> None:
    """Pre-seeded handle URLs land in the DB; the stored product_url is the
    fetched handle URL, NOT a numeric one fabricated from product_id."""
    embedder = FakeEmbedder()
    vs = FakeVectorStore()

    handle_url_a = f"{BASE_URL}/products/voice-pack-001"
    handle_url_b = f"{BASE_URL}/products/voice-pack-002"
    pid_a = 8087824892124
    pid_b = 8087824892125

    # Pre-seed two handle URLs directly into the queue so we don't need the sitemap.
    with ProductStateRepository(db_path) as repo:
        _ = repo.enqueue_url(STORE_ID, handle_url_a)
        _ = repo.enqueue_url(STORE_ID, handle_url_b)

    snapshots: dict[str, ProductSnapshot] = {
        handle_url_a: _snap(pid_a),
        handle_url_b: _snap(pid_b),
    }

    def fake_fetch(url: str, client: Any) -> ProductSnapshot:
        _ = client
        return snapshots[url]

    with (
        patch("estimator_king.crawler.cycle.populate_queue_from_sitemap", return_value=0),
        patch("estimator_king.crawler.async_pipeline.fetch_product", side_effect=fake_fetch),
    ):
        counters = asyncio.run(
            run_crawl_cycle(_config(), db_path, embedder, vs)  # pyright: ignore[reportArgumentType]
        )

    # Both products should have been processed and upserted.
    assert counters["fetched_ok"] == 2
    assert counters["created"] == 2
    assert counters["errors"] == 0

    expected_keys = {f"{STORE_ID}:{pid_a}", f"{STORE_ID}:{pid_b}"}
    assert set(vs.upserts) == expected_keys

    # Stored URL must be the fetched handle URL, not a fabricated numeric one.
    with ProductStateRepository(db_path) as repo:
        state_a = repo.get_by_external_key(f"{STORE_ID}:{pid_a}")
        assert state_a is not None
        assert state_a.last_indexed_at is not None
        assert state_a.product_url == handle_url_a
        assert state_a.product_url != f"{BASE_URL}/products/{pid_a}"
```

- [ ] **Step 10: Run all Task 1 tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_sync_engine.py tests/test_sync_engine_logging.py tests/test_async_pipeline.py tests/test_async_pipeline_logging.py tests/test_integration_async_pipeline.py -v -o addopts=""`
Expected: PASS (all).

- [ ] **Step 11: Type check and lint**

Run: `.venv/bin/basedpyright estimator_king`
Expected: 0 errors in production code.
Run: `uvx ruff check estimator_king tests`
Expected: no errors.

- [ ] **Step 12: Commit**

```bash
git add estimator_king/sync/engine.py estimator_king/crawler/async_pipeline.py estimator_king/crawler/cycle.py tests/test_sync_engine.py tests/test_sync_engine_logging.py tests/test_async_pipeline.py tests/test_async_pipeline_logging.py tests/test_integration_async_pipeline.py
git commit -m "fix(sync): store fetched URL instead of fabricating from product_id"
```

---

## Task 2: Drop definitively-gone (HTTP 404/410) entries from the crawl queue

**Files:**
- Modify: `estimator_king/crawler/async_pipeline.py` (import line 8, except block lines 87-94)
- Test: `tests/test_async_pipeline.py`

- [ ] **Step 1: Write the failing tests**

Add these imports at the top of `tests/test_async_pipeline.py` (the file already imports `ShopifyHTTPError`; add `ClientError`):
```python
from estimator_king.crawler.async_http_client import ClientError
```
Then append these tests:
```python
def test_client_error_404_deletes_queue_entry_for_new_product(repo):
    repo.enqueue_url("hololive", "https://x/products/gone")

    def boom(url, client):
        raise ClientError(url, status_code=404)

    with patch("estimator_king.crawler.async_pipeline.fetch_product", side_effect=boom):
        result = asyncio.run(async_process_queue("hololive", CrawlerPolicy(), repo,
                                                 FakeEmbedder(), FakeVectorStore()))

    assert result.failed == 1
    assert repo.peek_all("hololive") == []  # definitively gone: dropped, not retried


def test_client_error_410_deletes_queue_and_increments_when_row_exists(repo):
    # First, a successful run creates the product row.
    repo.enqueue_url("hololive", "https://x/products/1")
    with patch("estimator_king.crawler.async_pipeline.fetch_product", return_value=_snap(1)):
        asyncio.run(async_process_queue("hololive", CrawlerPolicy(), repo,
                                        FakeEmbedder(), FakeVectorStore()))
    repo.enqueue_url("hololive", "https://x/products/1")  # re-queue for the failing run

    def boom(url, client):
        raise ClientError(url, status_code=410)

    with patch("estimator_king.crawler.async_pipeline.fetch_product", side_effect=boom):
        result = asyncio.run(async_process_queue("hololive", CrawlerPolicy(), repo,
                                                 FakeEmbedder(), FakeVectorStore()))

    assert result.failed == 1
    assert repo.peek_all("hololive") == []  # dropped
    assert repo.get_by_external_key("hololive:1").consecutive_failures == 1


def test_client_error_400_keeps_queue_entry(repo):
    repo.enqueue_url("hololive", "https://x/products/1")

    def boom(url, client):
        raise ClientError(url, status_code=400)

    with patch("estimator_king.crawler.async_pipeline.fetch_product", side_effect=boom):
        result = asyncio.run(async_process_queue("hololive", CrawlerPolicy(), repo,
                                                 FakeEmbedder(), FakeVectorStore()))

    assert result.failed == 1
    assert repo.peek_all("hololive") != []  # only 404/410 are definitive; 400 is retried
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_async_pipeline.py -k "404 or 410" -v -o addopts=""`
Expected: FAIL — the 404/410 tests fail their `peek_all(...) == []` assertion (current code keeps every failed entry for retry).

- [ ] **Step 3: Add the `ClientError` import in `estimator_king/crawler/async_pipeline.py`**

Replace line 8:
```python
from estimator_king.crawler.async_http_client import AsyncHTTPClient, ClientError
```

- [ ] **Step 4: Update the except block in `_handle`**

Replace the except block (lines 87-94) of `_handle`:
```python
            except Exception as exc:
                logger.exception("Error processing %s (url=%s)", entry_id, product_url)
                existing = state_repo.get_by_product_url(store_id, product_url)
                if existing is not None:
                    state_repo.increment_consecutive_failures(existing.external_key)
                if isinstance(exc, ClientError) and exc.status_code in (404, 410):
                    # Definitively gone (HTTP 404/410): drop from queue so it is
                    # not re-fetched every cycle. Transient errors keep retrying.
                    state_repo.delete_queue_entry(entry_id)
                async with lock:
                    result.failed += 1
```

- [ ] **Step 5: Run the Task 2 tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_async_pipeline.py -v -o addopts=""`
Expected: PASS (the three new tests plus the existing `test_fetch_failure_increments_failures_and_keeps_queue`, which raises `ShopifyHTTPError(500)` — not a `ClientError` — so its entry is still kept).

- [ ] **Step 6: Type check and lint**

Run: `.venv/bin/basedpyright estimator_king`
Expected: 0 errors in production code.
Run: `uvx ruff check estimator_king tests`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add estimator_king/crawler/async_pipeline.py tests/test_async_pipeline.py
git commit -m "fix(crawler): drop 404/410 entries from queue instead of retrying forever"
```

---

## Task 3: One-time migration — purge stuck queue and reset bug-inflated counters

**Files:**
- Create: `scripts/__init__.py`
- Create: `scripts/migrate_2026_05_30_fix_product_urls.py`
- Test: `tests/test_migrate_fix_product_urls.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_migrate_fix_product_urls.py`:
```python
from pathlib import Path

import pytest

from estimator_king.database.repository import ProductStateRepository
from scripts.migrate_2026_05_30_fix_product_urls import migrate

OLD_TS = "2020-01-01T00:00:00Z"


def _insert_product(repo, key, failures, misses):
    repo.connection.execute(
        "INSERT INTO products (external_key, store_id, product_id, product_url, "
        "content_hash, normalizer_version, created_at, updated_at, "
        "consecutive_failures, consecutive_sitemap_misses) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (key, "s", key.split(":")[1], f"https://x/products/{key.split(':')[1]}",
         "h", 2, OLD_TS, OLD_TS, failures, misses),
    )


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "state.db")


def _seed(db_path: str) -> None:
    with ProductStateRepository(db_path) as repo:
        _insert_product(repo, "s:1", failures=2, misses=3)   # affected
        _insert_product(repo, "s:2", failures=0, misses=0)   # control
        repo.enqueue_url("s", "https://x/products/1")
        repo.enqueue_url("s", "https://x/products/2")


def test_migrate_purges_queue_and_resets_only_affected_rows(db_path: str) -> None:
    _seed(db_path)

    queue_deleted, rows_reset = migrate(db_path)

    assert queue_deleted == 2
    assert rows_reset == 1  # only the affected row

    with ProductStateRepository(db_path) as repo:
        assert repo.peek_all("s") == []  # queue purged
        affected = repo.get_by_external_key("s:1")
        assert affected is not None
        assert affected.consecutive_failures == 0
        assert affected.consecutive_sitemap_misses == 0

        control_row = repo.connection.execute(
            "SELECT consecutive_failures, consecutive_sitemap_misses, updated_at "
            "FROM products WHERE external_key = ?", ("s:2",),
        ).fetchone()
        assert control_row["consecutive_failures"] == 0
        assert control_row["consecutive_sitemap_misses"] == 0
        assert control_row["updated_at"] == OLD_TS  # untouched


def test_migrate_is_idempotent(db_path: str) -> None:
    _seed(db_path)
    migrate(db_path)

    queue_deleted, rows_reset = migrate(db_path)

    assert queue_deleted == 0
    assert rows_reset == 0
    with ProductStateRepository(db_path) as repo:
        control_row = repo.connection.execute(
            "SELECT updated_at FROM products WHERE external_key = ?", ("s:2",),
        ).fetchone()
        assert control_row["updated_at"] == OLD_TS
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_migrate_fix_product_urls.py -v -o addopts=""`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.migrate_2026_05_30_fix_product_urls'`.

- [ ] **Step 3: Create the `scripts` package marker**

Create `scripts/__init__.py` with a single line:
```python
"""Operational / one-time maintenance scripts."""
```

- [ ] **Step 4: Create the migration script**

Create `scripts/migrate_2026_05_30_fix_product_urls.py`:
```python
"""One-time migration (2026-05-30): purge the stuck crawl queue and reset the
failure / sitemap-miss counters that the product_url fabrication bug inflated.

Run with the bot stopped (single DB writer). This script does NOT touch
product_url — the handle is not stored and cannot be reconstructed; the stored
URLs self-heal on the next normal crawl.

Usage:
    .venv/bin/python scripts/migrate_2026_05_30_fix_product_urls.py [db_path]

db_path falls back to $DATABASE_PATH, then ./estimator_king.db.
"""

from __future__ import annotations

import os
import sys

from estimator_king.database.repository import ProductStateRepository


def migrate(db_path: str) -> tuple[int, int]:
    """Purge crawl_queue and zero out inflated counters.

    Returns (queue_rows_deleted, product_rows_reset). Idempotent.
    """
    with ProductStateRepository(db_path) as repo:
        conn = repo.connection
        queue_deleted = conn.execute("DELETE FROM crawl_queue").rowcount
        rows_reset = conn.execute(
            "UPDATE products "
            "SET consecutive_failures = 0, "
            "    consecutive_sitemap_misses = 0, "
            "    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE consecutive_failures > 0 OR consecutive_sitemap_misses > 0"
        ).rowcount
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
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_migrate_fix_product_urls.py -v -o addopts=""`
Expected: PASS (both tests).

- [ ] **Step 6: Type check and lint**

Run: `.venv/bin/basedpyright estimator_king scripts`
Expected: 0 errors.
Run: `uvx ruff check estimator_king scripts tests`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add scripts/__init__.py scripts/migrate_2026_05_30_fix_product_urls.py tests/test_migrate_fix_product_urls.py
git commit -m "feat(scripts): one-time migration to purge stuck queue and reset counters"
```

---

## Final verification (after all tasks)

- [ ] **Run the full affected test set with the project toolchain**

Run:
```bash
.venv/bin/python -m pytest tests/test_sync_engine.py tests/test_sync_engine_logging.py tests/test_async_pipeline.py tests/test_async_pipeline_logging.py tests/test_integration_async_pipeline.py tests/test_migrate_fix_product_urls.py tests/test_crawl_cycle.py -v -o addopts=""
```
Expected: PASS (all). `tests/test_crawl_cycle.py` is included to confirm the `async_process_queue` signature change did not break its variadic-mock patches.

- [ ] **Type check + lint the whole touched surface**

Run: `.venv/bin/basedpyright estimator_king scripts`
Expected: 0 errors.
Run: `uvx ruff check estimator_king scripts tests`
Expected: no errors.

- [ ] **Full suite with coverage**

Run: `.venv/bin/python -m pytest`
Expected: PASS.

---

## Operational rollout (one-time, manual — not part of the code commits)

1. Deploy the three commits above.
2. Stop the bot (single DB writer — concurrent writers cause `database is locked`).
3. Load env and run the migration once:
   ```bash
   set -a; source .env; set +a
   .venv/bin/python scripts/migrate_2026_05_30_fix_product_urls.py
   ```
   Expect it to report ~2318 queue rows deleted and the inflated counter rows reset.
4. Start the bot (or run one `crawl`). The first crawl re-enqueues handle URLs from the sitemap, fetches them, and `upsert` rewrites each `product_url` in place; because `content_hash` is unchanged, no re-embedding occurs.
5. On the second crawl, sitemap handle URLs now match the DB, `record_sitemap_seen` resets misses to 0, and the 404 numeric-URL errors are gone.
