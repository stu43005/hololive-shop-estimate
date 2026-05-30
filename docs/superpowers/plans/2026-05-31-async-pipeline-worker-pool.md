# Async Pipeline Worker Pool + Async-Native Fetch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修復 Ctrl+C 關閉慢數分鐘 + 心跳餓死：把 `async_process_queue` 改成固定 `concurrency` 個 worker 的 pool，並把 `fetch_product` 改 async-native、移除 `_AsyncToSyncHTTPAdapter` 的 thread↔loop ping-pong。

**Architecture:** (A) `asyncio.Queue` 預載全部 entries + 固定 `concurrency_per_domain` 個 worker 拉取，移除 `Semaphore`/`Lock`，任何時刻只有 `concurrency` 個 task。(B) `fetch_product` 改 `async def`，直接 `await client.get`（回 `str`），同步解析移到 `asyncio.to_thread(_build_snapshot)`，移除 adapter；HTTP 錯誤改由 `AsyncHTTPClient` 的 `ClientError`(4xx)/`ServerError`(5xx) 表示。

**Tech Stack:** Python 3.14、aiohttp、asyncio、pytest + pytest-asyncio、basedpyright、ruff。

**驗證指令：**
- 型別：`.venv/bin/basedpyright estimator_king`（production 0 errors）
- Lint：`uvx ruff check <paths>`
- 測試：`.venv/bin/pytest <path> -o addopts="" -q`

**Spec：** `docs/superpowers/specs/2026-05-31-async-pipeline-worker-pool-design.md`

**Task 順序：** Task 1（A：worker pool，保留 adapter，fetch 仍同步）→ Task 2（B：fetch 改 async + 移除 adapter）。Task 2 依賴 Task 1 完成。

---

## File Structure

| 檔案 | 責任 | Task |
|---|---|---|
| `estimator_king/crawler/async_pipeline.py` | A：Queue + worker pool（移除 Semaphore/Lock）；B：移除 adapter、`_handle` 直接 await fetch | 1, 2 |
| `estimator_king/crawler/shopify.py` | B：`fetch_product` 改 async + `_build_snapshot`；移除 `_HTTPResponse`/`_HTTPGetter`/`_raise_for_status`/`ShopifyHTTPError` | 2 |
| `tests/test_async_pipeline.py` | A：worker-pool 全處理測試；B：boom→ServerError、並行上限測試 | 1, 2 |
| `tests/test_shopify.py` | B：所有 fetch_product 測試改 async | 2 |

---

## Task 1: Worker pool（A）— `async_process_queue` 改固定 worker pool

> 本 Task 只改併發機制（`asyncio.Queue` + 固定 worker，移除 `Semaphore`/`Lock`），**保留** `_AsyncToSyncHTTPAdapter` 與 `fetch_product`（仍同步、仍走 `to_thread`）。Task 2 再處理 async fetch。

**Files:**
- Modify: `estimator_king/crawler/async_pipeline.py`
- Test: `tests/test_async_pipeline.py`

- [ ] **Step 1: 寫 worker-pool 全處理測試**

在 `tests/test_async_pipeline.py` 末端新增（`_snap`、`FakeEmbedder`、`FakeVectorStore`、`repo` fixture 皆已存在）：

```python
def test_worker_pool_processes_all_entries(repo):
    for i in range(1, 6):
        repo.enqueue_url("hololive", f"https://x/products/{i}")
    vs = FakeVectorStore()
    policy = CrawlerPolicy(concurrency_per_domain=2)
    snaps = {f"https://x/products/{i}": _snap(i) for i in range(1, 6)}

    def fake_fetch(url, client):
        return snaps[url]

    with patch("estimator_king.crawler.async_pipeline.fetch_product", side_effect=fake_fetch):
        result = asyncio.run(async_process_queue(
            "hololive", policy, repo, FakeEmbedder(), vs))

    assert result.processed == 5
    assert repo.peek_all("hololive") == []  # queue fully drained by the worker pool
```

