# 調整 LOG 觀察性 — 設計規格

日期：2026-05-30

## 目標

提升 estimator_king 的 log 可觀察性，讓運維者能從 log 直接看出：

1. 一條訊息來自 BOT 還是 CRAWLER 子系統。
2. 所有對外請求（爬蟲 HTTP 抓取 + LLM API 呼叫）的明細（DEBUG）。
3. CRAWLER 每一輪的處理進度與數量（sitemap 總數/新增數、隊列進度心跳、每 store 完成統計）。
4. BOT 收到使用者請求後的處理進度（生命週期 INFO + 細節 DEBUG）。

本次只調整 logging，不改變任何業務行為、回傳值或控制流。

## 非目標（Out of Scope）

- 不引入結構化（JSON）log 或第三方 logging 函式庫（如 structlog、loguru）。
- 不開啟第三方函式庫（aiohttp、urllib3、requests、openai、httpx）的內建 logger。
- 不為共用模組（`llm/`、`sync/`）加入「呼叫來源是 BOT 或 CRAWLER」的精準歸類機制。
- 不新增 log 檔案輪替、log 上傳或 log aggregation 整合。
- 不調整 `crawl` 子命令印到 stdout 的 JSON counters 輸出。

## 設計決策

### D1. 子系統區分方式：logger name 階層 + 格式加 `%(name)s`

在 `_main()` 的 `logging.basicConfig` 格式字串加入 logger name，子系統由 logger
name 的模組路徑前綴自然區分，不使用額外的 `[BOT]`/`[CRAWLER]` 標籤。

- `estimator_king.crawler.*` → CRAWLER 子系統
- `estimator_king.bot.*` → BOT 子系統
- `estimator_king.llm.*` / `estimator_king.sync.*` → 共用/中性模組

### D2. DEBUG 請求記錄：在各請求點手寫 `logger.debug`

在程式自身的請求點手動加 `logger.debug`，掌控訊息內容（method/url/status/耗時），
不開啟第三方函式庫 logger。涵蓋全部四個對外請求點：兩個爬蟲 HTTP client 與兩個
LLM 呼叫（embedding、chat）。只記 url/status/耗時，**不記錄 header 或 body**，
避免洩漏 API key。

### D3. CRAWLER 進度：每 store summary + 週期性心跳

每 store 記錄 sitemap 總數/新增數、隊列起始數、完成統計；隊列處理過程每處理固定
數量輸出一次進度心跳。

### D4. BOT 進度：關鍵生命週期 INFO + 細節 DEBUG

收到請求、處理完成、失敗於 INFO/WARNING/ERROR；每 chunk 進度於 DEBUG。

### 已知取捨

`llm/embeddings.py` 同時被 BOT（`bot/estimator.py`）與 CRAWLER（`sync/engine.py`）
呼叫。採用 logger name 階層後，其 log 的 name 固定為 `estimator_king.llm.embeddings`，
無法分辨呼叫來源是 BOT 還是 CRAWLER，視為第三類中性訊息。若日後需精準歸類，須另外
傳入 component context，本次不實作（YAGNI）。

## 變更項目

### 1. 全域 log 格式與 root logger 收斂

#### 1.1 格式字串（`estimator_king/__main__.py` `_main()`）

`logging.basicConfig` 的 `format` 由：

```
"%(asctime)s [%(levelname)s] %(message)s"
```

改為：

```
"%(asctime)s [%(levelname)s] %(name)s: %(message)s"
```

`level`、`stream=sys.stderr` 維持不變。

#### 1.2 將使用 root logger 的模組改為模組 logger

下列模組目前直接呼叫 `logging.xxx`（掛在 root logger，格式化後 name 為 `root`），
須改為在模組頂層建立 `logger = logging.getLogger(__name__)` 並改用 `logger.xxx`：

- `estimator_king/__main__.py`：共 4 處須改——`logging.error(...)` 3 處（第 62、
  69、79 行）與 `logging.info(...)` 1 處（第 108 行）→ 全部改為 `logger.error/info`
  （name 變為 `estimator_king.__main__`）。
