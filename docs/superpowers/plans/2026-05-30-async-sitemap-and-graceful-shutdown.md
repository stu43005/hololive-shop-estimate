# Async Sitemap Migration + Proxy + Dead-Code Removal + Graceful Shutdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把爬蟲 sitemap 列舉從同步阻塞改寫為 async（走既有 `AsyncHTTPClient`），讓整個 crawl cycle 在 bot event loop 上不再卡心跳；同時為 `AsyncHTTPClient` 加上 proxy 支援、移除遷移後變死碼的同步 `HTTPClient`、並讓 Ctrl+C 能優雅關閉（第二次強退）。

**Architecture:** `AsyncHTTPClient`（aiohttp + `asyncio.sleep`）成為唯一 HTTP stack。`SitemapEnumerator` 改吃 `AsyncHTTPClient`、方法全 async；`populate_queue_from_sitemap` 與 `run_crawl_cycle` 沿 async 鏈傳遞 `await`。關閉改為 cancel scheduler task + `bot.close()`，第二次訊號 `os._exit(130)`。

**Tech Stack:** Python 3.14、aiohttp 3.13.5、tenacity 9.1.4、discord.py、pytest + pytest-asyncio、basedpyright、ruff。

**驗證指令（每個 task 末段使用）：**
- 型別：`.venv/bin/basedpyright estimator_king`（production 須 0 errors）
- Lint：`uvx ruff check estimator_king tests`
- 測試：`.venv/bin/pytest <檔案> -o addopts="" -q`

**Spec：** `docs/superpowers/specs/2026-05-30-async-sitemap-and-graceful-shutdown-design.md`

**Task 執行順序（依相依）：** 1 → 2 → 3 → 4 → 5 → 6 → 7。Task 5（刪除 `http_client.py`）必須在 Task 3、4 之後（屆時已無 import 它）。

---

## File Structure

| 檔案 | 責任 | Task |
|---|---|---|
| `estimator_king/crawler/async_http_client.py` | 加 proxy 支援（scheme 選值 + 帳密拆解） | 1 |
| `estimator_king/crawler/async_pipeline.py` | `async_process_queue` 接受並轉傳 `proxy` | 2 |
| `estimator_king/crawler/sitemap.py` | `SitemapEnumerator` 改 async、走 `AsyncHTTPClient` | 3 |
| `estimator_king/crawler/pipeline.py` | `populate_queue_from_sitemap` 改 async | 3 |
| `estimator_king/crawler/cycle.py` | 用 `AsyncHTTPClient` 跑 sitemap、傳 proxy、await | 3 |
| `estimator_king/crawler/shopify.py` | `http_client` 型別改 `Protocol`、移除 requests/HTTPClient import | 4 |
| `estimator_king/crawler/http_client.py` | **刪除**（死碼） | 5 |
| `estimator_king/bot/runner.py` | 兩段式優雅關閉 + 第二次強退（`_Shutdowner`） | 6 |
| `estimator_king/bot/scheduler.py` | 無 production 改動；以測試鎖定 cancellation 契約 | 7 |

---

## Task 1: AsyncHTTPClient proxy 支援

**Files:**
- Modify: `estimator_king/crawler/async_http_client.py`
- Test: `tests/test_async_http_client.py`

- [ ] **Step 1: 擴充 `_FakeSession` 以捕獲 request kwargs，並寫 proxy 失敗測試**

修改 `tests/test_async_http_client.py`。先把既有 `_FakeSession` 改成會記錄最後一次 `request` 的 kwargs（向後相容，既有測試不受影響）：

```python
class _FakeSession:
    def __init__(self, request_factory):
        self._request_factory = request_factory
        self.closed = False
        self.last_kwargs: dict = {}

    def request(self, method: str, url: str, timeout=None, **kwargs):  # pyright: ignore[reportUnusedParameter]
        self.last_kwargs = kwargs
        return self._request_factory(method, url)

    async def close(self) -> None:
        self.closed = True
```

把檔案頂端的 import 補上 `ProxyConfig`：

```python
from estimator_king.config_schema import CrawlerPolicy, ProxyConfig
```

在檔案末端新增 proxy 測試：

```python
@pytest.mark.asyncio
async def test_proxy_selected_by_target_scheme(monkeypatch):
    policy = CrawlerPolicy(
        rate_limit_rps=1000.0, jitter_max=0.0, concurrency_per_domain=1, max_retries=1
    )
    fake_session = _FakeSession(
        lambda method, url: _FakeRequestContextManager(_FakeResponse(200, "ok"))
    )
    monkeypatch.setattr(
        "estimator_king.crawler.async_http_client.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )
    proxy = ProxyConfig(enabled=True, http_proxy="http://hp:8080", https_proxy="http://sp:8080")
    client = AsyncHTTPClient(policy, proxy=proxy)

    await client.get("http://plain.example/x")
    assert str(fake_session.last_kwargs["proxy"]) == "http://hp:8080"
    assert fake_session.last_kwargs.get("proxy_auth") is None

    await client.get("https://secure.example/x")
    assert str(fake_session.last_kwargs["proxy"]) == "http://sp:8080"

    await client.close()


@pytest.mark.asyncio
async def test_proxy_disabled_sends_no_proxy(monkeypatch):
    policy = CrawlerPolicy(
        rate_limit_rps=1000.0, jitter_max=0.0, concurrency_per_domain=1, max_retries=1
    )
    fake_session = _FakeSession(
        lambda method, url: _FakeRequestContextManager(_FakeResponse(200, "ok"))
    )
    monkeypatch.setattr(
        "estimator_king.crawler.async_http_client.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )
    proxy = ProxyConfig(enabled=False, http_proxy="http://hp:8080")
    client = AsyncHTTPClient(policy, proxy=proxy)

    await client.get("http://plain.example/x")
    assert "proxy" not in fake_session.last_kwargs

    await client.close()


@pytest.mark.asyncio
async def test_proxy_enabled_but_selected_value_empty_sends_no_proxy(monkeypatch):
    policy = CrawlerPolicy(
        rate_limit_rps=1000.0, jitter_max=0.0, concurrency_per_domain=1, max_retries=1
    )
    fake_session = _FakeSession(
        lambda method, url: _FakeRequestContextManager(_FakeResponse(200, "ok"))
    )
    monkeypatch.setattr(
        "estimator_king.crawler.async_http_client.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )
    # enabled, but https_proxy is empty and target is https -> no proxy
    proxy = ProxyConfig(enabled=True, http_proxy="http://hp:8080", https_proxy="")
    client = AsyncHTTPClient(policy, proxy=proxy)

    await client.get("https://secure.example/x")
    assert "proxy" not in fake_session.last_kwargs

    await client.close()


@pytest.mark.asyncio
async def test_proxy_credentials_split_into_basic_auth(monkeypatch):
    from aiohttp import BasicAuth

    policy = CrawlerPolicy(
        rate_limit_rps=1000.0, jitter_max=0.0, concurrency_per_domain=1, max_retries=1
    )
    fake_session = _FakeSession(
        lambda method, url: _FakeRequestContextManager(_FakeResponse(200, "ok"))
    )
    monkeypatch.setattr(
        "estimator_king.crawler.async_http_client.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )
    proxy = ProxyConfig(enabled=True, http_proxy="http://user:pass@hp:8080")
    client = AsyncHTTPClient(policy, proxy=proxy)

    await client.get("http://plain.example/x")
    assert str(fake_session.last_kwargs["proxy"]) == "http://hp:8080"
    assert fake_session.last_kwargs["proxy_auth"] == BasicAuth("user", "pass")

    await client.close()
```

