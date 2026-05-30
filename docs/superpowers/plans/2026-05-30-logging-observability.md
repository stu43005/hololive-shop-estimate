# Logging 觀察性調整 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 estimator_king 的 log 能區分 BOT/CRAWLER 子系統、在 DEBUG 記錄所有對外請求、並在 CRAWLER 與 BOT 的關鍵位置補上進度與數量記錄。

**Architecture:** 純加法式的 logging 調整——在 `logging.basicConfig` 格式加入 `%(name)s`、把仍用 root logger 的模組收斂為 `logging.getLogger(__name__)`，並在四個對外請求點與爬蟲/機器人關鍵流程插入 log 述句。不改變任何回傳值、例外傳遞或控制流。

**Tech Stack:** Python 3.14、標準庫 `logging`、`pytest` + `pytest-asyncio`（既有 `@pytest.mark.asyncio`）、`caplog` fixture、`unittest.mock`、`pyright`、`ruff`。

**驗證工具指令（全程沿用）：**
- 單一測試：`.venv/bin/python -m pytest <path>::<test> -v -p no:cov`
- 型別檢查：`.venv/bin/python -m pyright <file>`
- Lint：`.venv/bin/ruff check <file>`

> 註：`pytest.ini` 預設 `addopts` 帶 `--cov`，逐測試執行時加 `-p no:cov` 可避免覆蓋率雜訊；最終驗證（Task 13）會跑完整含 cov 的測試。

---

## 共用測試備註（caplog）

- `caplog` 會從各模組 logger 傳遞到 root 並被攔截；用 `caplog.set_level(logging.DEBUG)`（或 `logging.INFO`）設定攔截等級。
- 斷言時用 `record.name`（logger 名稱）、`record.levelno`/`record.levelname`、`record.getMessage()`（格式化後字串）。
- 非同步測試沿用既有 `@pytest.mark.asyncio` 標記。

---

## Task 1: `__main__.py` — 格式常數 + 模組 logger 收斂

**Files:**
- Modify: `estimator_king/__main__.py`
- Test: `tests/test_main_logging.py`（Create）

實作要點：
- 新增模組層常數 `_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"`，並讓 `_main()` 的 `logging.basicConfig(format=_LOG_FORMAT, ...)` 引用它（`level`、`stream=sys.stderr` 不變）。
- 在模組頂層（import 之後）新增 `logger = logging.getLogger(__name__)`。
- 將 `run_crawl` 內 3 處 `logging.error(...)`（config 載入失敗、缺 embedding key、cycle 例外）與 `run_bot` 內 1 處 `logging.info("Bot stopped by user")` 改為 `logger.error(...)` / `logger.info(...)`。

- [ ] **Step 1: 撰寫失敗測試**

建立 `tests/test_main_logging.py`：

```python
import logging

import pytest

import estimator_king.__main__ as cli


def test_log_format_includes_logger_name():
    assert cli._LOG_FORMAT == "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def test_run_crawl_config_failure_logs_under_module_logger(monkeypatch, caplog):
    def boom(_path):
        raise RuntimeError("bad config")

    monkeypatch.setattr(
        "estimator_king.__main__.AppConfig.from_yaml", staticmethod(boom)
    )
    args = type("A", (), {"config": "x.yaml", "db": None, "force_refetch": False})()

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit):
            cli.run_crawl(args)

    recs = [r for r in caplog.records if r.name == "estimator_king.__main__"]
    assert recs and recs[0].levelno == logging.ERROR
    assert "Failed to load config" in recs[0].getMessage()
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_main_logging.py -v -p no:cov`
Expected: FAIL（`AttributeError: module ... has no attribute '_LOG_FORMAT'`，以及記錄 name 為 `root` 而非模組路徑）

- [ ] **Step 3: 實作**

在 `estimator_king/__main__.py` 中：

```python
# 於 import 區塊之後新增：
logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
```

`run_crawl` 內三處改為：

```python
        logger.error("Failed to load config from %s: %s", args.config, e)
```
```python
        logger.error("OPENAI_API_KEY (or EMBEDDING_API_KEY) is required")
```
```python
        logger.error("Crawler failed: %s", e)
```

`run_bot` 內：

```python
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
```

`_main()` 中：

```python
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format=_LOG_FORMAT,
        stream=sys.stderr,
    )
```

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_main_logging.py -v -p no:cov`
Expected: PASS（2 passed）

- [ ] **Step 5: 型別與 lint**

Run: `.venv/bin/python -m pyright estimator_king/__main__.py tests/test_main_logging.py && .venv/bin/ruff check estimator_king/__main__.py tests/test_main_logging.py`
Expected: 0 errors

- [ ] **Step 6: Commit**

```bash
git add estimator_king/__main__.py tests/test_main_logging.py
git commit -m "feat(log): add logger name to format and converge __main__ to module logger"
```

---

## Task 2: `bot/runner.py` — 模組 logger 收斂（BOT 生命週期）

**Files:**
- Modify: `estimator_king/bot/runner.py`
- Test: `tests/test_runner_logging.py`（Create）

實作要點：在模組頂層新增 `logger = logging.getLogger(__name__)`，將 `run_bot` 內巢狀的 `on_ready`、`shutdown` 共 5 處 `logging.info(...)` 改為 `logger.info(...)`。`run_bot` 需要實際啟動 Discord client，無法在單元測試中觸發；測試僅驗證模組 logger 存在且名稱正確（足以證明 `getLogger(__name__)` 已加入）。

- [ ] **Step 1: 撰寫失敗測試**

建立 `tests/test_runner_logging.py`：

```python
from estimator_king.bot import runner


def test_runner_has_module_logger_with_qualified_name():
    assert runner.logger.name == "estimator_king.bot.runner"
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_runner_logging.py -v -p no:cov`
Expected: FAIL（`AttributeError: module 'estimator_king.bot.runner' has no attribute 'logger'`）

- [ ] **Step 3: 實作**

在 `estimator_king/bot/runner.py` import 區塊後新增：

```python
logger = logging.getLogger(__name__)
```

將 5 處呼叫改為 `logger.info`：

```python
        logger.info(f"Logged in as {bot.user}")
```
```python
            logger.info(f"Synced commands to guild {guild_id}")
```
```python
            logger.info("Synced commands globally")
