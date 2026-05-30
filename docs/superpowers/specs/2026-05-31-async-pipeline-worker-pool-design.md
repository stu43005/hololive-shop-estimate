# 設計規格：async_process_queue worker pool + async-native fetch

- 日期：2026-05-31
- 狀態：設計確認，待寫實現計畫

## 1. 背景與問題

Discord bot 按 Ctrl+C 後要等數分鐘才退出，期間 discord 持續噴 `heartbeat blocked`，最後以 `bot.start()` 的 `CancelledError` + `Unclosed client session` 結束。

### 已確認的 root cause（實證）

`async_process_queue`（[async_pipeline.py](../../estimator_king/crawler/async_pipeline.py)）一次性把整個佇列展開成「每筆一個 task」：

```python
await asyncio.gather(*[_bounded(entry) for entry in entries])   # 現 async_pipeline.py:104
```

佇列有幾千筆，就有幾千個 `_bounded` task 同時存活（絕大多數 park 在 `asyncio.Semaphore` 上）。關閉時 `scheduler_task.cancel()` 要拆掉這一大叢 waiter，loop 反覆掃描 semaphore 的 `_waiters`（heartbeat traceback 一致卡在 `Semaphore.acquire → _wake_up_next → fut.done()`），把共用的 bot event loop 佔住數分鐘 → 心跳送不出去。

證據：
- 診斷顯示 `shutdown:start` 當下有數百~數千個 `_bounded` 存活。
- 心跳 traceback 一致停在 `Semaphore._wake_up_next`。
- 微基準：取消大量 park 在 semaphore 的 task 隨 N 成長（O(N²) 級）。

加重因素：`_AsyncToSyncHTTPAdapter`（async_pipeline.py:33-40）讓 worker thread 透過 `run_coroutine_threadsafe(client.get(url), loop).result()` 阻塞等待**同一個 loop** 跑協程。取消後留下孤兒 `client.get` task（非 scheduler_task 子 task、取消不到）+ 阻塞中的 worker thread，要等 in-flight HTTP 自然排乾。

### 確認過的取消語意（實機驗證）

- `await asyncio.to_thread(blocking)` 被 cancel 時**立即**回傳 `CancelledError`，worker thread 被孤兒化在背景（不阻塞母 task）。
- 純 `asyncio.Semaphore` 取消很快（6000 task ~0.22s），但真實大佇列下與孤兒 in-flight HTTP 疊加成數分鐘。

## 2. 解決方向（已選定：A + B）

1. **A — worker pool**：把 `gather-per-entry` + `Semaphore` 改成固定 `concurrency` 個 worker 從共享 `asyncio.Queue` 拉取，讓任何時刻只有 `concurrency` 個 task 存活。直接消除 N-task semaphore churn 與心跳餓死。
2. **B — async-native fetch**：把 `fetch_product` 改 async、直接 `await client.get(...)`，移除 `_AsyncToSyncHTTPAdapter` 的 thread↔loop ping-pong（`run_coroutine_threadsafe(...).result()`）。fetch 階段變成可被 cancel，徹底消除孤兒 `client.get` task + 阻塞 thread 的 hang 根源（即 §3.4 當初需要「第二次 Ctrl+C 強退」兜底的那個 `.result()` 可能無限等待）。

效果：關閉從數分鐘降到接近即時；唯一殘留的不可中途取消工作 = `sync_products` 的 to_thread（embed + ChromaDB，真同步），有界於 `concurrency`、可接受。

## 3. 範圍與設計細節

### 3.1 Worker pool（A）— `estimator_king/crawler/async_pipeline.py`

把 `async_process_queue` 的併發機制改寫：

- 仍以 `entries = state_repo.peek_all(store_id)` 取得全部 entries（N 筆**資料**在記憶體無妨；問題是 N 個 **task**）。空佇列維持早退。
- 建立 `asyncio.Queue[dict[str, int | str]]`，把所有 entries `put_nowait` 進去。
- 定義 worker：
  ```python
  async def _worker() -> None:
      while True:
          try:
              entry = queue.get_nowait()
          except asyncio.QueueEmpty:
              return
          await _handle(entry)
  ```
- 啟動剛好 `policy.concurrency_per_domain` 個 worker，`await asyncio.gather(*workers)`。
- **移除 `sem = asyncio.Semaphore(...)` 與 `_bounded`**（worker 數本身就是並行上限）。
- **移除 `lock = asyncio.Lock()`** 與 `_handle` 內的 `async with lock:`——`result` 的累加與 progress log 都是同步、其間無 `await`，在單執行緒 event loop 上安全（worker 之間不會交錯到累加區塊中間）。`_handle` 的累加改為直接同步執行。
- `_handle` 的其餘邏輯（fetch → sync → delete_queue_entry → 累加；except 區塊的 `increment_consecutive_failures` 與 404/410 drop）維持不變，僅 fetch 呼叫方式依 §3.2 改變。