- [ ] **Step 2: 執行新測試確認失敗**

Run: `.venv/bin/pytest tests/test_async_http_client.py -o addopts="" -q -k proxy`
Expected: FAIL（`AsyncHTTPClient.__init__` 還沒有 `proxy` 參數 → `TypeError: unexpected keyword argument 'proxy'`）

- [ ] **Step 3: 在 `async_http_client.py` 實作 proxy 支援**

修改 `estimator_king/crawler/async_http_client.py`。

(a) import 區塊：把 `from ..config_schema import CrawlerPolicy` 改為同時匯入 `ProxyConfig`，並新增 aiohttp/yarl 工具：

```python
from aiohttp import BasicAuth
from aiohttp.helpers import strip_auth_from_url
from yarl import URL

from .. import __version__
from ..config_schema import CrawlerPolicy, ProxyConfig
```

（`BasicAuth` 匯入僅供型別清晰；實作不自行建構，由 `strip_auth_from_url` 回傳。若 ruff 報未使用，改為只匯入 `strip_auth_from_url` 與 `URL`。）

(b) `AsyncHTTPClient.__init__` 簽名新增 `proxy` 參數（放在 `policy` 之後、`*` 之前的 keyword 區，沿用既有風格放在 `*` 後亦可；此處放在現有 keyword-only 區塊最前）：

```python
    def __init__(
        self,
        policy: CrawlerPolicy,
        *,
        proxy: ProxyConfig | None = None,
        circuit_breaker_failure_threshold: int = 3,
        circuit_breaker_open_timeout_seconds: float = 60.0,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
        uniform_fn: Callable[[float, float], float] = random.uniform,
    ):
        self._policy = policy
        self._proxy: ProxyConfig = proxy or ProxyConfig()
        self._session: aiohttp.ClientSession | None = None
```

（即在原本 `self._policy = policy` 之後加入 `self._proxy` 一行；其餘 `__init__` 內容不變。）

(c) 新增 `_select_proxy` 方法（放在 `_get_domain_semaphore` 之後）：

```python
    def _select_proxy(self, url: str) -> str | None:
        if not self._proxy.enabled:
            return None
        scheme = urlsplit(url).scheme
        value = self._proxy.https_proxy if scheme == "https" else self._proxy.http_proxy
        return value or None
```

(d) 在 `_request_once` 內，把原本：

```python
            session = await self._get_session()
            start = time.monotonic()
            async with session.request("GET", url, timeout=timeout) as resp:
```

改為：

```python
            session = await self._get_session()
            request_kwargs: dict[str, object] = {"timeout": timeout}
            proxy_value = self._select_proxy(url)
            if proxy_value is not None:
                stripped, proxy_auth = strip_auth_from_url(URL(proxy_value))
                request_kwargs["proxy"] = stripped
                if proxy_auth is not None:
                    request_kwargs["proxy_auth"] = proxy_auth
            start = time.monotonic()
            async with session.request("GET", url, **request_kwargs) as resp:
```

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/bin/pytest tests/test_async_http_client.py -o addopts="" -q`
Expected: PASS（全部，含既有測試）

- [ ] **Step 5: 型別與 lint**

Run: `.venv/bin/basedpyright estimator_king/crawler/async_http_client.py`
Expected: 0 errors
Run: `uvx ruff check estimator_king/crawler/async_http_client.py tests/test_async_http_client.py`
Expected: All checks passed

- [ ] **Step 6: Commit**

```bash
git add estimator_king/crawler/async_http_client.py tests/test_async_http_client.py
git commit -m "feat(crawler): add proxy support to AsyncHTTPClient with credential splitting"
```

---

## Task 2: async_process_queue 接受並轉傳 proxy

**Files:**
- Modify: `estimator_king/crawler/async_pipeline.py`
- Test: `tests/test_async_pipeline.py`

- [ ] **Step 1: 寫 proxy 轉傳測試**

在 `tests/test_async_pipeline.py` 頂端 import 補 `ProxyConfig`：

```python
from estimator_king.config_schema import CrawlerPolicy, ProxyConfig
```

新增測試（驗證 `async_process_queue` 把 proxy 傳給它建立的 `AsyncHTTPClient`）：

```python
def test_proxy_forwarded_to_async_http_client(repo):
    repo.enqueue_url("hololive", "https://x/products/1")
    captured = {}

    class _FakeClient:
        def __init__(self, policy, proxy=None):
            captured["proxy"] = proxy

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    proxy_cfg = ProxyConfig(enabled=True, http_proxy="http://p:8080")
    with patch("estimator_king.crawler.async_pipeline.AsyncHTTPClient", _FakeClient), \
         patch("estimator_king.crawler.async_pipeline.fetch_product", return_value=_snap(1)):
        asyncio.run(async_process_queue(
            "hololive", "https://x", CrawlerPolicy(), repo,
            FakeEmbedder(), FakeVectorStore(), proxy=proxy_cfg))

    assert captured["proxy"] is proxy_cfg
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/bin/pytest tests/test_async_pipeline.py::test_proxy_forwarded_to_async_http_client -o addopts="" -q`
Expected: FAIL（`async_process_queue` 尚無 `proxy` 參數 → `TypeError`）

- [ ] **Step 3: 在 `async_pipeline.py` 加 proxy 參數**

修改 `estimator_king/crawler/async_pipeline.py`：

(a) 在 `TYPE_CHECKING` 區塊補 `ProxyConfig`：

```python
if TYPE_CHECKING:
    from estimator_king.config_schema import CrawlerPolicy, ProxyConfig
    from estimator_king.database.repository import ProductStateRepository
    from estimator_king.llm.embeddings import EmbeddingProvider
    from estimator_king.vectorstore.store import VectorStore