```
```python
        logger.info("Bot ready and commands synchronized")
```
```python
        logger.info("Shutting down bot...")
```

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_runner_logging.py -v -p no:cov`
Expected: PASS（1 passed）

- [ ] **Step 5: 型別與 lint**

Run: `.venv/bin/python -m pyright estimator_king/bot/runner.py tests/test_runner_logging.py && .venv/bin/ruff check estimator_king/bot/runner.py tests/test_runner_logging.py`
Expected: 0 errors

- [ ] **Step 6: Commit**

```bash
git add estimator_king/bot/runner.py tests/test_runner_logging.py
git commit -m "feat(log): converge bot runner lifecycle logs to module logger"
```

---

## Task 3: `sync/engine.py` — 模組 logger 收斂

**Files:**
- Modify: `estimator_king/sync/engine.py`
- Test: `tests/test_sync_engine_logging.py`（Create）

實作要點：在模組頂層新增 `logger = logging.getLogger(__name__)`，將 `sync_products` 失敗路徑的 `logging.exception("Sync failed for %s", external_key)` 改為 `logger.exception(...)`。測試透過讓 embedder 拋例外觸發該路徑，斷言記錄 name 為 `estimator_king.sync.engine`。

- [ ] **Step 1: 撰寫失敗測試**

建立 `tests/test_sync_engine_logging.py`：

```python
import logging

from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.database.repository import ProductStateRepository
from estimator_king.sync.engine import sync_products


class BoomEmbedder:
    def embed_documents(self, texts):
        raise RuntimeError("embed failed")


class FakeVectorStore:
    def upsert(self, id, document, embedding, metadata):
        pass


def _snap():
    return ProductSnapshot(
        product_id=1, title="T", description="d",
        variants=[ProductVariant(1, "S", "2000")], html_details={},
    )


def test_sync_failure_logs_under_module_logger(caplog):
    with ProductStateRepository(":memory:") as repo:
        with caplog.at_level(logging.ERROR):
            result = sync_products(
                [_snap()], "hololive", "https://x", repo,
                BoomEmbedder(), FakeVectorStore(),
            )

    assert result.failed == 1
    recs = [r for r in caplog.records if r.name == "estimator_king.sync.engine"]
    assert recs and recs[0].levelno == logging.ERROR
    assert "Sync failed for hololive:1" in recs[0].getMessage()
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_sync_engine_logging.py -v -p no:cov`
Expected: FAIL（記錄 name 為 `root`，篩選後 `recs` 為空）

- [ ] **Step 3: 實作**

在 `estimator_king/sync/engine.py` import 區塊後新增：

```python
logger = logging.getLogger(__name__)
```

將失敗路徑改為：

```python
        except Exception:  # embedding/vector failure: fire-and-forget
            logger.exception("Sync failed for %s", external_key)
```

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_sync_engine_logging.py -v -p no:cov`
Expected: PASS（1 passed）

- [ ] **Step 5: 型別與 lint**

Run: `.venv/bin/python -m pyright estimator_king/sync/engine.py tests/test_sync_engine_logging.py && .venv/bin/ruff check estimator_king/sync/engine.py tests/test_sync_engine_logging.py`
Expected: 0 errors

- [ ] **Step 6: Commit**

```bash
git add estimator_king/sync/engine.py tests/test_sync_engine_logging.py
git commit -m "feat(log): converge sync engine failure log to module logger"
```

---

## Task 4: `crawler/html_extractor.py` — 模組 logger 收斂

**Files:**
- Modify: `estimator_king/crawler/html_extractor.py`
- Test: `tests/test_html_extractor_logging.py`（Create）

實作要點：在模組頂層（import 區塊後）新增 `import logging` 與 `logger = logging.getLogger(__name__)`；移除 `extract_detail_sections` 內 `if not blocks_by_key:` 區塊中的區域 `import logging`，並把 `logging.debug(...)` 改為 `logger.debug(...)`。`extract_detail_sections("")` 會走 no-blocks 路徑觸發該 DEBUG。

- [ ] **Step 1: 撰寫失敗測試**

建立 `tests/test_html_extractor_logging.py`：

```python
import logging

from estimator_king.crawler.html_extractor import extract_detail_sections


def test_no_blocks_debug_logs_under_module_logger(caplog):
    with caplog.at_level(logging.DEBUG):
        out = extract_detail_sections("")

    assert out == {}
    recs = [
        r for r in caplog.records
        if r.name == "estimator_king.crawler.html_extractor"
    ]
    assert recs and recs[0].levelno == logging.DEBUG
    assert "No blocks found" in recs[0].getMessage()
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_html_extractor_logging.py -v -p no:cov`
Expected: FAIL（記錄 name 為 `root`，篩選後 `recs` 為空）

- [ ] **Step 3: 實作**

在 `estimator_king/crawler/html_extractor.py` 檔案頂層 import 區塊後新增：

```python
import logging

logger = logging.getLogger(__name__)
```

（`import logging` 若頂層尚未匯入則新增；放在既有其他 import 之後。）

將 no-blocks 區塊改為（移除區域 `import logging`）：

```python
    if not blocks_by_key:
        logger.debug(
            f"extract_detail_sections: No blocks found in HTML (len={len(html or '')})"
        )
        return {}
```

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_html_extractor_logging.py -v -p no:cov`
Expected: PASS（1 passed）

- [ ] **Step 5: 型別與 lint**

Run: `.venv/bin/python -m pyright estimator_king/crawler/html_extractor.py tests/test_html_extractor_logging.py && .venv/bin/ruff check estimator_king/crawler/html_extractor.py tests/test_html_extractor_logging.py`
Expected: 0 errors（ruff 應確認已無區域重複 import）

- [ ] **Step 6: Commit**

```bash
git add estimator_king/crawler/html_extractor.py tests/test_html_extractor_logging.py
git commit -m "feat(log): converge html_extractor debug log to module logger"
```

---

## Task 5: `crawler/async_http_client.py` — DEBUG 請求記錄

**Files:**
- Modify: `estimator_king/crawler/async_http_client.py`
- Test: `tests/test_async_http_client_logging.py`（Create）

實作要點：新增 `import logging` 與 `logger = logging.getLogger(__name__)`。在 `_request_once` 中，於 `async with session.request("GET", url, ...) as resp:` 取得 `status` 後、進入任何狀態碼分支 raise 之前，輸出一筆 DEBUG。耗時以 `time.monotonic()` 在 `session.request` 前後量測。

