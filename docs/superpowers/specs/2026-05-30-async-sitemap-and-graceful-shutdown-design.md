# 設計規格：Async sitemap 遷移 + proxy 支援 + 死碼清除 + 優雅關閉

- 日期：2026-05-30
- 狀態：設計確認，待寫實現計畫

## 1. 背景與問題

Discord bot 與 in-process 爬蟲排程器共用同一個 asyncio event loop。`CrawlScheduler.run_forever()` 透過 `asyncio.create_task()` 掛在 bot 的 loop 上。爬蟲一個 cycle 的 sitemap 列舉階段是**同步阻塞**的：

- `cycle.run_crawl_cycle` 呼叫 `populate_queue_from_sitemap`（同步）
- 其內 `SitemapEnumerator.enumerate_products` 走同步 `HTTPClient.get`（`requests` + tenacity 阻塞重試）
- 速率限制 `DomainRateLimiter.wait()` 使用 `time.sleep`

當這段同步 HTTP 在 event loop 上跑超過約 60 秒（慢回應、重試、rate-limit sleep 疊加），Discord gateway 心跳送不出去，discord.py 發出 `heartbeat blocked` 警告，嚴重時連線被當殭屍斷掉。

連帶問題：Ctrl+C 不會立即停止。`loop.add_signal_handler` 註冊的 callback 必須在 loop 上執行，但 loop 被同步 sitemap 卡死時 callback 排不進去。

### 已確認的程式現況

- `async_process_queue`（商品抓取階段）**已經是 loop 友善**：使用 `AsyncHTTPClient`（aiohttp + `asyncio.sleep`），並把 `fetch_product`／`sync_products` 以 `asyncio.to_thread` offload。它不會卡心跳。**本規格不改其核心並發邏輯**（僅為 proxy 多傳一個參數）。
- 真正阻塞 loop 的只有 sitemap 列舉這一段。
- 同步 `HTTPClient` 的唯一 production 實例化點是 `cycle.py` 為了 sitemap 而建立；`shopify.py` 對 `HTTPClient` 只是型別註解，執行期實際拿到的是 `_AsyncToSyncHTTPAdapter`（鴨子型別）。
- `config.proxy` 目前只接到 `cycle.py` 的同步 `HTTPClient`（即 sitemap 路徑）。`AsyncHTTPClient` 沒有 proxy 支援，所以商品抓取路徑現在就忽略 proxy。`stores_config.yaml` 的 proxy 預設 `enabled: false`。

## 2. 解決方向（已選定）

採方案 C：把 sitemap 列舉改寫為 async，走既有 `AsyncHTTPClient`，使整個 crawl cycle 變成 loop-native 純 async。配套：

1. AsyncHTTPClient 加上 proxy 支援，讓 sitemap 與商品抓取一致地支援 proxy。
2. 移除因遷移而變成 runtime 死碼的同步 `HTTPClient` 整檔及其測試。
3. 優雅關閉：signal handler 取消排程器、關閉 bot；第二次 Ctrl+C 強制退出作為逃生口。

選 C 而非「offload 同步段到 thread（B）」或「整個 cycle 丟 thread（A）」的理由：C 讓 cancellation 能乾淨穿透所有 `await`（aiohttp 請求、`asyncio.sleep` 都會即時中止），優雅關閉最單純；並收斂「同步 + 非同步」兩套 HTTP stack 的技術債。

## 3. 範圍與設計細節

### 3.1 Async sitemap 遷移

**`estimator_king/crawler/sitemap.py`**

- `SitemapEnumerator.__init__(self, http_client: AsyncHTTPClient)`：改為必填參數，移除 `Optional[HTTPClient]` 與 `http_client or HTTPClient()` 預設。
- `enumerate_products`、`_extract_products_sitemaps`、`_extract_product_urls` 全部改為 `async def`，內部 `await self.http_client.get(url)`。
- `AsyncHTTPClient.get()` 回傳 `str`，因此：
  - XML 解析改為 `ET.fromstring(text)`（傳入 `str`）。
  - **移除** `resp.content` 與 `resp.raise_for_status()` 的使用（`AsyncHTTPClient` 內部已對 403/430/429/4xx/5xx 主動 raise）。