```

(b) `async_process_queue` 簽名新增 keyword-only `proxy` 參數：

```python
async def async_process_queue(
    store_id: str,
    store_base_url: str,
    policy: CrawlerPolicy,
    state_repo: ProductStateRepository,
    embedder: EmbeddingProvider,
    vector_store: VectorStore,
    *,
    proxy: ProxyConfig | None = None,
) -> PipelineResult:
```

(c) 把建立 client 的那行：

```python
    async with AsyncHTTPClient(policy) as client:
```

改為：

```python
    async with AsyncHTTPClient(policy, proxy=proxy) as client:
```

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/bin/pytest tests/test_async_pipeline.py -o addopts="" -q`
Expected: PASS（全部）

- [ ] **Step 5: 型別與 lint**

Run: `.venv/bin/basedpyright estimator_king/crawler/async_pipeline.py`
Expected: 0 errors
Run: `uvx ruff check estimator_king/crawler/async_pipeline.py tests/test_async_pipeline.py`
Expected: All checks passed

- [ ] **Step 6: Commit**

```bash
git add estimator_king/crawler/async_pipeline.py tests/test_async_pipeline.py
git commit -m "feat(crawler): thread proxy config through async_process_queue"
```

---

## Task 3: Async sitemap 鏈（sitemap + pipeline + cycle）

> 這三個檔案是一條 async 傳遞鏈：`enumerate_products` → `populate_queue_from_sitemap` → `run_crawl_cycle`。改一個就必須同步改其餘兩個與對應測試，否則 `await` 同步值會在 runtime `TypeError`。因此本 task 一次完成三檔 + 三測試，最後一起跑、一起 commit。

**Files:**
- Modify: `estimator_king/crawler/sitemap.py`
- Modify: `estimator_king/crawler/pipeline.py`
- Modify: `estimator_king/crawler/cycle.py`
- Test: `tests/test_sitemap.py`, `tests/test_pipeline.py`, `tests/test_crawl_cycle.py`

- [ ] **Step 1: 改寫 `sitemap.py` 為 async（走 AsyncHTTPClient）**

把 `estimator_king/crawler/sitemap.py` 整檔改為：

```python
"""Sitemap parsing for Shopify stores."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import List, Set
from urllib.parse import urljoin

from estimator_king.crawler.async_http_client import AsyncHTTPClient, AsyncHTTPClientError


# XML Namespace for sitemaps (standard)
SITEMAP_NS = {"sitemap": "http://www.sitemaps.org/schemas/sitemap/0.9"}


class SitemapError(Exception):
    """Base error for sitemap operations."""


class SitemapParseError(SitemapError):
    """Raised when XML parsing fails."""


class SitemapEnumerator:
    """Enumerates product URLs from Shopify sitemap hierarchy.

    Flow:
    1. Fetch /sitemap.xml (sitemapindex)
    2. Extract all <sitemap><loc> entries containing "products"
    3. For each products sitemap, fetch and extract <url><loc> entries
    4. Filter out /en/ locale paths
    5. Return stable-ordered (sorted), deduplicated list
    """

    def __init__(self, http_client: AsyncHTTPClient):
        """Initialize enumerator with an async HTTP client."""
        self.http_client = http_client

    async def enumerate_products(self, base_url: str) -> List[str]:
        """Enumerate all product URLs from a Shopify store.

        Args:
            base_url: Store base URL (e.g., "https://shop.example.com")

        Returns:
            Sorted, deduplicated list of product URLs (excluding /en/ paths)

        Raises:
            SitemapError: If sitemap parsing or fetching fails
        """
        sitemap_index_url = urljoin(base_url, "/sitemap.xml")

        try:
            products_sitemap_urls = await self._extract_products_sitemaps(sitemap_index_url)

            all_product_urls: Set[str] = set()
            for sitemap_url in products_sitemap_urls:
                urls = await self._extract_product_urls(sitemap_url)
                all_product_urls.update(urls)

            filtered = [url for url in all_product_urls if "/products/" in url and "/en/" not in url]
            return sorted(filtered)

        except (ET.ParseError, AsyncHTTPClientError) as e:
            raise SitemapError(
                f"Failed to enumerate products from {base_url}: {e}"
            ) from e

    async def _extract_products_sitemaps(self, sitemap_index_url: str) -> List[str]:
        """Extract all products sitemap URLs from sitemapindex."""
        try:
            text = await self.http_client.get(sitemap_index_url)
            root = ET.fromstring(text)
        except ET.ParseError as e:
            raise SitemapParseError(f"Failed to parse sitemapindex: {e}") from e
        except AsyncHTTPClientError as e:
            raise SitemapParseError(f"Failed to fetch sitemapindex: {e}") from e

        products_urls: List[str] = []

        for sitemap_elem in root.findall("sitemap:sitemap", SITEMAP_NS):
            loc_elem = sitemap_elem.find("sitemap:loc", SITEMAP_NS)
            if loc_elem is not None and loc_elem.text:
                url = loc_elem.text.strip()
                if "products" in url:
                    products_urls.append(url)

        return products_urls

    async def _extract_product_urls(self, sitemap_url: str) -> List[str]:
        """Extract all product URLs from a products sitemap."""
        try:
            text = await self.http_client.get(sitemap_url)
            root = ET.fromstring(text)
        except ET.ParseError as e:
            raise SitemapParseError(
                f"Failed to parse sitemap {sitemap_url}: {e}"
            ) from e
        except AsyncHTTPClientError as e:
            raise SitemapParseError(
                f"Failed to fetch sitemap {sitemap_url}: {e}"
            ) from e

        product_urls: List[str] = []

        for url_elem in root.findall("sitemap:url", SITEMAP_NS):
            loc_elem = url_elem.find("sitemap:loc", SITEMAP_NS)
            if loc_elem is not None and loc_elem.text:
                url = loc_elem.text.strip()
                product_urls.append(url)

        return product_urls
```