worker pool 取消行為：cancel `gather(*workers)` 只需拆 `concurrency` 個 worker；每個 worker 不是停在 `queue.get_nowait()`（同步、瞬間）就是停在 `await _handle`（其 `await client.get` 可立即 cancel、`await asyncio.to_thread(...)` 立即回傳並孤兒化純 CPU thread）。無 semaphore churn。

並行度說明：`AsyncHTTPClient` 內部本就有 per-domain semaphore（亦為 `concurrency_per_domain`），與 worker 數對齊，不會額外序列化。

### 3.2 Async-native fetch（B）— `estimator_king/crawler/shopify.py`

把 `fetch_product` 改為 async、直接走 `AsyncHTTPClient.get`（回傳 `str`）：

- 簽名改為：
  ```python
  async def fetch_product(url: str, client: _AsyncGetter) -> ProductSnapshot:
  ```
  其中 `_AsyncGetter` 為本地 `typing.Protocol`：`async def get(self, url: str) -> str: ...`（取代現有同步的 `_HTTPGetter`）。
- 流程：
  ```python
  canonical_url = <既有正規化邏輯>
  json_url = canonical_url + ".json"
  json_text = await client.get(json_url)
  html_text = await client.get(canonical_url)
  return await asyncio.to_thread(_build_snapshot, json_text, html_text, canonical_url)
  ```
- **新增同步函式 `_build_snapshot(json_text: str, html_text: str, canonical_url: str) -> ProductSnapshot`**，把現有的同步解析整合進去：`json.loads(json_text)` → `_parse_product_json` → `extract_html_details(html_text)` → `_build_snapshot_from_product_json(...)` → `compute_content_hash` → 回傳 `ProductSnapshotWithHash`。此函式在 `asyncio.to_thread` 中執行，保持 CPU-bound 解析 off-loop（與原本「整個 fetch 在 thread」的 off-loop 行為一致）。
- **移除**：同步 `_HTTPResponse`、`_HTTPGetter` Protocol、`_raise_for_status` 函式。HTTP 狀態錯誤改由 `AsyncHTTPClient.get` 內部處理（4xx → `ClientError`、5xx → `ServerError`、403/430 → `WAFBlockedError`、429 → 重試）；這些例外從 `await client.get` 直接傳出。
- **移除 `ShopifyHTTPError`**（其唯一 production 觸發點 `_raise_for_status` 被移除後即成孤兒；4xx 改由 `ClientError` 表示）。保留 `ShopifyProductError`、`ShopifyJSONError`（`_parse_product_json`／`_build_snapshot` 仍會 raise `ShopifyJSONError`）。
- shopify.py 需新增 `import asyncio`（給 `asyncio.to_thread`）。
- 行為變更（明確記錄）：原本先抓 json、解析失敗即 raise（不抓 html）；新版先抓 json + html 兩個請求，再於 to_thread 內解析。錯誤路徑會多一次 html 抓取（罕見、可接受），成功路徑請求數不變。

### 3.3 移除 adapter plumbing — `estimator_king/crawler/async_pipeline.py`

- **移除 `_AsyncToSyncHTTPAdapter` 類別**。
- 移除 `async_process_queue` 內 `loop = asyncio.get_running_loop()`、`adapter = _AsyncToSyncHTTPAdapter(...)`、`fetch_with_adapter = cast(...)`。
- `_handle` 內改為直接 `snapshot = await fetch_product(product_url, client)`（`client` 為 `async with AsyncHTTPClient(...)` 的實例）；`sync_products` 維持 `await asyncio.to_thread(sync_products, ...)`。
- 清掉因此不再使用的 import（`Callable`、`Any`、`cast`——以實際殘留使用為準移除；`fetch_product` 的 import 改為被 `_handle` 直接 await）。

### 3.4 行為變更彙整（明確記錄）

- **關閉延遲**：數分鐘 → 接近即時（in-flight 上限 = `concurrency`，且 fetch 可被 cancel）。心跳餓死消除。
- **fetch 錯誤型別**：HTTP 狀態錯誤由 `ShopifyHTTPError` 改為 `AsyncHTTPClient` 的 `ClientError`/`ServerError`/`WAFBlockedError`。`_handle` 的 404/410 drop 邏輯（`isinstance(exc, ClientError) and exc.status_code in (404, 410)`）不變且仍正確。
- **`_AsyncToSyncHTTPAdapter` 移除**：fetch 不再佔用 thread pool 槽、不再有孤兒 `client.get` task。
- 不改 `sync_products`、`AsyncHTTPClient`、`CrawlerPolicy`、`runner.py`/`scheduler.py` 的關閉邏輯（Task 6 的兩段式關閉 + 第二次強退保留為最終逃生口；A+B 後正常情況不需動用）。

## 4. 受影響檔案清單