- except 子句：把**三處**對 `HTTPClientError` 的捕捉全部改為 `AsyncHTTPClientError`（從 `estimator_king.crawler.async_http_client` import）：
  - `enumerate_products` 的 `except (ET.ParseError, HTTPClientError)`（現 sitemap.py:68）
  - `_extract_products_sitemaps` 的 `except HTTPClientError`（現 sitemap.py:92）
  - `_extract_product_urls` 的 `except HTTPClientError`（現 sitemap.py:129）
  其中 `ET.ParseError` 的捕捉維持。內層兩處仍把錯誤包成 `SitemapParseError`（`SitemapError` 子類），外層 `enumerate_products` 包成 `SitemapError`，語意不變。
- 各 products sitemap 維持**循序**抓取（不引入 sitemap 層級的並發 gather，縮小改動面；rate limiter 本就負責 pacing）。
- 保留 `SitemapError`、`SitemapParseError` 類別與既有錯誤包裝語意。

**`estimator_king/crawler/pipeline.py`**

- `populate_queue_from_sitemap` 改為 `async def`，內部 `sitemap_urls = await enumerator.enumerate_products(store.base_url)`。其餘 repo 同步呼叫（`get_by_product_url`、`enqueue_url`、`record_sitemap_seen`、`list_active`、`increment_sitemap_miss`）維持同步，不變。
- `enqueue_oldest_products` 維持同步，不變。

**`estimator_king/crawler/cycle.py`**

- 移除 `from estimator_king.crawler.http_client import HTTPClient`。
- 改為在 store 迴圈外建立 async client：
  ```python
  async with AsyncHTTPClient(config.crawler, proxy=config.proxy) as sitemap_client:
      enumerator = SitemapEnumerator(http_client=sitemap_client)
      for store in config.stores:
          ...
          new_count = await populate_queue_from_sitemap(store, repo, enumerator)
  ```
- 一個 `AsyncHTTPClient` 共用於整個 cycle 的所有 store sitemap（沿用目前「一個 client 跑所有 sitemap」語意，使 per-domain rate limiter / circuit breaker 狀態跨 store 延續）。
- `async_process_queue` 呼叫維持，但需把 `config.proxy` 傳入（見 3.2）。

### 3.2 AsyncHTTPClient proxy 支援

**`estimator_king/crawler/async_http_client.py`**

- `AsyncHTTPClient.__init__` 新增參數 `proxy: ProxyConfig | None = None`（從 `..config_schema` import `ProxyConfig`）；存為 `self._proxy = proxy or ProxyConfig()`。
- `_request_once` 在呼叫 `session.request("GET", url, ...)` 時，依目標 URL 的 scheme 決定要用哪個 proxy 設定值：
  - http URL → 用 `self._proxy.http_proxy`
  - https URL → 用 `self._proxy.https_proxy`
  - 僅在 `self._proxy.enabled` 且選中的值為非空字串時才走 proxy；否則不傳任何 proxy 參數（直連）。