- [ ] **Step 2: 改寫 `pipeline.py` 的 `populate_queue_from_sitemap` 為 async**

在 `estimator_king/crawler/pipeline.py`，把 `populate_queue_from_sitemap` 的 `def` 改為 `async def`，並 `await` 列舉呼叫。只動兩處：

把：

```python
def populate_queue_from_sitemap(
    store: Store,
    repo: ProductStateRepository,
    enumerator: SitemapEnumerator,
) -> int:
```

改為：

```python
async def populate_queue_from_sitemap(
    store: Store,
    repo: ProductStateRepository,
    enumerator: SitemapEnumerator,
) -> int:
```

把：

```python
    # Step 1: enumerate product URLs from sitemap
    sitemap_urls = enumerator.enumerate_products(store.base_url)
```

改為：

```python
    # Step 1: enumerate product URLs from sitemap
    sitemap_urls = await enumerator.enumerate_products(store.base_url)
```

（其餘 repo 同步呼叫與 `enqueue_oldest_products` 不變。）

- [ ] **Step 3: 改寫 `cycle.py` 用 AsyncHTTPClient 跑 sitemap、傳 proxy、await**

在 `estimator_king/crawler/cycle.py`：

(a) import 區：移除同步 `HTTPClient`，改匯入 `AsyncHTTPClient`：

把：

```python
from estimator_king.crawler.http_client import HTTPClient
```

改為：

```python
from estimator_king.crawler.async_http_client import AsyncHTTPClient
```

(b) 把 `run_crawl_cycle` 內 `with ProductStateRepository(db_path) as repo:` 之後的整段（從建立 http_client 到 inactive sweep 之前）改寫——將 store 迴圈包進 `async with AsyncHTTPClient(...)`，`await` sitemap，並把 `proxy=config.proxy` 傳給 `async_process_queue`。完整新版函式主體：

```python
    with ProductStateRepository(db_path) as repo:
        async with AsyncHTTPClient(config.crawler, proxy=config.proxy) as sitemap_client:
            enumerator = SitemapEnumerator(http_client=sitemap_client)

            for store in config.stores:
                logger.info("Processing store %s", store.id)
                try:
                    new_count = await populate_queue_from_sitemap(store, repo, enumerator)
                    counters["discovered"] += new_count
                except Exception:
                    logger.exception("Sitemap failed for %s", store.id)
                    counters["errors"] += 1
                    continue

                if force_refetch:
                    for state in repo.list_active(store.id):
                        repo.enqueue_url(store.id, state.product_url)
                else:
                    remaining = max(0, config.crawler.max_products_per_run - new_count)
                    enqueue_oldest_products(store, repo, limit=remaining)

                try:
                    result = await async_process_queue(
                        store.id, store.base_url, config.crawler, repo, embedder, vector_store,
                        proxy=config.proxy)
                    counters["fetched_ok"] += result.processed
                    counters["created"] += result.created
                    counters["updated"] += result.updated
                    counters["skipped"] += result.sync_skipped
                    counters["errors"] += result.failed
                except Exception:
                    logger.exception("Queue processing failed for %s", store.id)
                    counters["errors"] += 1

        try:
            inactive_result = mark_inactive_products(
                repo, vector_store,
                failure_threshold=config.crawler.inactive_failure_threshold,
                miss_threshold=config.crawler.inactive_sitemap_miss_threshold,
            )
            counters["inactive"] += inactive_result.marked_inactive
        except Exception:
            logger.exception("Inactive sweep failed")
            counters["errors"] += 1

    return counters
```

（注意：inactive sweep 移到 `async with sitemap_client` 區塊**之外**、仍在 `with repo` 之內——sitemap client 在 sweep 前關閉，sweep 不需要它。）

- [ ] **Step 4: 改寫 `tests/test_sitemap.py` 為 async**

把 `tests/test_sitemap.py` 整檔改為下列內容（保留純 ET 解析的 `TestSitemapEnumeratorParsingFixtures` 不變；整合與錯誤測試改用 async fake client + `asyncio.run`；新增 4xx `ClientError` 包裝測試）：