- [ ] **Step 1: 撰寫失敗測試**

建立 `tests/test_async_http_client_logging.py`：

```python
import logging

import pytest

from estimator_king.config_schema import CrawlerPolicy
from estimator_king.crawler.async_http_client import AsyncHTTPClient, ClientError


class _FakeResponse:
    def __init__(self, status, text_value="", headers=None):
        self.status = status
        self._text_value = text_value
        self.headers = headers or {}

    async def text(self):
        return self._text_value


class _FakeCtx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, status):
        self._status = status
        self.closed = False

    def request(self, method, url, timeout=None, **kwargs):
        return _FakeCtx(_FakeResponse(self._status, "ok"))

    async def close(self):
        self.closed = True


def _policy():
    return CrawlerPolicy(
        rate_limit_rps=1000.0, jitter_max=0.0,
        concurrency_per_domain=1, max_retries=1,
    )


@pytest.mark.asyncio
async def test_debug_logs_successful_request(monkeypatch, caplog):
    monkeypatch.setattr(
        "estimator_king.crawler.async_http_client.aiohttp.ClientSession",
        lambda *a, **k: _FakeSession(200),
    )
    client = AsyncHTTPClient(_policy())
    with caplog.at_level(logging.DEBUG, logger="estimator_king.crawler.async_http_client"):
        await client.get("https://shop.example/products/1")
    await client.close()

    recs = [
        r for r in caplog.records
        if r.name == "estimator_king.crawler.async_http_client"
        and r.levelno == logging.DEBUG
    ]
    assert any(
        "https://shop.example/products/1" in r.getMessage() and "200" in r.getMessage()
        for r in recs
    )


@pytest.mark.asyncio
async def test_debug_logs_error_status_before_raise(monkeypatch, caplog):
    monkeypatch.setattr(
        "estimator_king.crawler.async_http_client.aiohttp.ClientSession",
        lambda *a, **k: _FakeSession(404),
    )
    client = AsyncHTTPClient(_policy())
    with caplog.at_level(logging.DEBUG, logger="estimator_king.crawler.async_http_client"):
        with pytest.raises(ClientError):
            await client.get("https://shop.example/products/x")
    await client.close()

    recs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("404" in r.getMessage() for r in recs)
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_async_http_client_logging.py -v -p no:cov`
Expected: FAIL（無 DEBUG 記錄，`recs` 為空）

- [ ] **Step 3: 實作**

在 `estimator_king/crawler/async_http_client.py` import 區塊後新增（`import time` 已存在）：

```python
import logging

logger = logging.getLogger(__name__)
```

修改 `_request_once`，把 `session.request` 段落改為：

```python
            session = await self._get_session()
            start = time.monotonic()
            async with session.request("GET", url, timeout=timeout) as resp:
                status = int(getattr(resp, "status", 0) or 0)
                logger.debug(
                    "GET %s -> %s in %.0fms",
                    url, status, (time.monotonic() - start) * 1000.0,
                )
                if status in (403, 430):
                    await self._circuit_breaker.record_waf_failure(domain)
                    raise WAFBlockedError(url, status_code=status)
                # ... 其餘狀態碼分支與成功路徑維持不變
```

（僅在取得 `status` 後、第一個 `if status in (403, 430):` 之前插入 `logger.debug(...)`，並把 `start = time.monotonic()` 放在 `async with` 之前。其餘程式碼不動。）

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_async_http_client_logging.py -v -p no:cov`
Expected: PASS（2 passed）

- [ ] **Step 5: 回歸 + 型別 + lint**

Run: `.venv/bin/python -m pytest tests/test_async_http_client.py -v -p no:cov && .venv/bin/python -m pyright estimator_king/crawler/async_http_client.py tests/test_async_http_client_logging.py && .venv/bin/ruff check estimator_king/crawler/async_http_client.py tests/test_async_http_client_logging.py`
Expected: 既有 async http client 測試全 PASS；型別/lint 0 errors

- [ ] **Step 6: Commit**

```bash
git add estimator_king/crawler/async_http_client.py tests/test_async_http_client_logging.py
git commit -m "feat(log): debug-log every async HTTP request with status and latency"
```

---

## Task 6: `crawler/http_client.py` — DEBUG 請求記錄

**Files:**
- Modify: `estimator_king/crawler/http_client.py`
- Test: `tests/test_http_client_logging.py`（Create）

實作要點：新增 `import logging` 與 `logger = logging.getLogger(__name__)`。在 `_request_once` 中，於 `resp = self.session.request(...)` 後、讀出 `status` 後、進入狀態碼分支 raise 之前輸出 DEBUG；耗時以 `time.monotonic()` 在 `session.request` 前後量測。訊息帶 `method`（不寫死 GET）。

- [ ] **Step 1: 撰寫失敗測試**

建立 `tests/test_http_client_logging.py`：

```python
import logging

import requests

from estimator_king.config_schema import CrawlerPolicy
from estimator_king.crawler.http_client import HTTPClient


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self):
        if 400 <= self.status_code:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _client(monkeypatch, status_code):
    session = requests.Session()

    def fake_request(method, url, timeout=None, **kwargs):
        return _FakeResponse(status_code)

    monkeypatch.setattr(session, "request", fake_request)
    return HTTPClient(
        crawler_policy=CrawlerPolicy(
            rate_limit_rps=1000.0, jitter_max=0.0,
            concurrency_per_domain=1, max_retries=1,
        ),
        session=session,
    )


def test_debug_logs_successful_request(monkeypatch, caplog):
    client = _client(monkeypatch, 200)
    with caplog.at_level(logging.DEBUG, logger="estimator_king.crawler.http_client"):
        client.get("https://example.com/page")

    recs = [
        r for r in caplog.records
        if r.name == "estimator_king.crawler.http_client" and r.levelno == logging.DEBUG
    ]
    assert any(
        "GET" in r.getMessage()
        and "https://example.com/page" in r.getMessage()
        and "200" in r.getMessage()
        for r in recs
    )
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_http_client_logging.py -v -p no:cov`
Expected: FAIL（無 DEBUG 記錄）

- [ ] **Step 3: 實作**

在 `estimator_king/crawler/http_client.py` import 區塊後新增（`import time` 已存在）：

```python
import logging