- **帳密處理（已對 aiohttp 3.13.5 原始碼查證，見 §6）**：aiohttp 對「顯式傳入」的 proxy URL **不會**自動拆出 userinfo 帳密（只有 `trust_env=True` 的 env 來源才會拆）。而 `ProxyConfig` 沒有獨立 auth 欄位，使用者可能把帳密寫進 URL（`http://user:pass@host:port`）。因此 `AsyncHTTPClient` 必須自己拆解：
  - 以 `strip_auth_from_url(URL(value))` 把選中的 proxy 值拆成 `(stripped_url, auth)`（`auth` 為 `BasicAuth | None`）。
  - 呼叫 `session.request(..., proxy=stripped_url, proxy_auth=auth)`；`auth` 為 `None` 時等同不帶認證。
  - `stripped_url` 型別為 `yarl.URL`（aiohttp 的 `proxy` 接受 `str | yarl.URL`）。
  - **import 來源（明示，避免猜測）**：實作端 `from aiohttp.helpers import strip_auth_from_url`、`from yarl import URL`；`BasicAuth` 由 `strip_auth_from_url` 回傳、實作端不自行建構。測試端若要建構/比對 `BasicAuth`，用 `from aiohttp import BasicAuth`。
- **scheme 限制（已查證）**：aiohttp 只支援 `http://`（與 socks5）proxy；`https://` proxy 會被忽略並警告。即使目標 URL 是 https，`http_proxy`/`https_proxy` 兩個設定值都應填 `http://` 形式的 proxy（aiohttp 會走 CONNECT 隧道）。本規格不對設定值做 scheme 驗證/轉換，沿用設定原值；此限制於 spec 與測試註記即可。

**`estimator_king/crawler/async_pipeline.py`**

- `async_process_queue` 新增參數，把 proxy 傳入它建立的 `AsyncHTTPClient`，使商品抓取也走 proxy。
  - 簽名新增 `proxy: ProxyConfig | None = None`（放在現有參數之後，避免破壞既有呼叫；或以 keyword-only 形式）。
  - `async with AsyncHTTPClient(policy, proxy=proxy) as client:`。
- `cycle.run_crawl_cycle` 呼叫 `async_process_queue` 時傳入 `config.proxy`。
- 此舉順帶修正「sitemap 走 proxy、商品抓取忽略 proxy」的既有不一致：遷移後兩條路徑都一致支援 proxy。

### 3.3 死碼清除

- **刪除整個檔案 `estimator_king/crawler/http_client.py`**：含同步 `HTTPClient`、`DomainRateLimiter`、`DomainCircuitBreaker`、`_parse_retry_after`、`_wait_http`、`_domain_from_url`，以及同步錯誤類別 `HTTPClientError`、`RateLimitError`、`ServerError`、`WAFBlockedError`、`CircuitBreakerOpenError`。遷移後無任何 production import。
- **刪除測試 `tests/test_http_client.py`、`tests/test_http_client_logging.py`**。其涵蓋的 rate-limit、circuit breaker、retry、debug log 行為已由 `tests/test_async_http_client.py`、`tests/test_async_http_client_logging.py` 對等覆蓋，無覆蓋損失。
- **`estimator_king/crawler/shopify.py`**：
  - 移除 `from .http_client import HTTPClient`。
  - `fetch_product` 的 `http_client` 參數型別改為本地定義的 `Protocol`，描述執行期實際被傳入的最小介面：一個 `get(url: str)` 方法，回傳具有 `status_code`（int 屬性）與 `text`（str 屬性）的物件。
    - 定義方式：在 shopify.py 內定義兩個 `typing.Protocol`——回應物件 Protocol（`status_code: int`、`text: str`）與 getter Protocol（`def get(self, url: str) -> <回應 Protocol>: ...`）。
    - `fetch_product` 內以 `getattr` 取用 `.status_code`／`.text` 的既有寫法維持。
  - `_raise_for_status` 的參數型別由 `requests.Response` 放寬為前述回應 Protocol。
  - 移除僅為型別註解而存在的 `import requests`。
- 需確認 `estimator_king/crawler/__init__.py`（及任何套件層級 re-export）沒有對 `http_client` 的匯出；若有則一併移除。

### 3.4 優雅關閉 + 第二次強退

**`estimator_king/bot/runner.py`**