- [ ] **Step 2: 執行測試（應先通過——現有 gather 實作也能處理全部）**

Run: `.venv/bin/pytest tests/test_async_pipeline.py::test_worker_pool_processes_all_entries -o addopts="" -q`
Expected: PASS（此測試描述的不變式在改寫前後都成立；它鎖定 worker pool 不會漏處理 entries）

- [ ] **Step 3: 改寫 `async_process_queue` 為 worker pool**

在 `estimator_king/crawler/async_pipeline.py`，把 `async_process_queue` 函式主體（從 `loop = asyncio.get_running_loop()` 到 `return result` 之前）改寫。完整新版函式：

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
    entries = state_repo.peek_all(store_id)
    if not entries:
        return PipelineResult()

    logger.info("store=%s queue: %d entries to process", store_id, len(entries))

    loop = asyncio.get_running_loop()
    result = PipelineResult()

    async with AsyncHTTPClient(policy, proxy=proxy) as client:
        adapter = _AsyncToSyncHTTPAdapter(client, loop)
        fetch_with_adapter = cast(Callable[[str, Any], Any], fetch_product)

        async def _handle(entry: dict[str, int | str]) -> None:
            entry_id = int(entry["id"])
            product_url = str(entry["product_url"])
            try:
                snapshot = await asyncio.to_thread(fetch_with_adapter, product_url, adapter)
                sync_result = await asyncio.to_thread(
                    sync_products, [(product_url, snapshot)], store_id,
                    state_repo, embedder, vector_store,
                )
                state_repo.delete_queue_entry(entry_id)
                result.created += sync_result.created
                result.updated += sync_result.updated
                result.sync_skipped += sync_result.skipped
                result.processed += 1
                if result.processed % _PROGRESS_LOG_EVERY == 0:
                    logger.info(
                        "store=%s progress: %d/%d processed",
                        store_id, result.processed, len(entries),
                    )
            except Exception as exc:
                logger.exception("Error processing %s (url=%s)", entry_id, product_url)
                existing = state_repo.get_by_product_url(store_id, product_url)
                if existing is not None:
                    state_repo.increment_consecutive_failures(existing.external_key)
                if isinstance(exc, ClientError) and exc.status_code in (404, 410):
                    # Definitively gone (HTTP 404/410): drop from queue so it is
                    # not re-fetched every cycle. Transient errors keep retrying.
                    state_repo.delete_queue_entry(entry_id)
                result.failed += 1

        queue: asyncio.Queue[dict[str, int | str]] = asyncio.Queue()
        for entry in entries:
            queue.put_nowait(entry)

        async def _worker() -> None:
            while True:
                try:
                    entry = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await _handle(entry)

        worker_count = max(1, policy.concurrency_per_domain)
        workers = [asyncio.create_task(_worker()) for _ in range(worker_count)]
        await asyncio.gather(*workers)

    logger.info(
        "store=%s done: created=%d updated=%d skipped=%d failed=%d",
        store_id, result.created, result.updated, result.sync_skipped, result.failed,
    )
    return result