production：
- `estimator_king/crawler/async_pipeline.py`（worker pool；移除 adapter/semaphore/lock；fetch 改直接 await）
- `estimator_king/crawler/shopify.py`（`fetch_product` 改 async + `_build_snapshot`；移除 `_HTTPResponse`/`_HTTPGetter`/`_raise_for_status`/`ShopifyHTTPError`；加 `_AsyncGetter` Protocol、`import asyncio`）

tests：
- `tests/test_shopify.py`（所有 `fetch_product` 測試改 async：`asyncio.run` + async fake client 回 `str`；HTTP-error 測試由斷言 `ShopifyHTTPError` 改為斷言 `AsyncHTTPClient` 的 `ClientError`；移除 `ShopifyHTTPError` import）
- `tests/test_async_pipeline.py`（`fetch_product` 變 async → `patch(..., return_value=_snap)` 自動 AsyncMock；`side_effect=boom` 的 `boom` 仍可 raise；其 `raise ShopifyHTTPError(...)` 改為 raise 一個仍存在的例外——例如 `ClientError(url, status_code=500)` 或一般 `Exception`，並移除 `ShopifyHTTPError` import；新增 worker-pool 行為測試見 §5）
- `tests/test_async_pipeline_logging.py`（`patch(fetch_product)` 自動 AsyncMock；確認 logging 斷言仍成立）
- `tests/test_integration_async_pipeline.py`（`patch(fetch_product, side_effect=fake_fetch)`：`fake_fetch(url, client)` 為同步函式回傳 snapshot，AsyncMock 會把同步回傳值當成 await 結果，相容；確認三個整合測試仍綠）

## 5. 測試策略

- **沿用本專案既有 async 測試慣例**（依目標檔）：`test_shopify.py`、`test_async_pipeline.py`、`test_integration_async_pipeline.py` 用 `asyncio.run(...)` 在同步 `def test_...` 內呼叫（比照 `test_async_pipeline.py` 現況）。
- **test_shopify.py 的 async fake client**：以 `async def get(self, url) -> str` 的 fake 物件取代現有回傳具 `.status_code`/`.text` 物件的同步 fake；依 URL 後綴回傳 json 字串或 html 字串。HTTP-error 測試讓 fake `get` 對指定 URL `raise ClientError(url, status_code=...)`（`from estimator_king.crawler.async_http_client import ClientError`），斷言 `fetch_product` 直接傳出該 `ClientError`。malformed-json / 缺 product 物件等測試維持斷言 `ShopifyJSONError`（在 `_build_snapshot` 內 raise、經 to_thread await 傳出）。
- **worker pool 行為測試**（新增於 `test_async_pipeline.py`）：
  - 多筆 entries（例如 5 筆）+ `concurrency_per_domain=2`，patch `fetch_product`/`sync_products`，斷言全部 entries 都被處理（`result.processed == 5`、queue 排空）。
  - 並行上限：以可觀測的併發計數（patch 的 fetch 在進入時 +1、離開時 -1，記錄 max），斷言同時併發數不超過 `concurrency_per_domain`。
- **既有 async_pipeline 測試**：確認 `patch(async_pipeline.fetch_product, return_value=_snap)` 在 fetch_product 變 async 後自動成為 AsyncMock（`await` 回傳 `_snap`），且 `side_effect` 版本相容。
- 全套件須維持全綠（先前 242 passed 基準；本次新增/改寫測試後數字會變動，但不得有 failure）。

## 6. 第三方/標準庫查證（research）

依專案規則，以下須在撰寫實現計畫前查證（讀文件/原始碼可解答者，不得寫進 plan）：

1. `unittest.mock.patch` 對「已成為 coroutine function 的 patch 目標」是否自動採用 `AsyncMock`，以及 `side_effect` 為同步函式（回傳值 / raise）時的行為（確認既有 `test_async_pipeline.py`/`test_integration_async_pipeline.py` 的 `patch(fetch_product, return_value=...)`/`side_effect=fake_fetch` 在 fetch_product 變 async 後仍正確）。
2. `asyncio.Queue.get_nowait()` 在空佇列時拋 `asyncio.QueueEmpty` 的行為，確認 worker 以此判斷結束正確。

（`asyncio.to_thread` 取消語意、`AsyncHTTPClient.get` 回傳/例外型別已於前期調查與既有程式碼確認。）

## 7. 非目標（Out of Scope）

- 不改 `sync_products`（embed + ChromaDB 仍為同步、仍走 `asyncio.to_thread`；其不可中途取消為可接受殘留）。
- 不改 `AsyncHTTPClient`、`CrawlerPolicy`、`ProxyConfig`。
- 不改 `runner.py`/`scheduler.py` 的關閉邏輯（Task 6 的兩段式關閉保留）。
- 不為兩個 fetch（json + html）引入並發（維持循序；per-domain rate limiter 本就序列化）。
- 不引入關閉逾時/強制等額外機制（A+B 已使正常關閉接近即時；既有第二次 Ctrl+C 強退為最終逃生口）。