logger = logging.getLogger(__name__)
```

修改 `_request_once`，把 `resp = self.session.request(...)` 段落改為：

```python
        start = time.monotonic()
        resp = self.session.request(method, url, timeout=timeout, **kwargs)

        status = int(getattr(resp, "status_code", 0) or 0)
        logger.debug(
            "%s %s -> %s in %.0fms",
            method, url, status, (time.monotonic() - start) * 1000.0,
        )
        if status in (403, 430):
            self._circuit_breaker.record_waf_failure(domain)
            raise WAFBlockedError(url, status_code=status, response=resp)
        # ... 其餘狀態碼分支與成功路徑維持不變
```

（既有就有 `status = int(getattr(resp, "status_code", 0) or 0)`；在它之後、第一個狀態碼分支之前插入 `logger.debug(...)`，並把 `start = time.monotonic()` 放在 `self.session.request(...)` 之前。其餘不動。）

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_http_client_logging.py -v -p no:cov`
Expected: PASS（1 passed）

- [ ] **Step 5: 回歸 + 型別 + lint**

Run: `.venv/bin/python -m pytest tests/test_http_client.py -v -p no:cov && .venv/bin/python -m pyright estimator_king/crawler/http_client.py tests/test_http_client_logging.py && .venv/bin/ruff check estimator_king/crawler/http_client.py tests/test_http_client_logging.py`
Expected: 既有 http client 測試全 PASS；型別/lint 0 errors

- [ ] **Step 6: Commit**

```bash
git add estimator_king/crawler/http_client.py tests/test_http_client_logging.py
git commit -m "feat(log): debug-log every sync HTTP request with method, status and latency"
```

---

## Task 7: `llm/embeddings.py` — DEBUG 請求記錄

**Files:**
- Modify: `estimator_king/llm/embeddings.py`
- Test: `tests/test_embeddings_logging.py`（Create）

實作要點：新增 `import logging`、`import time` 與 `logger = logging.getLogger(__name__)`。在 `_embed` 中，於兩個 `self._client.embeddings.create(...)` 分支前後以 `time.monotonic()` 量測，呼叫完成後輸出一筆 DEBUG。

- [ ] **Step 1: 撰寫失敗測試**

建立 `tests/test_embeddings_logging.py`：

```python
import logging
from unittest.mock import MagicMock, patch

from estimator_king.llm.config import ProviderConfig
from estimator_king.llm.embeddings import EmbeddingProvider


def _fake_response(vectors):
    resp = MagicMock()
    resp.data = [MagicMock(embedding=v) for v in vectors]
    return resp


@patch("estimator_king.llm.embeddings.OpenAI")
def test_embed_emits_debug_with_count_and_model(mock_openai, caplog):
    client = mock_openai.return_value
    client.embeddings.create.return_value = _fake_response([[0.0]])
    cfg = ProviderConfig(
        embedding_api_key="k", chat_api_key="k",
        embedding_model="text-embedding-3-large", embedding_dimensions=None,
    )

    with caplog.at_level(logging.DEBUG, logger="estimator_king.llm.embeddings"):
        EmbeddingProvider(cfg).embed_query("hello")

    recs = [
        r for r in caplog.records
        if r.name == "estimator_king.llm.embeddings" and r.levelno == logging.DEBUG
    ]
    assert any(
        "embedding request" in r.getMessage()
        and "1 inputs" in r.getMessage()
        and "text-embedding-3-large" in r.getMessage()
        for r in recs
    )
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_embeddings_logging.py -v -p no:cov`
Expected: FAIL（無 DEBUG 記錄）

- [ ] **Step 3: 實作**

在 `estimator_king/llm/embeddings.py` import 區塊後新增：

```python
import logging
import time

logger = logging.getLogger(__name__)
```

修改 `_embed`：

```python
    def _embed(self, inputs: list[str]) -> list[list[float]]:
        inputs = [self._truncate(text) for text in inputs]
        start = time.monotonic()
        if self._config.embedding_dimensions is not None:
            response = self._client.embeddings.create(
                model=self._config.embedding_model,
                input=inputs,
                dimensions=self._config.embedding_dimensions,
            )
        else:
            response = self._client.embeddings.create(
                model=self._config.embedding_model,
                input=inputs,
            )
        logger.debug(
            "embedding request: %d inputs model=%s -> %.0fms",
            len(inputs), self._config.embedding_model,
            (time.monotonic() - start) * 1000.0,
        )
        return [item.embedding for item in response.data]
```

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_embeddings_logging.py -v -p no:cov`
Expected: PASS（1 passed）

- [ ] **Step 5: 回歸 + 型別 + lint**

Run: `.venv/bin/python -m pytest tests/test_embeddings.py -v -p no:cov && .venv/bin/python -m pyright estimator_king/llm/embeddings.py tests/test_embeddings_logging.py && .venv/bin/ruff check estimator_king/llm/embeddings.py tests/test_embeddings_logging.py`
Expected: 既有 embeddings 測試全 PASS；型別/lint 0 errors

- [ ] **Step 6: Commit**

```bash
git add estimator_king/llm/embeddings.py tests/test_embeddings_logging.py
git commit -m "feat(log): debug-log embedding API calls with input count and latency"
```

---

## Task 8: `llm/chat.py` — DEBUG 請求記錄（try/finally）

**Files:**
- Modify: `estimator_king/llm/chat.py`
- Test: `tests/test_chat_provider_logging.py`（Create）

實作要點：新增 `import logging`、`import time` 與 `logger = logging.getLogger(__name__)`。在 `estimate` 內以 `try/finally` 包住既有分派呼叫，於 `finally` 輸出一筆 DEBUG（成功與拋 `EstimationError` 都會記錄）。`finally` 不含 `return`/`raise`，例外照常向上傳遞。

- [ ] **Step 1: 撰寫失敗測試**

建立 `tests/test_chat_provider_logging.py`：

```python
import logging
from unittest.mock import MagicMock, patch

import pytest

from estimator_king.llm.chat import ChatProvider, EstimateBatch, EstimationError
from estimator_king.llm.config import ProviderConfig