```

變更摘要（相對現況）：
- **移除** `lock = asyncio.Lock()` 與 `_handle` 內所有 `async with lock:`（result 累加區塊無 `await`，單執行緒 loop 上安全）。
- **移除** `sem = asyncio.Semaphore(...)` 與 `_bounded`。
- **新增** `asyncio.Queue` 預載 entries + `_worker`（`get_nowait()`／`QueueEmpty` 結束）+ `worker_count = max(1, policy.concurrency_per_domain)` 個 worker。
- `loop`／`_AsyncToSyncHTTPAdapter`／`fetch_with_adapter`／`_handle` 其餘邏輯**保留不變**（Task 2 才處理）。

- [ ] **Step 4: 執行 async_pipeline 全部測試**

Run: `.venv/bin/pytest tests/test_async_pipeline.py -o addopts="" -q`
Expected: PASS（含新測試與全部既有 404/410/400/proxy 測試）

- [ ] **Step 5: 型別與 lint**

Run: `.venv/bin/basedpyright estimator_king/crawler/async_pipeline.py`
Expected: 0 errors
Run: `uvx ruff check estimator_king/crawler/async_pipeline.py tests/test_async_pipeline.py`
Expected: All checks passed

- [ ] **Step 6: Commit**

```bash
git add estimator_king/crawler/async_pipeline.py tests/test_async_pipeline.py
git commit -m "feat(crawler): replace per-entry task fan-out with fixed worker pool"
```

---

## Task 2: Async-native fetch（B）— `fetch_product` 改 async + 移除 adapter

> 本 Task 把 `fetch_product` 改 async（直接 `await client.get`）、新增 `_build_snapshot`（to_thread 內解析）、移除同步 HTTP 相關殘骸；並把 `async_pipeline._handle` 改為直接 await、移除 `_AsyncToSyncHTTPAdapter`。fetch 改 async 會破壞舊的 `to_thread(fetch_with_adapter, ...)`，故 shopify.py 與 async_pipeline.py 必須同一 Task 一起改，連同 test_shopify.py 全面 async 化。

**Files:**
- Modify: `estimator_king/crawler/shopify.py`
- Modify: `estimator_king/crawler/async_pipeline.py`
- Test: `tests/test_shopify.py`（整檔重寫）
- Test: `tests/test_async_pipeline.py`（boom→ServerError、移除 ShopifyHTTPError import、加並行上限測試）

- [ ] **Step 1: 改寫 `shopify.py`——`fetch_product` 改 async + `_build_snapshot`**

在 `estimator_king/crawler/shopify.py`：

(a) 頂端 import 區：新增 `import asyncio`，並把 `_HTTPResponse`/`_HTTPGetter` 兩個 Protocol 替換為單一 async getter Protocol。把現況：

```python
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol, cast

from .html_extractor import extract_detail_sections as extract_html_details
from .snapshot import ProductSnapshot, ProductVariant, compute_content_hash

logger = logging.getLogger(__name__)


class _HTTPResponse(Protocol):
    status_code: int
    text: str


class _HTTPGetter(Protocol):
    def get(self, url: str) -> _HTTPResponse: ...
```

改為：

```python
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Protocol, cast

from .html_extractor import extract_detail_sections as extract_html_details
from .snapshot import ProductSnapshot, ProductVariant, compute_content_hash

logger = logging.getLogger(__name__)


class _AsyncGetter(Protocol):
    async def get(self, url: str) -> str: ...
```

(b) 移除 `ShopifyHTTPError` 類別與 `_raise_for_status` 函式。把現況：

```python
class ShopifyHTTPError(ShopifyProductError):
    url: str
    status_code: int

    def __init__(self, url: str, status_code: int):
        super().__init__(f"shopify http error: {status_code} {url}")
        self.url = url
        self.status_code = status_code


class ShopifyJSONError(ShopifyProductError):
    pass


def _raise_for_status(url: str, resp: _HTTPResponse) -> None:
    status = int(getattr(resp, "status_code", 0) or 0)
    if status < 200 or status >= 300:
        raise ShopifyHTTPError(url, status_code=status)
```

改為（只留 `ShopifyJSONError`）：

```python
class ShopifyJSONError(ShopifyProductError):
    pass
