# 修正 product_url 捏造（pass-through 真實 URL）+ 404 不再無限重試 + 一次性遷移

日期：2026-05-30

## 背景與問題

### 現象

`run`/`crawl` 時對 `https://shop.hololivepro.com/products/8087824892124.json` 這類「數字 ID」URL 抓取回傳 404。實測 DB（`estimator_king.db`）中 `hololive` store 的 **2285 筆產品 `product_url` 全部**是 `.../products/<數字>` 形式，並非個案；佇列累積 2318 筆卡死項目；`consecutive_sitemap_misses` 最大已達 3，`consecutive_failures` 最大已達 2。

### 根本原因

[`estimator_king/sync/engine.py`](../../../estimator_king/sync/engine.py) 的 `sync_products()` 在第 89 行：

```python
product_url = f"{base_url}/products/{snapshot.product_id}"
```

用 Shopify product JSON 的數字 `product.id`（例如 `8087824892124`）重新捏造 `product_url`，丟棄了真正抓取成功的 handle URL。資料流斷點：

1. `SitemapEnumerator` 從 sitemap 取得 handle URL（`/products/<handle>`），入佇列。
2. `async_pipeline._handle` 用 handle URL 呼叫 `fetch_product()` 抓取成功，回傳的 `ProductSnapshot` 只帶數字 `product_id`，**不帶來源 URL**。
3. 呼叫 `sync_products([snapshot], store_id, store_base_url, ...)` 時，**真實抓取的 handle URL 沒有被傳入**。
4. `sync_products` 用數字 `product_id` 捏造 `product_url` 存入 DB。

Shopify storefront 的 `/products/<x>` 與 `/products/<x>.json` 只認 handle，不認數字 product ID（數字 ID 屬 Admin API），故存入的數字 URL 一抓就 404。

### 兩條級聯症狀

- **症狀 A（被報告的 404）**：後續 crawl 由 `enqueue_oldest_products`（[`pipeline.py`](../../../estimator_king/crawler/pipeline.py)）把 DB 存的數字 URL 重新入佇列；失敗的佇列項目刻意保留重試（[`async_pipeline.py`](../../../estimator_king/crawler/async_pipeline.py) except 區塊），且無任何機制刪除，導致數字 URL 永遠卡在佇列重試 404，`consecutive_failures` 累加。
- **症狀 B（sitemap 永遠對不上）**：`populate_queue_from_sitemap` 用 sitemap 的 handle URL 呼叫 `get_by_product_url()`（精確字串比對），DB 存的是數字 URL → 永遠 None → 每個產品被當「新的」重抓（log 顯示「2286 total, 2286 new enqueued」即使已有 2285 筆）；同時所有數字 URL 產品不在 sitemap set 內 → 每輪 `increment_sitemap_miss`。`inactive_sitemap_miss_threshold` 為 4，目前已到 3，再跑一輪未修就會把健康商品全標下架。

## 目標

1. `product_url` 改用真正抓取成功的 URL（pass-through），不再用數字 `product_id` 捏造。
2. 修正後一次正常 crawl 即可自癒既有錯誤 URL（不需重新 embedding）。
3. 清除既有錯誤資料：卡死佇列與被 bug 灌大的計數器，避免修正後第一輪 crawl 誤標 inactive。
4. 順手修「佇列失敗項目永遠重試」放大器：確定消失（HTTP 404/410）的項目不再每輪重試。

## 非目標（明確排除）

- 不重建 ChromaDB（向量不受此 bug 影響）。
- 不更動 `external_key`（仍為 `{store_id}:{product_id}`，數字 product_id 本即穩定鍵）。
- 不更動 WAF（403/430）、429、5xx 的重試策略。
- 不直接以 SQL 改寫 `product_url`（DB 未儲存 handle，無法重建；交由 crawl 自癒）。

## 設計

### HTTP 例外分類（既有行為，作為 404 判斷依據）

[`async_http_client.py`](../../../estimator_king/crawler/async_http_client.py) `_request_once` 對狀態碼的映射：