VALID = {
    "estimates": [
        {
            "product_name": "p1",
            "suggested_price_jpy": 2000,
            "price_range_jpy": {"min": 1800, "max": 2200},
            "confidence": "high",
            "rationale": "because",
            "reference_products": [{"name": "ref", "price_jpy": 2000, "store": "hololive"}],
        }
    ]
}


@patch("estimator_king.llm.chat.OpenAI")
def test_debug_logged_on_success(mock_openai, caplog):
    client = mock_openai.return_value
    parsed = EstimateBatch.model_validate(VALID)
    msg = MagicMock(parsed=parsed, refusal=None)
    client.chat.completions.parse.return_value = MagicMock(choices=[MagicMock(message=msg)])
    cfg = ProviderConfig(embedding_api_key="k", chat_api_key="k", chat_structured_output=True)

    with caplog.at_level(logging.DEBUG, logger="estimator_king.llm.chat"):
        ChatProvider(cfg).estimate("sys", "user")

    recs = [
        r for r in caplog.records
        if r.name == "estimator_king.llm.chat" and r.levelno == logging.DEBUG
    ]
    assert any(
        "chat request" in r.getMessage() and "structured=True" in r.getMessage()
        for r in recs
    )


@patch("estimator_king.llm.chat.OpenAI")
def test_debug_logged_even_on_error(mock_openai, caplog):
    client = mock_openai.return_value
    msg = MagicMock(parsed=None, refusal="no")
    client.chat.completions.parse.return_value = MagicMock(choices=[MagicMock(message=msg)])
    cfg = ProviderConfig(embedding_api_key="k", chat_api_key="k", chat_structured_output=True)

    with caplog.at_level(logging.DEBUG, logger="estimator_king.llm.chat"):
        with pytest.raises(EstimationError):
            ChatProvider(cfg).estimate("sys", "user")

    recs = [
        r for r in caplog.records
        if r.name == "estimator_king.llm.chat" and r.levelno == logging.DEBUG
    ]
    assert any("chat request" in r.getMessage() for r in recs)
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_chat_provider_logging.py -v -p no:cov`
Expected: FAIL（無 DEBUG 記錄）

- [ ] **Step 3: 實作**

在 `estimator_king/llm/chat.py` import 區塊後新增（既有已有 `import json`）：

```python
import logging
import time

logger = logging.getLogger(__name__)
```

修改 `estimate`：

```python
    def estimate(self, system_prompt: str, user_prompt: str) -> EstimateBatch:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        start = time.monotonic()
        try:
            if self._config.chat_structured_output:
                return self._estimate_structured(messages)
            return self._estimate_json_object(messages)
        finally:
            logger.debug(
                "chat request: model=%s structured=%s -> %.0fms",
                self._config.chat_model,
                self._config.chat_structured_output,
                (time.monotonic() - start) * 1000.0,
            )
```

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_chat_provider_logging.py -v -p no:cov`
Expected: PASS（2 passed）

- [ ] **Step 5: 回歸 + 型別 + lint**

Run: `.venv/bin/python -m pytest tests/test_chat_provider.py -v -p no:cov && .venv/bin/python -m pyright estimator_king/llm/chat.py tests/test_chat_provider_logging.py && .venv/bin/ruff check estimator_king/llm/chat.py tests/test_chat_provider_logging.py`
Expected: 既有 chat provider 測試全 PASS（含 refusal/invalid-json 例外仍正常傳遞）；型別/lint 0 errors

- [ ] **Step 6: Commit**

```bash
git add estimator_king/llm/chat.py tests/test_chat_provider_logging.py
git commit -m "feat(log): debug-log chat API calls with model and latency via try/finally"
```

---

## Task 9: `crawler/pipeline.py` — sitemap 數量 INFO

**Files:**
- Modify: `estimator_king/crawler/pipeline.py`
- Test: `tests/test_pipeline_logging.py`（Create）

實作要點：在 `populate_queue_from_sitemap` 的 `return enqueued` 之前，新增一筆 INFO 記錄 sitemap 總數與新增數。空 sitemap 走既有 early return（WARNING），不發此 INFO。

- [ ] **Step 1: 撰寫失敗測試**

建立 `tests/test_pipeline_logging.py`：

```python
import logging

import pytest

from estimator_king.config_schema import Store
from estimator_king.crawler.pipeline import populate_queue_from_sitemap
from estimator_king.database.repository import ProductStateRepository


@pytest.fixture
def repo():
    with ProductStateRepository(":memory:") as r:
        yield r


def _store():
    return Store(id="hololive", base_url="https://x", sitemap_url="https://x/sitemap.xml")


class FakeEnumerator:
    def __init__(self, urls):
        self._urls = urls

    def enumerate_products(self, base_url):
        return self._urls


def test_sitemap_summary_info_logged(repo, caplog):
    enum = FakeEnumerator(["https://x/products/1", "https://x/products/2"])
    with caplog.at_level(logging.INFO, logger="estimator_king.crawler.pipeline"):
        populate_queue_from_sitemap(_store(), repo, enum)

    recs = [
        r for r in caplog.records
        if r.name == "estimator_king.crawler.pipeline" and r.levelno == logging.INFO
    ]
    assert any(
        "store=hololive" in r.getMessage()
        and "2 total" in r.getMessage()
        and "2 new enqueued" in r.getMessage()
        for r in recs
    )


def test_empty_sitemap_warns_and_skips_summary(repo, caplog):
    enum = FakeEnumerator([])
    with caplog.at_level(logging.INFO, logger="estimator_king.crawler.pipeline"):
        result = populate_queue_from_sitemap(_store(), repo, enum)

    assert result == 0
    msgs = [r.getMessage() for r in caplog.records]
    assert any("returned 0 URLs" in m for m in msgs)
    assert not any("new enqueued" in m for m in msgs)
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_pipeline_logging.py -v -p no:cov`
Expected: FAIL（第一個測試無 summary INFO）

- [ ] **Step 3: 實作**

在 `estimator_king/crawler/pipeline.py` 的 `populate_queue_from_sitemap` 結尾改為：

```python
    logger.info(
        "store=%s sitemap: %d total, %d new enqueued",
        store.id, len(sitemap_urls), enqueued,
    )
    return enqueued
```