- `estimator_king/bot/runner.py`：所有 `logging.info(...)`（含 `on_ready`、
  `shutdown` 內共 5 處）→ `logger.info`（name 變為 `estimator_king.bot.runner`，
  歸 BOT）。
- `estimator_king/sync/engine.py`：`logging.exception(...)`（第 117 行）→
  `logger.exception`（name 變為 `estimator_king.sync.engine`）。
- `estimator_king/crawler/html_extractor.py`：第 159 行的區域 `import logging` 與
  其後的 logging 呼叫，改為使用模組頂層 `logger = logging.getLogger(__name__)`
  並移除區域 import（name 變為 `estimator_king.crawler.html_extractor`，歸 CRAWLER）。
- `estimator_king/crawler/shopify.py`：已有頂層 `import logging` 但無模組 logger，
  且有 3 處裸 `logging.debug(...)`（第 153、156、159 行）。新增模組頂層
  `logger = logging.getLogger(__name__)`，將 3 處改為 `logger.debug(...)`
  （name 變為 `estimator_king.crawler.shopify`，歸 CRAWLER）。

已正確使用 `logging.getLogger(__name__)` 的模組（`crawler/cycle.py`、
`crawler/pipeline.py`、`crawler/async_pipeline.py`、`bot/estimator.py`、
`bot/scheduler.py`）不需改動其 logger 建立方式。

### 2. DEBUG：所有對外請求

統一格式：記錄 HTTP 方法、URL、回應狀態碼、耗時（毫秒，以 `time.monotonic()` 量測）。

#### 2.1 爬蟲非同步 HTTP（`estimator_king/crawler/async_http_client.py`）

在 `AsyncHTTPClient._request_once` 中：

- 於 `async with session.request(...) as resp:` 取得 `resp` 並讀出 `status` 後、
  進入各狀態碼分支（WAF/429/5xx/4xx raise）**之前**，輸出一筆 DEBUG，確保所有
  完成的請求（含 4xx/5xx/429/WAF）都被記錄到其真實狀態碼。
- 耗時量測：在發出 `session.request` 前以 `time.monotonic()` 取起始時間，取得
  `status` 後計算經過毫秒。
- 訊息格式：`"GET %s -> %s in %.0fms"`，參數為 `url`、`status`、耗時毫秒。

`module` 已有 `import time`。需新增 `import logging` 與模組 logger
`logger = logging.getLogger(__name__)`（目前此檔無 logger）。

#### 2.2 爬蟲同步 HTTP（`estimator_king/crawler/http_client.py`）

在 `HTTPClient._request_once` 中：

- 於 `resp = self.session.request(...)` 之後、讀出 `status` 後、進入狀態碼分支
  raise **之前**，輸出一筆 DEBUG（同 2.1 理由，涵蓋所有完成的請求）。
- 耗時量測：在 `self.session.request(...)` 前後以 `time.monotonic()` 計算經過毫秒。
- 訊息格式：`"%s %s -> %s in %.0fms"`，參數為 `method`、`url`、`status`、耗時毫秒
  （此 client 的 `request` 帶 `method` 參數，故 method 不寫死 GET）。

`module` 已有 `import time`。需新增 `import logging` 與模組 logger
`logger = logging.getLogger(__name__)`（目前此檔無 logger）。

#### 2.3 Embedding API（`estimator_king/llm/embeddings.py`）

在 `EmbeddingProvider._embed` 中：

- 於 `self._client.embeddings.create(...)`（兩個分支：有/無 `dimensions`）前後
  以 `time.monotonic()` 量測，呼叫完成後輸出一筆 DEBUG。
- 訊息格式：`"embedding request: %d inputs model=%s -> %.0fms"`，參數為
  `len(inputs)`、`self._config.embedding_model`、耗時毫秒。

需新增 `import logging`、`import time` 與模組 logger
`logger = logging.getLogger(__name__)`（目前此檔無 logger 與這兩個 import）。

#### 2.4 Chat API（`estimator_king/llm/chat.py`）

在 `ChatProvider.estimate` 中：