```

（保留 `ShopifyProductError` 基底類別不動。）

(c) 把現有同步 `fetch_product`（從 `def fetch_product(...)` 到檔尾 `return ProductSnapshotWithHash(...)`）整段替換為新的 async `fetch_product` + 同步 `_build_snapshot`：

```python
async def fetch_product(url: str, client: _AsyncGetter) -> ProductSnapshot:
    canonical_url = url.strip()
    if not canonical_url:
        raise ValueError("url must be a non-empty string")
    if canonical_url.endswith(".json"):
        canonical_url = canonical_url[: -len(".json")]
    canonical_url = canonical_url.rstrip("/")
    json_url = canonical_url + ".json"

    json_text = await client.get(json_url)
    html_text = await client.get(canonical_url)
    return await asyncio.to_thread(_build_snapshot, json_text, html_text, canonical_url)


def _build_snapshot(json_text: str, html_text: str, canonical_url: str) -> ProductSnapshot:
    try:
        payload = cast(object, json.loads(json_text))
    except Exception as e:  # noqa: BLE001
        raise ShopifyJSONError(f"invalid shopify json: {e}") from e

    product = _parse_product_json(payload)

    html_details = extract_html_details(html_text)
    logger.debug(f"Extracted html_details for {canonical_url}: {html_details}")
    if html_details:
        for key, value in html_details.items():
            logger.debug(f"  {key}: {value[:50] if len(value) > 50 else value}")
    snapshot = _build_snapshot_from_product_json(product, html_details=html_details)
    content_hash = compute_content_hash(snapshot)
    logger.debug(f"Product {snapshot.product_id} hash: {content_hash[:8]}...")
    return ProductSnapshotWithHash(
        product_id=snapshot.product_id,
        title=snapshot.title,
        description=snapshot.description,
        variants=snapshot.variants,
        html_details=snapshot.html_details,
        content_hash=content_hash,
    )
```

（`_parse_product_json`、`_build_snapshot_from_product_json`、`ProductSnapshotWithHash`、`_clean_body_html` 等**保留不變**；`_build_snapshot` 是新外層 wrapper，與 `_build_snapshot_from_product_json` 是不同函式。回傳型別註解維持 `-> ProductSnapshot`，實際回傳子類 `ProductSnapshotWithHash`——刻意。）

- [ ] **Step 2: 改寫 `async_pipeline.py`——`_handle` 直接 await、移除 adapter**

在 `estimator_king/crawler/async_pipeline.py`：

(a) import 區：移除不再使用的 `Any`、`Callable`、`cast`（它們僅用於 adapter 那行）。把：

```python
from typing import TYPE_CHECKING, Any, Callable, cast
```

改為：

```python
from typing import TYPE_CHECKING
```

(b) **移除整個 `_AsyncToSyncHTTPAdapter` 類別**（現 async_pipeline.py 的 `class _AsyncToSyncHTTPAdapter: ...`）。

(c) 在 `async_process_queue` 內移除 `loop`、`adapter`、`fetch_with_adapter`，並把 `_handle` 的 fetch 改為直接 await。把 Task 1 的：

```python
    loop = asyncio.get_running_loop()
    result = PipelineResult()

    async with AsyncHTTPClient(policy, proxy=proxy) as client:
        adapter = _AsyncToSyncHTTPAdapter(client, loop)
        fetch_with_adapter = cast(Callable[[str, Any], Any], fetch_product)

        async def _handle(entry: dict[str, int | str]) -> None:
            entry_id = int(entry["id"])
            product_url = str(entry["product_url"])
            try:
                snapshot = await asyncio.to_thread(fetch_with_adapter, product_url, adapter)
```

改為：

```python
    result = PipelineResult()

    async with AsyncHTTPClient(policy, proxy=proxy) as client:

        async def _handle(entry: dict[str, int | str]) -> None:
            entry_id = int(entry["id"])
            product_url = str(entry["product_url"])
            try:
                snapshot = await fetch_product(product_url, client)
```

（其餘 `_handle` 內容、queue/worker/gather、最終 log 與 return 維持 Task 1 的版本不變。`sync_products` 仍走 `await asyncio.to_thread(...)`。`ClientError` import 維持。）

- [ ] **Step 3: 整檔重寫 `tests/test_shopify.py` 為 async**

把 `tests/test_shopify.py` 整檔替換為：

```python
# pyright: reportMissingImports=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownParameterType=false
# pyright: reportMissingParameterType=false