（插入於既有 `return enqueued` 之前；module logger 已存在。）

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_pipeline_logging.py -v -p no:cov`
Expected: PASS（2 passed）

- [ ] **Step 5: 回歸 + 型別 + lint**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -v -p no:cov && .venv/bin/python -m pyright estimator_king/crawler/pipeline.py tests/test_pipeline_logging.py && .venv/bin/ruff check estimator_king/crawler/pipeline.py tests/test_pipeline_logging.py`
Expected: 既有 pipeline 測試全 PASS；型別/lint 0 errors

- [ ] **Step 6: Commit**

```bash
git add estimator_king/crawler/pipeline.py tests/test_pipeline_logging.py
git commit -m "feat(log): info-log sitemap total and newly enqueued counts per store"
```

---

## Task 10: `crawler/async_pipeline.py` — 隊列起始/心跳/完成 INFO

**Files:**
- Modify: `estimator_king/crawler/async_pipeline.py`
- Test: `tests/test_async_pipeline_logging.py`（Create）

實作要點：新增模組常數 `_PROGRESS_LOG_EVERY = 20`。在 `async_process_queue` 中：取得非空 `entries` 後輸出起始 INFO；在 `_handle` 成功路徑既有 `async with lock:` 內 `result.processed += 1` 之後判斷 `% _PROGRESS_LOG_EVERY == 0` 輸出心跳 INFO；`gather` 之後、`return result` 之前輸出完成 INFO。複用既有 lock，不另開鎖。

- [ ] **Step 1: 撰寫失敗測試**

建立 `tests/test_async_pipeline_logging.py`：

```python
import asyncio
import logging
from unittest.mock import patch

import pytest

from estimator_king.config_schema import CrawlerPolicy
from estimator_king.crawler import async_pipeline
from estimator_king.crawler.async_pipeline import async_process_queue
from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.database.repository import ProductStateRepository


class FakeEmbedder:
    def embed_documents(self, texts):
        return [[0.1, 0.2] for _ in texts]


class FakeVectorStore:
    def upsert(self, id, document, embedding, metadata):
        pass

    def delete(self, ids):
        pass


@pytest.fixture
def repo():
    with ProductStateRepository(":memory:") as r:
        yield r


def _snap(pid):
    return ProductSnapshot(
        product_id=pid, title=f"T{pid}", description="d",
        variants=[ProductVariant(1, "S", "2000")], html_details={},
    )


def test_queue_start_heartbeat_and_done_logged(repo, caplog):
    n = async_pipeline._PROGRESS_LOG_EVERY + 5
    for pid in range(1, n + 1):
        repo.enqueue_url("hololive", f"https://x/products/{pid}")

    def fake_fetch(url, client):
        pid = int(url.rsplit("/", 1)[1])
        return _snap(pid)

    with caplog.at_level(logging.INFO, logger="estimator_king.crawler.async_pipeline"):
        with patch(
            "estimator_king.crawler.async_pipeline.fetch_product",
            side_effect=fake_fetch,
        ):
            result = asyncio.run(async_process_queue(
                "hololive", "https://x", CrawlerPolicy(), repo,
                FakeEmbedder(), FakeVectorStore()))

    assert result.processed == n
    msgs = [
        r.getMessage() for r in caplog.records
        if r.name == "estimator_king.crawler.async_pipeline" and r.levelno == logging.INFO
    ]
    assert any(f"queue: {n} entries to process" in m for m in msgs)
    assert any("progress:" in m and f"/{n} processed" in m for m in msgs)
    assert any("done: created=" in m for m in msgs)
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_async_pipeline_logging.py -v -p no:cov`
Expected: FAIL（`AttributeError: ... '_PROGRESS_LOG_EVERY'` 或無對應 INFO）

- [ ] **Step 3: 實作**

在 `estimator_king/crawler/async_pipeline.py` 的 `logger = logging.getLogger(__name__)` 之後新增常數：

```python
_PROGRESS_LOG_EVERY = 20
```

修改 `async_process_queue`，於非空 `entries` 之後（`result = PipelineResult()` 前後皆可，置於 `if not entries: return PipelineResult()` 之後）新增起始 INFO：

```python
    entries = state_repo.peek_all(store_id)
    if not entries:
        return PipelineResult()

    logger.info("store=%s queue: %d entries to process", store_id, len(entries))

    loop = asyncio.get_running_loop()
    result = PipelineResult()
    lock = asyncio.Lock()
```

在 `_handle` 成功路徑既有 `async with lock:` 區塊內，於 `result.processed += 1` 之後新增心跳：

```python
                async with lock:
                    result.created += sync_result.created
                    result.updated += sync_result.updated
                    result.sync_skipped += sync_result.skipped
                    result.processed += 1
                    if result.processed % _PROGRESS_LOG_EVERY == 0:
                        logger.info(
                            "store=%s progress: %d/%d processed",
                            store_id, result.processed, len(entries),
                        )
```

在 `await asyncio.gather(...)` 之後、`return result` 之前新增完成 INFO：

```python
        await asyncio.gather(*[_bounded(entry) for entry in entries])

    logger.info(
        "store=%s done: created=%d updated=%d skipped=%d failed=%d",
        store_id, result.created, result.updated, result.sync_skipped, result.failed,
    )
    return result
```

（注意 `return result` 在 `async with AsyncHTTPClient(...)` 區塊外；完成 INFO 與 `return result` 同層，置於 `async with` 區塊結束後。）

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_async_pipeline_logging.py -v -p no:cov`
Expected: PASS（1 passed）

- [ ] **Step 5: 回歸 + 型別 + lint**

Run: `.venv/bin/python -m pytest tests/test_async_pipeline.py tests/test_integration_async_pipeline.py -v -p no:cov && .venv/bin/python -m pyright estimator_king/crawler/async_pipeline.py tests/test_async_pipeline_logging.py && .venv/bin/ruff check estimator_king/crawler/async_pipeline.py tests/test_async_pipeline_logging.py`
Expected: 既有 async pipeline 測試全 PASS；型別/lint 0 errors

- [ ] **Step 6: Commit**

```bash
git add estimator_king/crawler/async_pipeline.py tests/test_async_pipeline_logging.py
git commit -m "feat(log): info-log queue start, progress heartbeat and per-store done counts"
```

---

## Task 11: `bot/estimator.py` — 完成 INFO + 每 chunk DEBUG

**Files:**
- Modify: `estimator_king/bot/estimator.py`
- Test: `tests/test_estimator_logging.py`（Create）

實作要點：新增 `import time`（既有 `import logging` 與 module logger 已存在）。`estimate_products` 進入前以 `time.monotonic()` 記錄起始；在 chunk 迴圈內於呼叫 `_estimate_chunk` 前輸出每 chunk DEBUG；return 前輸出完成 INFO（含估價數與耗時秒）。既有的請求 INFO 保留。

- [ ] **Step 1: 撰寫失敗測試**

建立 `tests/test_estimator_logging.py`：

```python
import logging