- 於分派到 `_estimate_structured` / `_estimate_json_object`（兩者皆會呼叫
  `self._client.chat.completions.*`）前後以 `time.monotonic()` 量測整體耗時，
  完成後輸出一筆 DEBUG。
- 訊息格式：`"chat request: model=%s structured=%s -> %.0fms"`，參數為
  `self._config.chat_model`、`self._config.chat_structured_output`、耗時毫秒。
- 即使下游拋出 `EstimationError`，仍應記錄已耗時間：`estimate()` 目前無 try 區塊，
  須新增一個 `try/finally` 包住既有的分派呼叫（`if self._config.chat_structured_output:
  return self._estimate_structured(messages)` / `return self._estimate_json_object(messages)`），
  於 `finally` 輸出 DEBUG（finally 不吞例外，例外照常往上傳遞，行為不變）。

需新增 `import logging`、`import time` 與模組 logger
`logger = logging.getLogger(__name__)`（目前此檔無 logger 與這兩個 import）。

### 3. CRAWLER INFO：進度與數量

#### 3.1 sitemap 數量（`estimator_king/crawler/pipeline.py` `populate_queue_from_sitemap`）

在計算出 `sitemap_urls` 與 `enqueued` 後（函式 return 前），新增一筆 INFO：

- 訊息格式：`"store=%s sitemap: %d total, %d new enqueued"`，參數為
  `store.id`、`len(sitemap_urls)`、`enqueued`。
- 既有的空 sitemap WARNING（`"Sitemap for %s returned 0 URLs — skipping"`）保留不動；
  空 sitemap 時走既有 early return，不輸出上述 INFO。

#### 3.2 隊列起始、進度心跳、完成（`estimator_king/crawler/async_pipeline.py` `async_process_queue`）

新增模組常數 `_PROGRESS_LOG_EVERY = 20`（每處理 20 個輸出一次心跳）。

- **起始**：在取得 `entries` 且非空後（`if not entries: return` 之後），輸出一筆
  INFO：`"store=%s queue: %d entries to process"`，參數為 `store_id`、`len(entries)`。
- **心跳**：在 `_handle` 成功路徑既有的 `async with lock:` 區塊內，`result.processed`
  累加之後，判斷 `if result.processed % _PROGRESS_LOG_EVERY == 0:` 則輸出一筆 INFO：
  `"store=%s progress: %d/%d processed"`，參數為 `store_id`、`result.processed`、
  `len(entries)`。利用既有 lock，不另開鎖。
- **完成**：在 `await asyncio.gather(...)` 之後、`return result` 之前，輸出一筆 INFO：
  `"store=%s done: created=%d updated=%d skipped=%d failed=%d"`，參數為 `store_id`、
  `result.created`、`result.updated`、`result.sync_skipped`、`result.failed`。

#### 3.3 既有 crawler INFO 保留

`crawler/cycle.py` 既有的 `"Processing store %s"`（每 store 開始）與
`bot/scheduler.py` 的 `"Crawl cycle complete: %s"` 保留不動，作為 store banner 與
全輪總結。

### 4. BOT INFO/DEBUG：使用者請求處理進度

#### 4.1 estimator（`estimator_king/bot/estimator.py` `Estimator`）

- `estimate_products`：
  - 既有 INFO `"estimate request from %s for %d products"`（第 53 行）保留。
  - 進入處理前以 `time.monotonic()` 記錄起始時間；在 return 前輸出一筆 INFO：
    `"estimate done for %s: %d estimates in %.1fs"`，參數為 `user_id`、
    `len(all_estimates)`、耗時秒數。
- `_estimate_chunk`：因不含 chunk 序號，改在 `estimate_products` 的 chunk 迴圈內
  輸出 DEBUG。在迴圈中計算 chunk 序號（`start // CHUNK_SIZE + 1`）與總 chunk 數
  （`(len(product_names) + CHUNK_SIZE - 1) // CHUNK_SIZE`），於呼叫 `_estimate_chunk`
  前輸出 DEBUG：`"chunk %d/%d: %d products"`，參數為 chunk 序號、總 chunk 數、
  `len(chunk)`。