- 維持以 `loop.add_signal_handler` 為 SIGINT/SIGTERM 註冊處理常式，但改為兩段式：
  - 以一個可變的關閉狀態旗標（例如 closure 內的 `list`/小物件，或 `asyncio.Event` 搭配旗標）追蹤是否已在關閉中。
  - 第一次訊號：設旗標 → 建立 `shutdown()` task（強引用存入既有 `_background_tasks`）。記錄一行 log 提示「再按一次 Ctrl+C 強制退出」。
  - 第二次訊號（旗標已設）：`os._exit(130)` 立即強制退出。
- `shutdown()` 改為：
  1. `scheduler_task.cancel()`
  2. `await scheduler_task`，以 `try/except asyncio.CancelledError: pass` 吞掉取消例外
  3. `await bot.close()`
- `scheduler_task` 在 `shutdown()` closure 可見（於 `run_bot` 內先行定義）。
- 強退（`os._exit(130)`）的權衡：跳過 DB flush、log flush 等清理。這是刻意的逃生口語意——用於優雅關閉因 in-flight `to_thread` 卡住而無法即時完成的情況。

**強退必要性的根因**：`async_pipeline._AsyncToSyncHTTPAdapter.get` 在 worker thread 內以 `asyncio.run_coroutine_threadsafe(coro, loop).result()` 阻塞等待 loop 執行 coroutine。關閉時若該 coroutine 尚未完成而 loop 即將停止，`.result()` 可能無限等待，使 thread 卡住、`asyncio.run` 的 executor 收尾也卡住。第二次 Ctrl+C 強退即為此設計。

**`estimator_king/bot/scheduler.py`**

- 確認並維持：`run_once` 的 `except Exception` 不會吃掉 `asyncio.CancelledError`（後者繼承 `BaseException`，不被 `except Exception` 捕捉），使取消能乾淨往外傳遞，`run_forever` 隨之結束。`finally` 內 `self._running = False` 維持。
- 不需要額外的合作式停止旗標——全 async 後 cancellation 即足夠。

### 3.5 行為變更（明確記錄）

- **sitemap 4xx 回應**：原同步路徑以 `requests` 的 `resp.raise_for_status()` 處理（且其拋出的 `requests.HTTPError` 並非 `HTTPClientError`，捕捉行為不一致）。遷移後，4xx 由 `AsyncHTTPClient` 統一 raise `ClientError`（`AsyncHTTPClientError` 子類）；在 `_extract_*` 內層先被包成 `SitemapParseError`（`SitemapError` 子類），最終以 `SitemapError` 形式由 `cycle.run_crawl_cycle` 以 per-store error 計數處理（`counters["errors"] += 1` 並 `continue`）。此為行為改善（更一致的錯誤處理），需在實作與測試中明確涵蓋。
- **proxy 一致化**：遷移後商品抓取也支援 proxy（先前忽略）。proxy 預設 `enabled: false`，預設情境行為不變（皆直連）。

## 4. 受影響檔案清單

production：
- `estimator_king/crawler/sitemap.py`（改 async + 換 client/錯誤型別）
- `estimator_king/crawler/pipeline.py`（`populate_queue_from_sitemap` 改 async）
- `estimator_king/crawler/cycle.py`（async client、傳 proxy、移除同步 import）
- `estimator_king/crawler/async_http_client.py`（加 proxy 支援）
- `estimator_king/crawler/async_pipeline.py`（傳 proxy）
- `estimator_king/crawler/shopify.py`（改 Protocol、移除 requests/HTTPClient import）
- `estimator_king/crawler/http_client.py`（**刪除**）
- `estimator_king/bot/runner.py`（兩段式關閉、cancel scheduler、第二次強退）
- `estimator_king/bot/scheduler.py`（確認 cancellation 穿透；多半不需改碼）
- `estimator_king/crawler/__init__.py`（若有 http_client re-export 則移除）