```python
"""Tests for Shopify sitemap enumeration."""

import asyncio
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from estimator_king.crawler.async_http_client import AsyncHTTPClientError, ClientError
from estimator_king.crawler.sitemap import (
    SitemapEnumerator,
    SitemapError,
    SitemapParseError,
)


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def sitemap_index_xml(fixtures_dir: Path) -> bytes:
    with open(fixtures_dir / "sitemap_index.xml", "rb") as f:
        return f.read()


@pytest.fixture
def sitemap_products_1_xml(fixtures_dir: Path) -> bytes:
    with open(fixtures_dir / "sitemap_products_1.xml", "rb") as f:
        return f.read()


@pytest.fixture
def sitemap_products_2_xml(fixtures_dir: Path) -> bytes:
    with open(fixtures_dir / "sitemap_products_2.xml", "rb") as f:
        return f.read()


class FakeAsyncClient:
    """Minimal async stand-in for AsyncHTTPClient.get used by SitemapEnumerator.

    `router(url)` returns the XML text (str) for that URL, or raises.
    """

    def __init__(self, router):
        self._router = router
        self.call_urls: list[str] = []

    async def get(self, url: str) -> str:
        self.call_urls.append(url)
        return self._router(url)


def _fixtures_router(index_xml: bytes, p1_xml: bytes, p2_xml: bytes):
    def router(url: str) -> str:
        if url.endswith("/sitemap.xml"):
            return index_xml.decode("utf-8")
        elif "products_1" in url:
            return p1_xml.decode("utf-8")
        elif "products_2" in url:
            return p2_xml.decode("utf-8")
        raise AssertionError(f"Unexpected URL: {url}")

    return router


class TestSitemapEnumeratorParsingFixtures:
    """Test parsing of fixture-based sitemaps (pure ET, no client)."""

    def test_parse_sitemap_index_fixture(self, sitemap_index_xml: bytes):
        root = ET.fromstring(sitemap_index_xml)
        ns = {"sitemap": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        sitemaps = root.findall("sitemap:sitemap", ns)
        assert len(sitemaps) == 4

        locs = [
            elem.find("sitemap:loc", ns).text
            for elem in sitemaps
            if elem.find("sitemap:loc", ns) is not None
        ]
        assert len(locs) == 4
        assert any("products_1" in loc for loc in locs)
        assert any("products_2" in loc for loc in locs)
        assert any("pages_1" in loc for loc in locs)
        assert any("collections_1" in loc for loc in locs)

    def test_parse_sitemap_products_1_fixture(self, sitemap_products_1_xml: bytes):
        root = ET.fromstring(sitemap_products_1_xml)
        ns = {"sitemap": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        urls = root.findall("sitemap:url", ns)
        assert len(urls) == 5

        locs = [
            elem.find("sitemap:loc", ns).text
            for elem in urls
            if elem.find("sitemap:loc", ns) is not None
        ]
        assert "https://shop.example.com/products/item-001" in locs
        assert "https://shop.example.com/products/item-002" in locs
        assert "https://shop.example.com/en/products/item-001-en" in locs

    def test_parse_sitemap_products_2_fixture(self, sitemap_products_2_xml: bytes):
        root = ET.fromstring(sitemap_products_2_xml)
        ns = {"sitemap": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        urls = root.findall("sitemap:url", ns)
        assert len(urls) == 5

        locs = [
            elem.find("sitemap:loc", ns).text
            for elem in urls
            if elem.find("sitemap:loc", ns) is not None
        ]
        assert "https://shop.example.com/products/item-005" in locs
        assert "https://shop.example.com/products/item-006" in locs


class TestSitemapEnumeratorIntegration:
    """Integration tests with an async fake HTTP client."""

    def _enumerate(self, index_xml, p1_xml, p2_xml):
        client = FakeAsyncClient(_fixtures_router(index_xml, p1_xml, p2_xml))
        enumerator = SitemapEnumerator(http_client=client)
        urls = asyncio.run(enumerator.enumerate_products("https://shop.example.com"))
        return client, urls

    def test_enumerate_products_with_mocked_http(
        self, sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml
    ):
        _, urls = self._enumerate(sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml)
        assert isinstance(urls, list)
        assert len(urls) > 0
        assert all(isinstance(url, str) for url in urls)

    def test_enumerate_products_excludes_en_paths(
        self, sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml
    ):
        _, urls = self._enumerate(sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml)
        assert "/en/" not in "\n".join(urls)
        assert all("/en/" not in url for url in urls)

    def test_enumerate_products_returns_sorted(
        self, sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml
    ):
        _, urls = self._enumerate(sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml)
        assert urls == sorted(urls)

    def test_enumerate_products_deduplicates(
        self, sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml
    ):
        _, urls = self._enumerate(sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml)
        assert len(urls) == len(set(urls))

    def test_enumerate_products_includes_query_params(
        self, sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml
    ):
        _, urls = self._enumerate(sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml)
        assert any("variant=" in url for url in urls)

    def test_enumerate_products_skips_non_products_sitemaps(
        self, sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml
    ):
        client, _ = self._enumerate(sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml)
        assert any("products" in url for url in client.call_urls)
        assert not any("pages_1" in url for url in client.call_urls)
        assert not any("collections_1" in url for url in client.call_urls)


class TestSitemapEnumeratorErrorHandling:
    """Test error handling in sitemap enumeration."""

    def test_parse_error_on_malformed_index(self):
        client = FakeAsyncClient(lambda url: "<not-valid-xml>")
        enumerator = SitemapEnumerator(http_client=client)

        with pytest.raises(SitemapError):
            asyncio.run(enumerator.enumerate_products("https://shop.example.com"))

    def test_parse_error_on_malformed_products_sitemap(self, sitemap_index_xml):
        def router(url: str) -> str:
            if url.endswith("/sitemap.xml"):
                return sitemap_index_xml.decode("utf-8")
            return "<not-closed-xml>"

        client = FakeAsyncClient(router)
        enumerator = SitemapEnumerator(http_client=client)

        with pytest.raises(SitemapParseError):
            asyncio.run(enumerator.enumerate_products("https://shop.example.com"))

    def test_http_error_on_fetch_failure(self):
        def router(url: str) -> str:
            raise AsyncHTTPClientError("Connection failed")

        client = FakeAsyncClient(router)
        enumerator = SitemapEnumerator(http_client=client)

        with pytest.raises(SitemapError):
            asyncio.run(enumerator.enumerate_products("https://shop.example.com"))

    def test_client_error_4xx_wraps_to_sitemap_error(self):
        def router(url: str) -> str:
            raise ClientError(url, status_code=404)

        client = FakeAsyncClient(router)
        enumerator = SitemapEnumerator(http_client=client)

        with pytest.raises(SitemapError):
            asyncio.run(enumerator.enumerate_products("https://shop.example.com"))

    def test_empty_sitemapindex_returns_empty_list(self):
        empty_index = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
</sitemapindex>
"""
        client = FakeAsyncClient(lambda url: empty_index)
        enumerator = SitemapEnumerator(http_client=client)
        urls = asyncio.run(enumerator.enumerate_products("https://shop.example.com"))

        assert urls == []

    def test_sitemapindex_without_products_returns_empty_list(self):
        no_products_index = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap>
    <loc>https://shop.example.com/sitemap_pages_1.xml</loc>
  </sitemap>
</sitemapindex>
"""
        client = FakeAsyncClient(lambda url: no_products_index)
        enumerator = SitemapEnumerator(http_client=client)
        urls = asyncio.run(enumerator.enumerate_products("https://shop.example.com"))

        assert urls == []


class TestSitemapEnumeratorRealFixtures:
    """Tests using the actual fixture files from disk."""

    def test_enumerate_with_real_fixtures(
        self, sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml
    ):
        client = FakeAsyncClient(
            _fixtures_router(sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml)
        )
        enumerator = SitemapEnumerator(http_client=client)
        urls = asyncio.run(enumerator.enumerate_products("https://shop.example.com"))

        expected_urls = [
            "https://shop.example.com/products/item-001",
            "https://shop.example.com/products/item-002",
            "https://shop.example.com/products/item-003",
            "https://shop.example.com/products/item-004?variant=blue",
            "https://shop.example.com/products/item-004?variant=red",
            "https://shop.example.com/products/item-005",
            "https://shop.example.com/products/item-006",
            "https://shop.example.com/products/item-007",
        ]

        assert sorted(urls) == sorted(expected_urls)
        assert all("/en/" not in url for url in urls)
```