from estimator_king.bot.estimator import Estimator
from estimator_king.llm.chat import EstimateBatch, PriceRange, ProductEstimate
from estimator_king.vectorstore.store import QueryHit


class FakeEmbedder:
    def embed_query(self, text):
        return [0.1, 0.2]


class FakeVectorStore:
    def query(self, embedding, n_results, where=None):
        return [QueryHit(
            id="hololive:1", document="doc",
            metadata={"title": "ref", "price_jpy": 2000, "store_id": "hololive"},
            distance=0.1,
        )]


class FakeChat:
    def estimate(self, system_prompt, user_prompt):
        return EstimateBatch(estimates=[ProductEstimate(
            product_name="p", suggested_price_jpy=2000,
            price_range_jpy=PriceRange(min=1800, max=2200),
            confidence="high", rationale="r", reference_products=[],
        )])


def test_chunk_debug_and_done_info(caplog):
    est = Estimator(FakeEmbedder(), FakeChat(), FakeVectorStore())
    est.CHUNK_SIZE = 1  # force two chunks

    with caplog.at_level(logging.DEBUG, logger="estimator_king.bot.estimator"):
        est.estimate_products(["a", "b"], "discord-1")

    recs = [r for r in caplog.records if r.name == "estimator_king.bot.estimator"]
    debug_msgs = [r.getMessage() for r in recs if r.levelno == logging.DEBUG]
    info_msgs = [r.getMessage() for r in recs if r.levelno == logging.INFO]

    assert any("chunk 1/2: 1 products" in m for m in debug_msgs)
    assert any("chunk 2/2: 1 products" in m for m in debug_msgs)
    assert any(
        "estimate done for discord-1" in m and "2 estimates" in m for m in info_msgs
    )
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_estimator_logging.py -v -p no:cov`
Expected: FAIL（無 chunk DEBUG / done INFO）

- [ ] **Step 3: 實作**

在 `estimator_king/bot/estimator.py` import 區塊新增 `import time`（置於既有 `import logging` 附近）。修改 `estimate_products`：

```python
    def estimate_products(self, product_names: list[str], user_id: str) -> EstimateBatch:
        if not product_names:
            return EstimateBatch(estimates=[])
        logger.info("estimate request from %s for %d products", user_id, len(product_names))
        start = time.monotonic()
        total_chunks = (len(product_names) + self.CHUNK_SIZE - 1) // self.CHUNK_SIZE
        all_estimates = []
        for start_idx in range(0, len(product_names), self.CHUNK_SIZE):
            chunk = product_names[start_idx : start_idx + self.CHUNK_SIZE]
            logger.debug(
                "chunk %d/%d: %d products",
                start_idx // self.CHUNK_SIZE + 1, total_chunks, len(chunk),
            )
            batch = self._estimate_chunk(chunk)
            all_estimates.extend(batch.estimates)
        logger.info(
            "estimate done for %s: %d estimates in %.1fs",
            user_id, len(all_estimates), time.monotonic() - start,
        )
        return EstimateBatch(estimates=all_estimates)
```

（既有迴圈變數名為 `start`；此處改名為 `start_idx` 以避免與計時用的 `start` 衝突。`_estimate_chunk` 不變。）

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_estimator_logging.py -v -p no:cov`
Expected: PASS（1 passed）

- [ ] **Step 5: 回歸 + 型別 + lint**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -v -p no:cov && .venv/bin/python -m pyright estimator_king/bot/estimator.py tests/test_estimator_logging.py && .venv/bin/ruff check estimator_king/bot/estimator.py tests/test_estimator_logging.py`
Expected: 既有 estimator 測試全 PASS；型別/lint 0 errors

- [ ] **Step 6: Commit**

```bash
git add estimator_king/bot/estimator.py tests/test_estimator_logging.py
git commit -m "feat(log): info-log estimate completion and debug-log per-chunk progress"
```

---

## Task 12: `bot/commands.py` — modal/驗證/錯誤記錄

**Files:**
- Modify: `estimator_king/bot/commands.py`
- Test: `tests/test_bot_commands_logging.py`（Create）

實作要點：新增 `import logging` 與 `logger = logging.getLogger(__name__)`。在 `ProductInputModal.on_submit` 中：解析後 DEBUG；驗證失敗（<1、>MAX）WARNING；`except EstimationError` 加 ERROR；`except Exception` 加 `logger.exception`。所有既有回傳使用者訊息行為不變。測試以 mock interaction + monkeypatch `parse_product_lines` 驅動各分支。

- [ ] **Step 1: 撰寫失敗測試**

建立 `tests/test_bot_commands_logging.py`：

```python
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

from estimator_king.bot.commands import ProductInputModal
from estimator_king.llm.chat import EstimationError


def _interaction(user_id=123):
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


class OkEstimator:
    def estimate_products(self, names, user_id):
        from estimator_king.llm.chat import EstimateBatch
        return EstimateBatch(estimates=[])


class BoomEstimator:
    def estimate_products(self, names, user_id):
        raise EstimationError("model refused")


def test_too_many_products_logs_warning(monkeypatch, caplog):
    monkeypatch.setattr(
        "estimator_king.bot.commands.parse_product_lines",
        lambda text: ["p"] * 11,
    )
    modal = ProductInputModal(OkEstimator())
    interaction = _interaction()

    with caplog.at_level(logging.DEBUG, logger="estimator_king.bot.commands"):
        asyncio.run(modal.on_submit(interaction))

    recs = [r for r in caplog.records if r.name == "estimator_king.bot.commands"]
    assert any(
        r.levelno == logging.WARNING and "exceeds max" in r.getMessage() for r in recs
    )
    interaction.response.send_message.assert_awaited()  # 既有行為保留