tests：
- `tests/test_sitemap.py`（改 async via `asyncio.run`，mock async get；fake `get` 直接回傳 XML 字串）
- `tests/test_pipeline.py`（`populate_queue_from_sitemap` 測試改 async；其 `FakeEnumerator.enumerate_products` 須改為 `async def`，且測試以 `asyncio.run(populate_queue_from_sitemap(...))` 呼叫，否則 `await` 同步回傳值會 `TypeError`）
- `tests/test_crawl_cycle.py`（驗證 cycle 在 async sitemap 下正常；現有 `patch("...cycle.populate_queue_from_sitemap", return_value=0)` 須改為 awaitable mock——例如 `new=AsyncMock(return_value=0)` 或 `side_effect` 為 async 函式，比照同檔 `async_process_queue` 的 `fake_proc` 寫法，否則 `await 0` 會 `TypeError`）
- `tests/test_async_http_client.py`（新增 proxy 測試；須擴充該檔 `_FakeSession.request`/`request_factory`，使其捕獲並暴露 `proxy`/`proxy_auth` 等 kwargs——現有 factory 只接 `(method, url)` 並把 `**kwargs` 吞掉，無法觀察 proxy 參數。測試案例須含：scheme 選值、enabled 開關、及帳密拆解 `BasicAuth`，詳見 §5）
- `tests/test_http_client.py`（**刪除**）
- `tests/test_http_client_logging.py`（**刪除**）
- 關閉行為測試（新增或擴充 `tests/test_scheduler.py` / runner 相關）：scheduler 被 cancel 後乾淨退出；第一次訊號觸發 cancel + close、第二次觸發強退路徑（強退以可注入的 exit 函式測試，避免測試真的呼叫 `os._exit`）。

## 5. 測試策略

- **本專案兩種 async 測試慣例並存，依目標檔沿用該檔既有風格**（不可一概而論）：
  - `tests/test_sitemap.py`、`tests/test_pipeline.py`、`tests/test_crawl_cycle.py`：沿用 `asyncio.run(...)` 在同步 `def test_...` 內呼叫（比照 `tests/test_async_pipeline.py`、`tests/test_crawl_cycle.py`）。
  - `tests/test_async_http_client.py`、`tests/test_scheduler.py`：沿用該檔既有的 `@pytest.mark.asyncio` + `async def test_...`（兩檔現皆採此寫法）。
- sitemap/pipeline 測試的 HTTP mock：以提供 `async def get(self, url) -> str` 的 fake 物件（或 `unittest.mock.AsyncMock` 設定 `return_value` 為 XML 字串）取代原本回傳 `requests.Response`（具 `.content`/`.raise_for_status`）的 `MagicMock`。**fake 的 `get` 直接回傳 XML 字串本身**，不再包裝任何具 `.content`/`.raise_for_status` 的物件（因 §3.1 已移除這兩者的使用）。沿用 `tests/fixtures/*.xml`（以字串讀入）。
- proxy 測試：擴充 `tests/test_async_http_client.py` 既有的 `_FakeSession`/`request_factory` 使其捕獲 `proxy`/`proxy_auth` 等 kwargs（現有 factory 只收 `(method, url)` 並把 `**kwargs` 吞掉，無法觀察 proxy）。須涵蓋：
  - **scheme 選值**：http 目標用 `http_proxy`、https 目標用 `https_proxy`。
  - **enabled 開關**：`enabled: false` 時完全不帶 proxy（直連）；`enabled: true` 但選中值為空字串時也不帶 proxy。
  - **帳密拆解**：設定值含 userinfo（如 `http://user:pass@host:8080`）時，捕獲到的 `proxy` 為已移除帳密的 stripped URL、`proxy_auth` 為 `BasicAuth("user", "pass")`（`from aiohttp import BasicAuth`）；設定值不含 userinfo 時 `proxy_auth` 為 `None`。