> 註：`ClientError` 是 `AsyncHTTPClientError` 子類，定義於 `async_http_client.py`。`SitemapParseError` 是 `SitemapError` 子類，故 `test_*_4xx*` 與 `test_http_error*` 以 `pytest.raises(SitemapError)` 斷言。

- [ ] **Step 5: 改寫 `tests/test_pipeline.py` 的 FakeEnumerator 與呼叫為 async**

修改 `tests/test_pipeline.py`：

頂端新增 `import asyncio`（放在現有 import 之上）：

```python
import asyncio
from datetime import datetime, timedelta, timezone
```

把 `FakeEnumerator.enumerate_products` 改 async、把測試呼叫包 `asyncio.run`：

```python
class FakeEnumerator:
    def __init__(self, urls):
        self._urls = urls

    async def enumerate_products(self, base_url):
        return self._urls


def test_populate_enqueues_only_new_urls(repo):
    repo.upsert(_state(1, None))  # existing
    enum = FakeEnumerator(["https://x/products/1", "https://x/products/2"])

    new_count = asyncio.run(populate_queue_from_sitemap(_store(), repo, enum))

    assert new_count == 1
    assert [e["product_url"] for e in repo.peek_all("hololive")] == ["https://x/products/2"]
```

- [ ] **Step 6: 改寫 `tests/test_crawl_cycle.py` 的 populate patch 為 awaitable，並新增 sitemap 失敗測試**

修改 `tests/test_crawl_cycle.py`：

(a) import：把 `from unittest.mock import patch` 改為含 `AsyncMock`，並匯入 `SitemapError`：

```python
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from estimator_king.config_schema import AppConfig, CrawlerPolicy, Store
from estimator_king.crawler.cycle import run_crawl_cycle
from estimator_king.crawler.sitemap import SitemapError
```

(b) 兩個既有測試裡的 `patch("estimator_king.crawler.cycle.populate_queue_from_sitemap", return_value=0)` 改為 awaitable mock：

```python
    with patch("estimator_king.crawler.cycle.populate_queue_from_sitemap", new=AsyncMock(return_value=0)), \
```

（兩處皆改。其餘 `enqueue_oldest_products`、`async_process_queue`、`mark_inactive_products` 的 patch 不變；`fake_proc` 已是 async，相容新增的 `proxy=` kwarg，因為它收 `*a, **k`。）

(c) 新增測試，驗證 sitemap 失敗時 cycle 計入 per-store error 並 continue（§3.5 行為）：

```python
def test_sitemap_failure_counts_error_and_continues(db_path):
    cfg = _config()
    with patch("estimator_king.crawler.cycle.populate_queue_from_sitemap",
               new=AsyncMock(side_effect=SitemapError("boom"))), \
         patch("estimator_king.crawler.cycle.enqueue_oldest_products") as enq, \
         patch("estimator_king.crawler.cycle.async_process_queue") as proc, \
         patch("estimator_king.crawler.cycle.mark_inactive_products") as inactive:
        async def fake_proc(*a, **k):
            from estimator_king.crawler.async_pipeline import PipelineResult
            return PipelineResult()
        proc.side_effect = fake_proc

        counters = asyncio.run(run_crawl_cycle(cfg, db_path, FakeEmbedder(), FakeVectorStore()))

    assert counters["errors"] >= 1
    assert inactive.call_count == 1  # cross-store sweep still runs once
    enq.assert_not_called()  # store skipped via continue before budget enqueue
    proc.assert_not_called()  # store skipped via continue before queue processing
```

- [ ] **Step 7: 執行三個測試檔確認通過**