import asyncio
import json
from pathlib import Path

import pytest

from estimator_king.crawler.async_http_client import ClientError, ServerError
from estimator_king.crawler.shopify import (
    ShopifyJSONError,
    fetch_product,
)
from estimator_king.crawler.snapshot import compute_content_hash


class _FakeAsyncClient:
    def __init__(self, *, json_text, html_text, json_exc=None, html_exc=None):
        self._json_text = json_text
        self._html_text = html_text
        self._json_exc = json_exc
        self._html_exc = html_exc

    async def get(self, url: str) -> str:
        if url.endswith(".json"):
            if self._json_exc is not None:
                raise self._json_exc
            return self._json_text
        if self._html_exc is not None:
            raise self._html_exc
        return self._html_text


def _read_fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")


def _mk_client(*, json_text, html_text, json_exc=None, html_exc=None):
    return _FakeAsyncClient(
        json_text=json_text, html_text=html_text, json_exc=json_exc, html_exc=html_exc
    )


def test_fetch_product_success_hololive_extracts_details_and_hash():
    json_text = _read_fixture("product_json_hololive.json")
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _mk_client(json_text=json_text, html_text=html_text)

    url = "https://shop.hololivepro.com/products/sample"
    snapshot = asyncio.run(fetch_product(url, client))

    assert snapshot.product_id == 1000000001
    assert snapshot.title == "Hololive Sample Product"
    assert "これは説明" in snapshot.description
    assert "<p>" not in snapshot.description, f"HTML tag found: {snapshot.description!r}"
    assert "<br>" not in snapshot.description, f"HTML tag found: {snapshot.description!r}"
    assert len(snapshot.variants) == 2
    assert "セット詳細" in snapshot.html_details
    assert "グッズ詳細" in snapshot.html_details

    expected_hash = compute_content_hash(snapshot)
    assert getattr(snapshot, "content_hash") == expected_hash
    assert len(expected_hash) == 64


def test_fetch_product_success_vspo_extracts_english_details_and_hash():
    json_text = _read_fixture("product_json_vspo.json")
    html_text = _read_fixture("product_html_vspo_basic.html")
    client = _mk_client(json_text=json_text, html_text=html_text)

    url = "https://store.vspo.jp/products/sample"
    snapshot = asyncio.run(fetch_product(url, client))

    assert snapshot.product_id == 1000000002
    assert snapshot.title == "VSPO Sample Product"
    assert len(snapshot.variants) == 1
    assert "Set Details" in snapshot.html_details
    assert "Merch details" in snapshot.html_details
    assert getattr(snapshot, "content_hash") == compute_content_hash(snapshot)


def test_fetch_product_no_detail_sections_returns_empty_dict():
    json_text = _read_fixture("product_json_hololive.json")
    html_text = _read_fixture("product_html_none.html")
    client = _mk_client(json_text=json_text, html_text=html_text)

    snapshot = asyncio.run(fetch_product("https://shop.hololivepro.com/products/x", client))
    assert snapshot.html_details == {}


@pytest.mark.parametrize("status,exc_type", [(404, ClientError), (500, ServerError)])
def test_fetch_product_http_error_propagates(status, exc_type):
    json_text = _read_fixture("product_json_hololive.json")
    html_text = _read_fixture("product_html_hololive_basic.html")
    url = "https://shop.hololivepro.com/products/x"
    client = _mk_client(
        json_text=json_text, html_text=html_text,
        json_exc=exc_type(url + ".json", status_code=status),
    )

    with pytest.raises(exc_type):
        _ = asyncio.run(fetch_product(url, client))