def test_estimation_error_logs_error(monkeypatch, caplog):
    monkeypatch.setattr(
        "estimator_king.bot.commands.parse_product_lines",
        lambda text: ["p"],
    )
    modal = ProductInputModal(BoomEstimator())
    interaction = _interaction()

    with caplog.at_level(logging.DEBUG, logger="estimator_king.bot.commands"):
        asyncio.run(modal.on_submit(interaction))

    recs = [r for r in caplog.records if r.name == "estimator_king.bot.commands"]
    assert any(
        r.levelno == logging.ERROR and "estimation failed" in r.getMessage() for r in recs
    )
    interaction.followup.send.assert_awaited()  # 既有行為保留
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_bot_commands_logging.py -v -p no:cov`
Expected: FAIL（無對應 WARNING/ERROR 記錄）

- [ ] **Step 3: 實作**

在 `estimator_king/bot/commands.py` import 區塊後新增：

```python
import logging

logger = logging.getLogger(__name__)
```

修改 `on_submit`：

```python
    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Parse product lines from user input
        product_list = parse_product_lines(self.products.value)
        logger.debug(
            "modal submitted by %s: %d products parsed",
            interaction.user.id, len(product_list),
        )

        # Validation: minimum 1 product
        if len(product_list) < 1:
            logger.warning("validation failed for %s: empty input", interaction.user.id)
            await interaction.response.send_message(
                "❌ Please enter at least 1 product name", ephemeral=True
            )
            return

        # Validation: maximum 10 products
        if len(product_list) > MAX_PRODUCTS:
            logger.warning(
                "validation failed for %s: %d products exceeds max %d",
                interaction.user.id, len(product_list), MAX_PRODUCTS,
            )
            await interaction.response.send_message(
                f"❌ Maximum {MAX_PRODUCTS} products allowed", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        try:
            user_id = f"discord-{interaction.user.id}"
            batch = self._estimator.estimate_products(product_list, user_id)
            for embed in format_estimates(batch):
                await interaction.followup.send(embed=embed)
        except EstimationError as e:
            logger.error("estimation failed for %s: %s", interaction.user.id, e)
            await interaction.followup.send(f"❌ Estimation failed: {e}")
        except Exception as e:
            logger.exception(
                "unexpected error handling request from %s", interaction.user.id
            )
            await interaction.followup.send(f"❌ Unexpected error: {e}")
```

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_bot_commands_logging.py -v -p no:cov`
Expected: PASS（2 passed）

- [ ] **Step 5: 回歸 + 型別 + lint**

Run: `.venv/bin/python -m pytest tests/test_bot_commands.py -v -p no:cov && .venv/bin/python -m pyright estimator_king/bot/commands.py tests/test_bot_commands_logging.py && .venv/bin/ruff check estimator_king/bot/commands.py tests/test_bot_commands_logging.py`
Expected: 既有 bot commands 測試全 PASS；型別/lint 0 errors

- [ ] **Step 6: Commit**

```bash
git add estimator_king/bot/commands.py tests/test_bot_commands_logging.py
git commit -m "feat(log): log modal submission, validation failures and estimation errors"
```

---

## Task 13: 全套件最終驗證

**Files:** 無（僅驗證）

- [ ] **Step 1: 全套件測試（含覆蓋率）**

Run: `.venv/bin/python -m pytest`
Expected: 全部 PASS（既有測試 + 12 個新測試檔），無 ERROR/FAIL

- [ ] **Step 2: 全專案型別檢查**

Run: `.venv/bin/python -m pyright`
Expected: 0 errors（沿用 `pyrightconfig.json` 設定）

- [ ] **Step 3: 全專案 lint**

Run: `.venv/bin/ruff check`
Expected: All checks passed

- [ ] **Step 4: 手動煙霧驗證（DEBUG 等級觀察 log 區分）**

Run: `.venv/bin/python -m estimator_king crawl --log-level DEBUG --config stores_config.yaml 2>&1 | head -40`
Expected: stderr log 行帶有 logger name（如 `estimator_king.crawler.cycle: Processing store ...`、`estimator_king.crawler.pipeline: store=... sitemap: N total, M new enqueued`），可從 name 前綴區分 CRAWLER；DEBUG 等級可見 `GET ... -> ... in ...ms` 與 `embedding request: ...`。

> 註：此步驟需有效 `stores_config.yaml` 與 API key；若環境不具備，記錄為「環境不足、略過」並依賴 Task 5–11 的 caplog 測試作為等效驗證證據。

---

## Self-Review（writing-plans 自檢）

**1. Spec coverage（逐項對照）：**
- §1.1 格式加 `%(name)s` → Task 1（`_LOG_FORMAT`）
- §1.2 root logger 收斂：`__main__.py` → Task 1；`bot/runner.py` → Task 2；`sync/engine.py` → Task 3；`crawler/html_extractor.py` → Task 4
- §2.1 async HTTP DEBUG → Task 5
- §2.2 sync HTTP DEBUG → Task 6
- §2.3 embedding DEBUG → Task 7
- §2.4 chat DEBUG（try/finally）→ Task 8
- §3.1 sitemap 數量 INFO → Task 9
- §3.2 隊列起始/心跳/完成 INFO（`_PROGRESS_LOG_EVERY=20`）→ Task 10
- §3.3 既有 crawler INFO 保留 → 未改動 `cycle.py`/`scheduler.py`（無對應 Task，符合「保留不動」）
- §4.1 estimator 完成 INFO + chunk DEBUG → Task 11
- §4.2 commands modal/驗證/錯誤記錄 → Task 12
- 測試策略 1–4 → 各對應 Task 的 caplog 測試；型別/lint/測試門檻 → 各 Task Step 5 + Task 13
- 已知取捨（共用模組 name 為中性）→ 不需實作，Task 7（embeddings）的 name 即 `estimator_king.llm.embeddings`，與取捨一致

**2. Placeholder scan：** 已檢查，無 TBD/TODO/「適當處理」等空泛描述；每個程式碼步驟均含實際程式碼與斷言。

**3. Type consistency：** 訊息格式字串與參數在 spec 與各 Task 一致；`_PROGRESS_LOG_EVERY` 名稱於 Task 10 定義與測試引用一致；`_LOG_FORMAT` 於 Task 1 定義與測試引用一致；estimator 迴圈改名 `start_idx` 避免與計時 `start` 衝突，且不影響 `_estimate_chunk` 介面。