Run: `.venv/bin/pytest tests/test_sitemap.py tests/test_pipeline.py tests/test_crawl_cycle.py -o addopts="" -q`
Expected: PASS（全部）

- [ ] **Step 8: 型別與 lint**

Run: `.venv/bin/basedpyright estimator_king/crawler/sitemap.py estimator_king/crawler/pipeline.py estimator_king/crawler/cycle.py`
Expected: 0 errors
Run: `uvx ruff check estimator_king/crawler/sitemap.py estimator_king/crawler/pipeline.py estimator_king/crawler/cycle.py tests/test_sitemap.py tests/test_pipeline.py tests/test_crawl_cycle.py`
Expected: All checks passed

- [ ] **Step 9: Commit**

```bash
git add estimator_king/crawler/sitemap.py estimator_king/crawler/pipeline.py estimator_king/crawler/cycle.py tests/test_sitemap.py tests/test_pipeline.py tests/test_crawl_cycle.py
git commit -m "feat(crawler): migrate sitemap enumeration to async via AsyncHTTPClient"
```

---

## Task 4: shopify.py 改用 Protocol、移除 requests/HTTPClient import

**Files:**
- Modify: `estimator_king/crawler/shopify.py`
- Test: `tests/test_shopify.py`（既有，應仍通過，不需改）

- [ ] **Step 1: 修改 `shopify.py` 的 import 與型別註解**

在 `estimator_king/crawler/shopify.py`：

(a) 把頂端 import 區：

```python
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import cast

import requests

from .html_extractor import extract_detail_sections as extract_html_details
from .http_client import HTTPClient
from .snapshot import ProductSnapshot, ProductVariant, compute_content_hash

logger = logging.getLogger(__name__)
```

改為：

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

(b) 把 `_raise_for_status` 的參數型別由 `requests.Response` 改為 `_HTTPResponse`：

```python
def _raise_for_status(url: str, resp: _HTTPResponse) -> None:
    status = int(getattr(resp, "status_code", 0) or 0)
    if status < 200 or status >= 300:
        raise ShopifyHTTPError(url, status_code=status)
```

(c) 把 `fetch_product` 的 `http_client` 型別由 `HTTPClient` 改為 `_HTTPGetter`：

```python
def fetch_product(url: str, http_client: _HTTPGetter) -> ProductSnapshot:
```

（函式內以 `getattr(json_resp, "text", "")`、`getattr(html_resp, "text", "")`、`_raise_for_status` 取值的既有寫法**不變**。）

- [ ] **Step 2: 執行 shopify 相關測試確認仍通過**

Run: `.venv/bin/pytest tests/test_shopify.py tests/test_shopify_logging.py tests/test_async_pipeline.py -o addopts="" -q`
Expected: PASS（`test_shopify.py` 用本地 `_Resp`（具 `status_code`/`text`）與 `Mock()`，結構相容新 Protocol）

- [ ] **Step 3: 型別與 lint**

Run: `.venv/bin/basedpyright estimator_king/crawler/shopify.py`
Expected: 0 errors
Run: `uvx ruff check estimator_king/crawler/shopify.py`
Expected: All checks passed

- [ ] **Step 4: Commit**

```bash
git add estimator_king/crawler/shopify.py
git commit -m "refactor(crawler): type shopify http_client via Protocol, drop requests/HTTPClient"
```

---

## Task 5: 刪除死碼 `http_client.py` 及其測試

**Files:**
- Delete: `estimator_king/crawler/http_client.py`
- Delete: `tests/test_http_client.py`
- Delete: `tests/test_http_client_logging.py`

- [ ] **Step 1: 確認已無任何 import 殘留**

Run:
```bash
grep -rn "from estimator_king.crawler.http_client\|from \.http_client\|crawler\.http_client import\|import http_client" estimator_king tests
```
Expected: 只剩 `tests/test_http_client.py` 與 `tests/test_http_client_logging.py` 自身（這兩個檔即將刪除）。若 production 程式碼仍有任何一筆，**停止**並回頭修正（Task 3/4 漏改）。

也確認 `estimator_king/crawler/__init__.py` 沒有 re-export：
```bash
grep -n "http_client\|HTTPClient" estimator_king/crawler/__init__.py
```
Expected: 無輸出。

- [ ] **Step 2: 刪除三個檔案**

```bash
git rm estimator_king/crawler/http_client.py tests/test_http_client.py tests/test_http_client_logging.py
```

- [ ] **Step 3: 全套件型別檢查（確認刪除無懸空參照）**

Run: `.venv/bin/basedpyright estimator_king`
Expected: 0 errors（production 零錯誤閘門）

- [ ] **Step 4: 跑爬蟲相關測試全綠**

Run: `.venv/bin/pytest tests/ -o addopts="" -q`
Expected: PASS（全套件；確認刪除未波及其他測試的 collection）

- [ ] **Step 5: Commit**

```bash
git add -u estimator_king/crawler/http_client.py tests/test_http_client.py tests/test_http_client_logging.py
git commit -m "chore(crawler): remove now-dead sync HTTPClient and its tests"
```

---

## Task 6: runner.py 優雅關閉 + 第二次強退

**Files:**
- Modify: `estimator_king/bot/runner.py`
- Test: `tests/test_runner_shutdown.py`（新建）

- [ ] **Step 1: 寫關閉行為測試（先建測試檔）**

新建 `tests/test_runner_shutdown.py`：