- 4xx 行為測試：mock async get 對 sitemap URL 拋 `ClientError`，斷言 `enumerate_products` 包成 `SitemapError`，且 `run_crawl_cycle` 計入 per-store error 並 continue。
- 關閉行為測試：以可注入的「強退函式」替代 `os._exit`，驗證第二次訊號走強退分支；驗證 `shutdown()` 會 cancel scheduler task 並 await、再 close bot。

## 6. 第三方套件查證（research）

依專案規則，以下行為須在撰寫實現計畫前查證，不得在 plan 中以「事前查證」型 Task 呈現。

### 6.1 aiohttp 3.13.5 proxy API — 已查證（讀實際安裝原始碼）

- **參數簽名**（`client.py` `_request`）：`proxy: Optional[StrOrURL] = None`、`proxy_auth: Optional[BasicAuth] = None`、`proxy_headers: Optional[LooseHeaders] = None`。`BasicAuth(login: str, password: str = "", encoding: str = "latin1")`（`helpers.py`）。
- **顯式 proxy URL 內含帳密**：**不會**被自動拆解。`client.py:580-587` 對顯式 `proxy` 僅做 `URL(proxy)`，不呼叫 `strip_auth_from_url`；自動拆解只發生在 `trust_env=True` 的 env 來源（`helpers.py:281` `proxies_from_env`）。→ 結論：本專案需自行用 `aiohttp.helpers.strip_auth_from_url(yarl.URL(value))` 拆成 `(stripped_url, BasicAuth|None)`，再分別傳 `proxy=` 與 `proxy_auth=`（見 §3.2）。
- **scheme 限制**：只支援 `http://`（與 socks5）proxy；`https://`/`wss` proxy 被忽略並 warning（`helpers.py:285-289`）。https 目標 URL 也用 http proxy（走 CONNECT）。
- **trust_env**：預設 `False`（`client.py`）。本專案走顯式 proxy，與 env 無關；config 載入時已自行把 `HTTP_PROXY`/`HTTPS_PROXY` 併入設定值。

### 6.2 ElementTree 與 tenacity — 已查證（實際執行驗證）

1. **`xml.etree.ElementTree.fromstring(str)` 含 encoding 宣告**：在本專案 Python 3.14.3 上**可行**。以含 `<?xml version="1.0" encoding="UTF-8"?>` 的 `str` 直接 `ET.fromstring(text)` 不報錯；真實 fixtures（`tests/fixtures/sitemap_index.xml`、`sitemap_products_1.xml`，皆帶 UTF-8 宣告）以 decode 後字串解析成功。→ §3.1 直接 `ET.fromstring(text)`（`text: str`）安全，無需 `.encode()` 或 strip 宣告。
2. **tenacity 9.1.4 在 async 上對 `CancelledError`**：**不重試、正常往外傳**。`retry_if_exception_type((RateLimitError, ServerError))` 以 `isinstance` 比對（`tenacity/retry.py:87-98`），`CancelledError` 不在其中故不觸發重試；`reraise=True` 下原例外正常拋出（`tenacity/asyncio/__init__.py:119`、`tenacity/__init__.py:391-393`）。實測 cancel 後僅 1 次 attempt、`CancelledError` 正常傳播。→ §3.1 cancellation 穿透 retry wrapper 安全，無需特殊處理。

研究產出已直接反映於 §3.1、§3.2 的設計描述；實現計畫各 Task 將沿用這些確認結果。

## 7. 非目標（Out of Scope）

- 不重寫 `async_process_queue` 的並發/offload 架構（僅多傳 proxy 參數）。
- 不改 `shopify.py` 的抓取邏輯（僅換型別註解與移除 requests/HTTPClient import）。
- 不為 sitemap 引入跨 sitemap 的並發抓取。
- 不調整 `CrawlerPolicy`／`ProxyConfig` 的 schema 欄位（proxy 保留現有欄位）。
- 不更動 CLI `crawl` 子指令的對外行為（它經 `asyncio.run(run_crawl_cycle(...))`，自動受惠於 async sitemap）。