- `403, 430` → `WAFBlockedError`
- `429` → `RateLimitError`（HTTP 層重試）
- `500–599` → `ServerError`（HTTP 層重試）
- `400–499`（其餘，含 404、410）→ `ClientError`（不重試，立即拋出，帶 `status_code`）

`ClientError` 會原樣穿過 `_AsyncToSyncHTTPAdapter` 的 `run_coroutine_threadsafe(...).result()` 與 `asyncio.to_thread`，在 `_handle` 的 except 中可用 `isinstance` 判斷。「確定消失」定義為 `ClientError` 且 `status_code in (404, 410)`。

### 元件 1：`sync_products` 改用傳入的 URL（`estimator_king/sync/engine.py`）

- 簽名由
  `sync_products(snapshots: Iterable[ProductSnapshot], store_id: str, base_url: str, repository, embedder, vector_store)`
  改為
  `sync_products(items: Iterable[tuple[str, ProductSnapshot]], store_id: str, repository, embedder, vector_store)`。
  **移除 `base_url` 參數。**
- 迴圈改為 `for product_url, snapshot in items:`，**刪除第 89 行的 f-string 捏造**，`product_url` 直接取自 tuple。
- `_format_product_document(snapshot, store_id, product_url)` 已接受 `product_url` 參數，內部邏輯不變。
- `external_key = f"{store_id}:{snapshot.product_id}"`（不變）。
- 其餘 unchanged/skip 判斷、`repository.upsert(...)` 寫入欄位皆不變，唯 `product_url` 來源改為傳入值。

### 元件 2：pipeline 傳遞真實 URL + 404 不重試（`estimator_king/crawler/async_pipeline.py`）

- `_handle` 內：
  - `snapshot` 取得後，呼叫改為
    `sync_products([(product_url, snapshot)], store_id, state_repo, embedder, vector_store)`
    （`product_url` 即佇列項目的 URL，亦即實際抓取成功的 URL）。
- `async_process_queue` 簽名**移除 `store_base_url` 參數**（原僅用於傳給 `sync_products`）。
- except 區塊改為：
  - 維持 `existing = state_repo.get_by_product_url(store_id, product_url)`；若 `existing is not None` 則 `increment_consecutive_failures(existing.external_key)`（計入 inactive 門檻，行為不變）。
  - **新增**：`if isinstance(exc, ClientError) and exc.status_code in (404, 410): state_repo.delete_queue_entry(entry_id)`（確定消失，不再每輪重試）。
  - 其他例外（`ServerError`、`WAFBlockedError`、`RateLimitError`、一般 `Exception`）：**維持保留佇列項目重試**（現狀，不刪除）。
  - 新商品（`existing is None`）遇 404/410：跳過 increment（無 row），但仍刪除佇列項目。
  - 透過 `from estimator_king.crawler.async_http_client import ClientError` 匯入。
  - 為取得例外物件，except 子句改為 `except Exception as exc:`。

### 元件 3：呼叫端（`estimator_king/crawler/cycle.py`）

- 第 57–59 行 `async_process_queue(...)` 呼叫**移除 `store.base_url` 引數**，其餘引數順序不變。

### 元件 4：一次性遷移腳本（新增 `scripts/migrate_2026_05_30_fix_product_urls.py`）

- 提供可匯入的函式（例如 `migrate(db_path: str) -> tuple[int, int]`，回傳 `(queue_deleted, rows_reset)`）以利測試，並附 `__main__` 進入點。
- 動作（單一 transaction）：
  1. `DELETE FROM crawl_queue`（清空全部卡死佇列；佇列為暫時性，每輪由 sitemap + budget 重建）。
  2. `UPDATE products SET consecutive_failures = 0, consecutive_sitemap_misses = 0, updated_at = <now> WHERE consecutive_failures > 0 OR consecutive_sitemap_misses > 0`（清除被 bug 灌大的計數器；`WHERE` 使重置筆數可量測且冪等）。
  3. 印出刪除筆數與重置筆數。