def test_fetch_product_html_http_error_propagates():
    json_text = _read_fixture("product_json_hololive.json")
    html_text = _read_fixture("product_html_hololive_basic.html")
    url = "https://shop.hololivepro.com/products/x"
    client = _mk_client(
        json_text=json_text, html_text=html_text,
        html_exc=ServerError(url, status_code=500),
    )

    with pytest.raises(ServerError):
        _ = asyncio.run(fetch_product(url, client))


def test_fetch_product_malformed_json_raises_shopify_json_error():
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _mk_client(json_text="{not json", html_text=html_text)

    with pytest.raises(ShopifyJSONError):
        _ = asyncio.run(fetch_product("https://shop.hololivepro.com/products/x", client))


def test_fetch_product_missing_product_object_raises_shopify_json_error():
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _mk_client(json_text=json.dumps({"nope": {}}), html_text=html_text)

    with pytest.raises(ShopifyJSONError):
        _ = asyncio.run(fetch_product("https://shop.hololivepro.com/products/x", client))


def test_fetch_product_accepts_url_with_json_suffix():
    json_text = _read_fixture("product_json_hololive.json")
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _mk_client(json_text=json_text, html_text=html_text)

    snapshot = asyncio.run(
        fetch_product("https://shop.hololivepro.com/products/x.json", client)
    )
    assert snapshot.product_id == 1000000001


def test_fetch_product_empty_url_raises_value_error():
    client = _mk_client(json_text="{}", html_text="")
    with pytest.raises(ValueError):
        _ = asyncio.run(fetch_product("   ", client))


def test_fetch_product_json_root_not_object_raises_shopify_json_error():
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _mk_client(json_text=json.dumps([1, 2, 3]), html_text=html_text)
    with pytest.raises(ShopifyJSONError):
        _ = asyncio.run(fetch_product("https://shop.hololivepro.com/products/x", client))


@pytest.mark.parametrize(
    "product_patch",
    [
        {"id": "not-int"},
        {"id": 123, "title": None},
        {"id": 123, "title": "X", "body_html": 42},
        {"id": 123, "title": "X", "variants": {"not": "a list"}},
        {"id": 123, "title": "X", "variants": ["nope"]},
        {
            "id": 123,
            "title": "X",
            "variants": [{"id": "bad", "title": "T", "price": "1"}],
        },
        {"id": 123, "title": "X", "variants": [{"id": 1, "title": None, "price": "1"}]},
        {"id": 123, "title": "X", "variants": [{"id": 1, "title": "T", "price": 1}]},
        {
            "id": 123,
            "title": "X",
            "variants": [{"id": 1, "title": "T", "price": "1", "sku": 9}],
        },
    ],
)
def test_fetch_product_json_validation_errors_raise_shopify_json_error(
    product_patch: dict[str, object],
):
    html_text = _read_fixture("product_html_hololive_basic.html")
    base: dict[str, object] = {"id": 123, "title": "X", "body_html": "", "variants": []}
    merged = dict(base)
    merged.update(product_patch)
    client = _mk_client(
        json_text=json.dumps({"product": merged}),
        html_text=html_text,
    )
    with pytest.raises(ShopifyJSONError):
        _ = asyncio.run(fetch_product("https://shop.hololivepro.com/products/x", client))


def test_fetch_product_allows_null_body_html_and_variants():
    html_text = _read_fixture("product_html_none.html")
    product = {"id": 123, "title": "X", "body_html": None, "variants": None}
    client = _mk_client(json_text=json.dumps({"product": product}), html_text=html_text)

    snapshot = asyncio.run(fetch_product("https://shop.hololivepro.com/products/x", client))
    assert snapshot.description == ""
    assert snapshot.variants == []
    assert snapshot.html_details == {}
    assert getattr(snapshot, "content_hash") == compute_content_hash(snapshot)