需新增 `import time`（既有 `import logging` 與模組 logger 已存在）。

#### 4.2 commands（`estimator_king/bot/commands.py` `ProductInputModal.on_submit`）

新增模組 logger `logger = logging.getLogger(__name__)`（需新增 `import logging`）。
在 `on_submit` 中：

- 解析 `product_list` 後輸出 DEBUG：`"modal submitted by %s: %d products parsed"`，
  參數為 `interaction.user.id`、`len(product_list)`。
- 驗證失敗時輸出 WARNING：
  - 數量 < 1：`"validation failed for %s: empty input"`，參數為 `interaction.user.id`。
  - 數量 > MAX_PRODUCTS：`"validation failed for %s: %d products exceeds max %d"`，
    參數為 `interaction.user.id`、`len(product_list)`、`MAX_PRODUCTS`。
- 既有的 `except EstimationError as e:` 區塊新增 ERROR log：
  `"estimation failed for %s: %s"`，參數為 `interaction.user.id`、`e`；
  既有回傳使用者訊息的行為保留不動。
- 既有的 `except Exception as e:` 區塊新增 `logger.exception`：
  `"unexpected error handling request from %s"`，參數為 `interaction.user.id`；
  既有回傳使用者訊息的行為保留不動。

`user_id` 的呈現：commands 層 log 使用 `interaction.user.id`（原始數字 ID）；
estimator 層 log 沿用既有 `discord-{id}` 格式的 `user_id` 字串（呼叫端在
`commands.py` 既有的 `user_id = f"discord-{interaction.user.id}"` 組出後傳入）。
兩層 ID 可由數字部分對應，不需統一格式。

## 測試策略

使用 pytest 的 `caplog` fixture 驗證關鍵 log 被發出且等級正確。新增/調整測試覆蓋：

1. **格式與 root logger 收斂**：
   - 驗證 `bot/runner.py`、`sync/engine.py`、`crawler/html_extractor.py` 發出的
     log 其 `record.name` 為對應模組路徑（`estimator_king.bot.runner` 等），而非
     `root`。
2. **DEBUG 請求記錄**（在 `caplog.set_level(logging.DEBUG)` 下）：
   - 爬蟲非同步 client：對成功與錯誤（如 4xx）狀態各斷言有一筆含 url 與狀態碼的
     DEBUG 記錄（沿用既有 async_http_client 測試的 mock 方式）。
   - 爬蟲同步 client：同上，斷言 method/url/status 出現。
   - embedding：斷言呼叫後有一筆含 `inputs` 數量與 model 的 DEBUG。
   - chat：斷言成功與失敗（EstimationError）路徑各有一筆 DEBUG（驗證 finally 行為）。
3. **CRAWLER 進度 INFO**：
   - `populate_queue_from_sitemap`：斷言發出 `sitemap: N total, M new enqueued`，
     且空 sitemap 時走 WARNING、不發此 INFO。
   - `async_process_queue`：以超過 `_PROGRESS_LOG_EVERY` 筆 entries 的情境，斷言
     發出起始、至少一筆心跳、與完成統計三類 INFO。
4. **BOT 進度**：
   - `Estimator.estimate_products`：斷言發出完成 INFO 與每 chunk 的 DEBUG。
   - `ProductInputModal.on_submit`：以 mock interaction 斷言驗證失敗發出 WARNING、
     EstimationError 發出 ERROR。

驗證準則（CLAUDE.md 強制）：實作完成後須通過 `pyright`（type check）、`ruff`
（lint）、以及上述 pytest 相關測試，全部通過才算完成。

## 風險與緩解

- **DEBUG log 量大**：請求層 DEBUG 在預設 INFO 等級下不會輸出，僅在 `--log-level
  DEBUG` 時啟用，對正常運行無影響。
- **心跳鎖競爭**：心跳判斷複用既有 `async with lock` 區塊，不引入新鎖，無額外競爭。
- **行為不變保證**：所有變更僅新增 log 述句，不修改回傳值、例外傳遞或控制流；既有
  測試應全數維持通過。