- **冪等**：再次執行刪除 0 筆、重置 0 筆。
- `db_path` 來自命令列參數或 `DATABASE_PATH` 環境變數，預設 `./estimator_king.db`。
- 不更動 `product_url`（交由 crawl 自癒）。
- 文件/腳本註明：**執行前需停掉 bot**（單一 DB writer，避免 `database is locked`）。

### 自癒流程（部署後驗證）

修正 + 遷移後跑一次普通 `crawl`：

1. `populate_queue_from_sitemap`：sitemap handle URL 與 DB 舊數字 URL 仍對不上 → 全數重新入佇列為「新」；`increment_sitemap_miss` 使數字 row 計數 0→1（遠低於門檻 4，安全）。
2. drain 佇列：handle URL 抓取成功 → `sync_products` 以相同數字 `product_id` 組成 `external_key` → 命中既有 row → `upsert` 就地把 `product_url` 改為 handle URL；因 `content_hash` 未變且 `last_indexed_at` 非空 → `unchanged=True` → **不重新 embedding**；`consecutive_failures` 重置為 0。
3. inactive sweep：misses=1 < 4、failures=0 < 3 → 不誤標。
4. 第二輪起：sitemap handle URL 命中 DB（已是 handle URL）→ `record_sitemap_seen` 使 misses 歸零，系統完全乾淨。

## 測試策略

### `tests/test_sync_engine.py`、`tests/test_sync_engine_logging.py`

- 全面更新為 tuple 簽名 `sync_products([(url, snapshot)], store_id, repo, embedder, vector_store)`（移除 `base_url` 引數）。
- **新增回歸測試**：以 handle URL（如 `https://shop.hololivepro.com/products/some-handle`）與帶數字 `product_id` 的 snapshot 呼叫 `sync_products`，斷言寫入 DB 的 `product_url` 等於該 handle URL，**而非** `.../products/<numeric_id>`。

### `tests/test_async_pipeline.py`、`tests/test_async_pipeline_logging.py`、`tests/test_integration_async_pipeline.py`

- 更新 `async_process_queue(...)` 呼叫，移除 `store_base_url` 引數。
- **新增測試**：`fetch_product` 拋 `ClientError(url, status_code=404)` 時，`delete_queue_entry` 被呼叫（佇列項目移除）且 `increment_consecutive_failures` 被呼叫（若有對應 row）；`result.failed` +1。
- **新增測試**：`fetch_product` 拋 `ServerError` 或一般 `Exception` 時，`delete_queue_entry` **未**被呼叫（佇列項目保留重試），`result.failed` +1。

### `tests/test_migrate_fix_product_urls.py`（新增）

- 建立臨時 DB（套用既有 schema），塞入：含數字 URL 的 products（`consecutive_failures > 0`、`consecutive_sitemap_misses > 0`）與若干 `crawl_queue` 項目。
- 呼叫遷移函式，斷言：`crawl_queue` 清空、受影響 products 的兩個計數器歸零、回傳筆數正確。
- **冪等測試**：第二次呼叫回傳 `(0, 0)` 且狀態不變。

## 驗證（toolchain）

依專案 `CLAUDE.md`：

- Type check：`.venv/bin/basedpyright estimator_king scripts`（production code 0 errors）。
- Lint：`uvx ruff check estimator_king scripts tests`。
- 相關測試：`.venv/bin/python -m pytest tests/test_sync_engine.py tests/test_sync_engine_logging.py tests/test_async_pipeline.py tests/test_async_pipeline_logging.py tests/test_integration_async_pipeline.py tests/test_migrate_fix_product_urls.py -v -o addopts=""`。

## 部署/操作順序（一次性）

1. 合併程式修正（元件 1–4）。
2. 停掉 bot（單一 DB writer）。
3. 執行遷移腳本一次。
4. 啟動 bot 或執行一次 `crawl`，由自癒流程就地修正 `product_url`。
5. 觀察第二輪 crawl：`new enqueued` 應大幅下降、`sitemap_misses` 歸零、無 404 數字 URL。