```

- [ ] **Step 4: 更新 `tests/test_async_pipeline.py`——boom→ServerError + 並行上限測試**

(a) 把頂端 import：

```python
from estimator_king.crawler.async_http_client import ClientError
from estimator_king.crawler.shopify import ShopifyHTTPError
```

改為：

```python
from estimator_king.crawler.async_http_client import ClientError, ServerError
```

（移除 `from estimator_king.crawler.shopify import ShopifyHTTPError` 整行。）

(b) 在 `test_fetch_failure_increments_failures_and_keeps_queue` 內，把：

```python
    def boom(url, client):
        raise ShopifyHTTPError(url, status_code=500)
```

改為：

```python
    def boom(url, client):
        raise ServerError(url, status_code=500)
```

（`ServerError`（5xx）非 `ClientError`，`_handle` 的 404/410 drop 不觸發 → entry 仍保留重試，測試斷言不變。）

(c) 在檔案末端新增並行上限測試：

```python
def test_worker_pool_caps_concurrency_at_worker_count(repo):
    for i in range(1, 7):
        repo.enqueue_url("hololive", f"https://x/products/{i}")
    policy = CrawlerPolicy(concurrency_per_domain=2)

    active = 0
    max_active = 0

    async def fake_fetch(url, client):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return _snap(int(url.rsplit("/", 1)[-1]))

    with patch("estimator_king.crawler.async_pipeline.fetch_product", side_effect=fake_fetch):
        asyncio.run(async_process_queue(
            "hololive", policy, repo, FakeEmbedder(), FakeVectorStore()))

    assert max_active == 2  # worker pool caps concurrent fetches at concurrency_per_domain
```

- [ ] **Step 5: 執行受影響測試**

Run: `.venv/bin/pytest tests/test_shopify.py tests/test_shopify_logging.py tests/test_async_pipeline.py tests/test_async_pipeline_logging.py tests/test_integration_async_pipeline.py -o addopts="" -q`
Expected: PASS（全部）

說明：`test_async_pipeline_logging.py` / `test_integration_async_pipeline.py` 以 `patch(async_pipeline.fetch_product, ...)` 運作；`fetch_product` 變 async 後 `patch` 自動採用 `AsyncMock`，`return_value`/同步 `side_effect=fake_fetch` 回傳值會被當成 await 結果，無需改動該兩檔（若任一意外失敗，回報而非硬改）。

- [ ] **Step 6: 全套件 + 型別 + lint**

Run: `.venv/bin/pytest tests/ -o addopts="" -q`
Expected: PASS（全綠）
Run: `.venv/bin/basedpyright estimator_king`
Expected: 0 errors（production 零錯誤閘門）
Run: `uvx ruff check estimator_king/crawler/shopify.py estimator_king/crawler/async_pipeline.py tests/test_shopify.py tests/test_async_pipeline.py`
Expected: All checks passed

- [ ] **Step 7: 確認 adapter 與 ShopifyHTTPError 已無殘留參照**

Run:
```bash
grep -rn "_AsyncToSyncHTTPAdapter\|ShopifyHTTPError\|_raise_for_status\|_HTTPGetter\|_HTTPResponse" estimator_king tests
```
Expected: 無輸出（全數移除）。若仍有，回頭清除。

- [ ] **Step 8: Commit**

```bash
git add estimator_king/crawler/shopify.py estimator_king/crawler/async_pipeline.py tests/test_shopify.py tests/test_async_pipeline.py
git commit -m "feat(crawler): make fetch_product async-native and drop sync-bridge adapter"
```

---

## 最終整體驗證（兩個 Task 完成後）

- [ ] **全套件測試**

Run: `.venv/bin/pytest tests/ -o addopts="" -q`
Expected: PASS（全綠）

- [ ] **Production 型別零錯誤閘門**

Run: `.venv/bin/basedpyright estimator_king`
Expected: 0 errors

- [ ] **全域 lint**

Run: `uvx ruff check estimator_king tests`
Expected: All checks passed