```python
import asyncio

import pytest

from estimator_king.bot import runner


class _FakeBot:
    def __init__(self):
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_shutdown_cancels_scheduler_then_closes_bot():
    async def long_running():
        await asyncio.sleep(3600)

    scheduler_task = asyncio.create_task(long_running())
    bot = _FakeBot()
    shutdowner = runner._Shutdowner(scheduler_task, bot)

    await shutdowner.shutdown()

    assert scheduler_task.cancelled()
    assert bot.closed is True


@pytest.mark.asyncio
async def test_first_signal_requests_shutdown_second_forces_exit():
    async def long_running():
        await asyncio.sleep(3600)

    scheduler_task = asyncio.create_task(long_running())
    bot = _FakeBot()
    exits: list[int] = []
    shutdowner = runner._Shutdowner(scheduler_task, bot, force_exit=exits.append)

    # First signal: schedules graceful shutdown, no force exit.
    shutdowner.handle_signal()
    assert shutdowner._requested is True
    assert exits == []

    # Second signal: forces exit with code 130.
    shutdowner.handle_signal()
    assert exits == [130]

    # Let the scheduled graceful shutdown task settle.
    await asyncio.sleep(0)
    scheduler_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/bin/pytest tests/test_runner_shutdown.py -o addopts="" -q`
Expected: FAIL（`runner._Shutdowner` 尚不存在 → `AttributeError`）

- [ ] **Step 3: 在 `runner.py` 實作 `_Shutdowner` 與強退**

修改 `estimator_king/bot/runner.py`：

(a) 頂端 import 補 `os`（與既有 import 並列）：

```python
import asyncio
import logging
import os
import signal
import sys
from typing import Optional
```

(b) 在模組層級（`_background_tasks` 定義之後）新增可注入的強退函式與 `_Shutdowner` 類別：

```python
def _force_exit(code: int) -> None:  # pragma: no cover - replaced via injection in tests
    os._exit(code)


class _Shutdowner:
    """Two-stage shutdown: first signal cancels the scheduler and closes the
    bot gracefully; a second signal forces an immediate exit (escape hatch for
    in-flight blocking work that cannot be cancelled cooperatively)."""

    def __init__(self, scheduler_task, bot, *, force_exit=_force_exit) -> None:
        self._scheduler_task = scheduler_task
        self._bot = bot
        self._force_exit = force_exit
        self._requested = False

    def handle_signal(self) -> None:
        if self._requested:
            logger.warning("Forced shutdown (second interrupt)")
            self._force_exit(130)
            return
        self._requested = True
        logger.info("Shutdown requested; press Ctrl+C again to force quit")
        task = asyncio.create_task(self.shutdown())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    async def shutdown(self) -> None:
        logger.info("Shutting down bot...")
        self._scheduler_task.cancel()
        try:
            await self._scheduler_task
        except asyncio.CancelledError:
            pass
        await self._bot.close()
```

(c) 在 `run_bot` 內，把原本的 `shutdown()` closure 與 signal handler 註冊：

```python
    async def shutdown() -> None:
        logger.info("Shutting down bot...")
        await bot.close()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))
```

改為：

```python
    shutdowner = _Shutdowner(scheduler_task, bot)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdowner.handle_signal)
```

（`scheduler_task` 已在前面 `asyncio.create_task(scheduler.run_forever())` 建立，於 `_Shutdowner` 可見。）

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/bin/pytest tests/test_runner_shutdown.py tests/test_runner_logging.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 5: 型別與 lint**

Run: `.venv/bin/basedpyright estimator_king/bot/runner.py`
Expected: 0 errors
Run: `uvx ruff check estimator_king/bot/runner.py tests/test_runner_shutdown.py`
Expected: All checks passed

- [ ] **Step 6: Commit**

```bash
git add estimator_king/bot/runner.py tests/test_runner_shutdown.py
git commit -m "feat(bot): graceful shutdown cancels scheduler, second interrupt force-exits"
```

---

## Task 7: scheduler cancellation 契約測試（無 production 改動）

> `scheduler.py` 的 `run_once` 以 `except Exception` 捕捉，`CancelledError` 繼承 `BaseException` 不被吃掉，故取消能乾淨穿透 `run_forever`。本 task 不改 production 程式碼，只新增一個 regression 測試鎖定此契約。

**Files:**
- Test: `tests/test_scheduler.py`（擴充）

- [ ] **Step 1: 新增 cancellation 傳遞測試**

在 `tests/test_scheduler.py` 頂端補 import：

```python
from estimator_king.config_schema import AppConfig, CrawlerPolicy, Store
```

在檔案末端新增測試：

```python
def _schedulable_config():
    return AppConfig(
        stores=[Store(id="hololive", base_url="https://x", sitemap_url="https://x/sm.xml")],
        crawler=CrawlerPolicy(),
    )


@pytest.mark.asyncio
async def test_run_forever_propagates_cancellation(monkeypatch):
    entered = asyncio.Event()

    async def fake_cycle(*a, **k):
        entered.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr("estimator_king.bot.scheduler.run_crawl_cycle", fake_cycle)
    sched = CrawlScheduler(
        config=_schedulable_config(), db_path="db", embedder=object(), vector_store=object()
    )

    task = asyncio.create_task(sched.run_forever())
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert sched._running is False
```

- [ ] **Step 2: 執行測試確認通過（行為已存在）**

Run: `.venv/bin/pytest tests/test_scheduler.py -o addopts="" -q`
Expected: PASS（含新測試；無需改 production 程式碼）

- [ ] **Step 3: Lint**

Run: `uvx ruff check tests/test_scheduler.py`
Expected: All checks passed

- [ ] **Step 4: Commit**

```bash
git add tests/test_scheduler.py
git commit -m "test(bot): lock in scheduler cancellation-propagation contract"
```

---

## 最終整體驗證（所有 task 完成後）

- [ ] **全套件測試**

Run: `.venv/bin/pytest tests/ -o addopts="" -q`
Expected: PASS（全綠）

- [ ] **Production 型別零錯誤閘門**

Run: `.venv/bin/basedpyright estimator_king`
Expected: 0 errors

- [ ] **全域 lint**

Run: `uvx ruff check estimator_king tests`
Expected: All checks passed

- [ ] **確認死碼確實移除**

Run: `git ls-files estimator_king/crawler/http_client.py tests/test_http_client.py tests/test_http_client_logging.py`
Expected: 無輸出（三檔皆已自版控移除）
