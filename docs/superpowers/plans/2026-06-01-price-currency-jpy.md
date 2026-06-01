# 強制並驗證爬蟲價格幣別為 JPY Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓爬蟲對 Shopify product `.json` 的請求一律以 JPY 回傳價格，並在解析時防禦性驗證幣別確為 JPY，杜絕非 JPY 價格被當成 JPY 寫入。

**Architecture:** 改動集中在價格的唯一來源 `estimator_king/crawler/shopify.py`。`fetch_product` 在建構 JSON 請求 URL 時以 `urlsplit` 正規化並附加 `?currency=JPY`；`_build_snapshot_from_product_json` 對每個 variant 驗證 `price_currency == "JPY"`，不符即 `raise ShopifyJSONError`（被既有 pipeline 當成一次 fetch 失敗）。資料模型、content hash、HTTP client 皆不變。

**Tech Stack:** Python 3、aiohttp（既有 HTTP client，本計畫不動）、pytest、basedpyright、ruff。

---

## 驗證指令（每個 Task 末尾使用）

- Type check：`.venv/bin/basedpyright estimator_king/`（production code 須 0 errors）
- Lint：`uvx ruff check estimator_king/ tests/`
- 本計畫相關測試（單檔）：`.venv/bin/python -m pytest tests/test_shopify.py -v -o addopts=""`

> 注意：`pytest.ini` 設了 `addopts = --cov=...`，單檔測試必須加 `-o addopts=""` 覆寫，**不要**用 `-p no:cov`。

## 提交規範

依專案規範，所有 commit **必須**透過 `git-master` 技能執行，以具體檔案路徑加入（**禁止** `git add -A`/`git add .`）。各 Task 的 commit 步驟已列出「要加入的具體檔案」與「commit message」，執行時交由 git-master 規劃 atomic commit。

## File Structure

- `estimator_king/crawler/shopify.py`（Modify）— 新增 `_FORCE_CURRENCY` 常數、`fetch_product` URL 正規化與強制幣別、`_build_snapshot_from_product_json` 幣別驗證。
- `tests/test_shopify.py`（Modify）— 更新 `_FakeAsyncClient.get` 路由與 `_product_json` helper；新增 URL 強制與幣別驗證測試。
- `tests/fixtures/product_json_hololive.json`（Modify）— 兩個 variant 補 `price_currency`。
- `tests/fixtures/product_json_vspo.json`（Modify）— variant 補 `price_currency`。
- `CLAUDE.md`、`docs/local-runbook.md`、`docs/ops-runbook.md`（Modify）— 文件說明 JPY 強制與舊資料修復。

---

## Task 1：在 `fetch_product` 強制 `?currency=JPY` 並正規化 URL

**Files:**
- Modify: `estimator_king/crawler/shopify.py`（`fetch_product` 在 131-142；imports 在 1-13）
- Modify: `tests/test_shopify.py`（`_FakeAsyncClient.get` 在 24-38；imports 在 8-21）

> 本 Task 不加入幣別驗證，因此既有 fixtures（尚無 `price_currency`）仍能通過解析，測試套件在本 Task commit 後維持綠燈。

- [ ] **Step 1：更新 fake client 路由以容忍 query string（先改測試基礎設施，避免後續誤路由）**

在 `tests/test_shopify.py` 最上方 import 區（檔案開頭 import 區塊，`import json` 之後）新增：

```python
from urllib.parse import urlsplit
```

將 `_FakeAsyncClient.get`（目前第 31-38 行）的路由判斷由 `url.endswith(".json")` 改為對 path 判斷，使附加 `?currency=JPY` 後的 JSON URL 仍正確路由：

```python
    async def get(self, url: str) -> str:
        if urlsplit(url).path.endswith(".json"):
            if self._json_exc is not None:
                raise self._json_exc
            return self._json_text
        if self._html_exc is not None:
            raise self._html_exc
        return self._html_text
```

- [ ] **Step 2：新增 URL 強制測試（記錄被請求的 URL）**

在 `tests/test_shopify.py` 末尾新增。此測試使用一個會記錄所有被請求 URL 的 fake client，並回傳既有 hololive fixture，斷言 JSON 與 HTML 請求 URL 的完整字串：

```python
class _RecordingAsyncClient:
    def __init__(self, *, json_text: str, html_text: str):
        self._json_text = json_text
        self._html_text = html_text
        self.requested_urls: list[str] = []

    async def get(self, url: str) -> str:
        self.requested_urls.append(url)
        if urlsplit(url).path.endswith(".json"):
            return self._json_text
        return self._html_text


def test_fetch_product_forces_jpy_currency_on_json_url():
    json_text = _read_fixture("product_json_hololive.json")
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _RecordingAsyncClient(json_text=json_text, html_text=html_text)

    asyncio.run(fetch_product("https://shop.hololivepro.com/products/sample", client))

    assert (
        "https://shop.hololivepro.com/products/sample.json?currency=JPY"
        in client.requested_urls
    )
    assert "https://shop.hololivepro.com/products/sample" in client.requested_urls


def test_fetch_product_strips_existing_query_before_forcing_currency():
    json_text = _read_fixture("product_json_hololive.json")
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _RecordingAsyncClient(json_text=json_text, html_text=html_text)

    asyncio.run(
        fetch_product("https://shop.hololivepro.com/products/sample.json?foo=bar", client)
    )

    assert (
        "https://shop.hololivepro.com/products/sample.json?currency=JPY"
        in client.requested_urls
    )
    assert "https://shop.hololivepro.com/products/sample" in client.requested_urls
```

- [ ] **Step 3：執行新測試，確認失敗**

Run: `.venv/bin/python -m pytest tests/test_shopify.py::test_fetch_product_forces_jpy_currency_on_json_url tests/test_shopify.py::test_fetch_product_strips_existing_query_before_forcing_currency -v -o addopts=""`
Expected: FAIL（JSON URL 仍為 `.../sample.json`，不含 `?currency=JPY`；帶 query 的輸入會產生畸形 URL）。

- [ ] **Step 4：新增模組常數 `_FORCE_CURRENCY`**

在 `estimator_king/crawler/shopify.py` 的 `logger = logging.getLogger(__name__)`（第 13 行）之後新增：

```python
# Shopify Markets returns the JSON `.json` endpoint's prices in a geo/locale-detected
# currency. Pin every product fetch to JPY via the `currency` query param, which
# overrides cookie/geo detection. The system is JPY-only (ProductItem.price_jpy).
_FORCE_CURRENCY = "JPY"
```

- [ ] **Step 5：新增 `urlsplit`/`urlunsplit` import**

在 `estimator_king/crawler/shopify.py` import 區（第 2-8 行附近，與其他 stdlib import 並列）新增：

```python
from urllib.parse import urlsplit, urlunsplit
```

- [ ] **Step 6：改寫 `fetch_product` 的 URL 建構**

將 `fetch_product`（目前第 131-142 行）整段替換為：

```python
async def fetch_product(url: str, client: _AsyncGetter) -> ProductSnapshot:
    raw = url.strip()
    if not raw:
        raise ValueError("url must be a non-empty string")
    parts = urlsplit(raw)
    path = parts.path
    if path.endswith(".json"):
        path = path[: -len(".json")]
    path = path.rstrip("/")
    # Drop any existing query/fragment so we never build a malformed double-query URL.
    canonical_url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    json_url = f"{canonical_url}.json?currency={_FORCE_CURRENCY}"

    json_text = await client.get(json_url)
    html_text = await client.get(canonical_url)
    return await asyncio.to_thread(_build_snapshot, json_text, html_text, canonical_url)
```

- [ ] **Step 7：執行新測試，確認通過**

Run: `.venv/bin/python -m pytest tests/test_shopify.py::test_fetch_product_forces_jpy_currency_on_json_url tests/test_shopify.py::test_fetch_product_strips_existing_query_before_forcing_currency -v -o addopts=""`
Expected: PASS

- [ ] **Step 8：執行整個 shopify 測試檔，確認既有測試仍綠燈**

Run: `.venv/bin/python -m pytest tests/test_shopify.py -v -o addopts=""`
Expected: 全數 PASS（含 `test_fetch_product_accepts_url_with_json_suffix`、http error 等，路由改動相容）。

- [ ] **Step 9：型別與 lint**

Run: `.venv/bin/basedpyright estimator_king/`
Expected: production code 0 errors。
Run: `uvx ruff check estimator_king/ tests/`
Expected: 無錯誤。

- [ ] **Step 10：Commit（透過 git-master）**

加入檔案：`estimator_king/crawler/shopify.py`、`tests/test_shopify.py`
commit message：`feat(crawl): force JPY currency on shopify product json fetch`

---

## Task 2：驗證 `price_currency == "JPY"` 並更新測試資產

**Files:**
- Modify: `estimator_king/crawler/shopify.py`（`_build_snapshot_from_product_json` variant 迴圈在 96-119，price 驗證在 108-109）
- Modify: `tests/fixtures/product_json_hololive.json`（variants 在 6-19）
- Modify: `tests/fixtures/product_json_vspo.json`（variant 在 6-13）
- Modify: `tests/test_shopify.py`（`_product_json` helper 在 214-222）

> 幣別驗證一旦加入，所有走成功路徑、會進入 variant 解析的測試都會要求 `price_currency`。因此本 Task 在同一 commit 內**先更新 fixtures 與 helper**，再加入驗證，確保測試套件始終綠燈。

- [ ] **Step 1：新增幣別驗證測試（會失敗）**

在 `tests/test_shopify.py` 末尾新增。沿用既有 `fetch_product` + `json.dumps({"product": ...})` 模式：

```python
@pytest.mark.parametrize(
    "currency_value",
    ["USD", "EUR", 123, None],  # wrong currency, non-str, and missing (None)
)
def test_fetch_product_rejects_non_jpy_price_currency(currency_value):
    html_text = _read_fixture("product_html_hololive_basic.html")
    variant: dict[str, object] = {"id": 1, "title": "T", "price": "1000", "sku": None}
    if currency_value is not None:
        variant["price_currency"] = currency_value
    product = {"id": 123, "title": "X", "body_html": "", "variants": [variant]}
    client = _mk_client(
        json_text=json.dumps({"product": product}), html_text=html_text
    )
    with pytest.raises(ShopifyJSONError):
        _ = asyncio.run(fetch_product("https://shop.hololivepro.com/products/x", client))


def test_fetch_product_accepts_jpy_price_currency():
    html_text = _read_fixture("product_html_none.html")
    variant = {
        "id": 1,
        "title": "T",
        "price": "1000",
        "sku": None,
        "price_currency": "JPY",
    }
    product = {"id": 123, "title": "X", "body_html": "", "variants": [variant]}
    client = _mk_client(
        json_text=json.dumps({"product": product}), html_text=html_text
    )
    snapshot = asyncio.run(fetch_product("https://shop.hololivepro.com/products/x", client))
    assert len(snapshot.variants) == 1
    assert snapshot.variants[0].price == "1000"
```

- [ ] **Step 2：執行新測試，確認 reject 測試失敗**

Run: `.venv/bin/python -m pytest tests/test_shopify.py::test_fetch_product_rejects_non_jpy_price_currency -v -o addopts=""`
Expected: FAIL（尚未驗證幣別，非 JPY 不會 raise）。

> `test_fetch_product_accepts_jpy_price_currency` 此時即會 PASS（尚無驗證），屬正常。

- [ ] **Step 3：在 hololive fixture 兩個 variant 補上 `price_currency`**

修改 `tests/fixtures/product_json_hololive.json`，兩個 variant 各補 `"price_currency": "JPY"`（注意在 `sku` 行加逗號）：

```json
{
  "product": {
    "id": 1000000001,
    "title": "Hololive Sample Product",
    "body_html": "<p>これは説明です。</p>",
    "variants": [
      {
        "id": 2000000001,
        "title": "Default Title",
        "price": "3500",
        "sku": "Holo-001",
        "price_currency": "JPY"
      },
      {
        "id": 2000000002,
        "title": "Limited",
        "price": "4500",
        "sku": "Holo-001-L",
        "price_currency": "JPY"
      }
    ]
  }
}
```

- [ ] **Step 4：在 vspo fixture variant 補上 `price_currency`**

修改 `tests/fixtures/product_json_vspo.json`：

```json
{
  "product": {
    "id": 1000000002,
    "title": "VSPO Sample Product",
    "body_html": "<div>Some intro text.</div>",
    "variants": [
      {
        "id": 3000000001,
        "title": "One Size",
        "price": "2500",
        "sku": "VSPO-ABC",
        "price_currency": "JPY"
      }
    ]
  }
}
```

- [ ] **Step 5：在 `_product_json` helper variant 補上 `price_currency`**

修改 `tests/test_shopify.py` 的 `_product_json`（目前第 219 行 variant dict）：

```python
def _product_json(**extra) -> str:
    product = {
        "id": 123,
        "title": "Test Product",
        "body_html": "<p>desc</p>",
        "variants": [
            {
                "id": 1,
                "title": "グッズ / Item A",
                "price": "500",
                "sku": None,
                "price_currency": "JPY",
            }
        ],
    }
    product.update(extra)
    return json.dumps({"product": product})
```

- [ ] **Step 6：在 `_build_snapshot_from_product_json` 加入幣別驗證**

修改 `estimator_king/crawler/shopify.py`，在 variant 迴圈中 price 字串驗證（目前第 108-109 行）之後、`sku` 驗證（第 110 行）之前插入：

```python
        if not isinstance(price, str):
            raise ShopifyJSONError("shopify variant.price missing or not str")
        price_currency = v_obj.get("price_currency")
        if price_currency != _FORCE_CURRENCY:
            raise ShopifyJSONError(
                f"shopify variant.price_currency expected {_FORCE_CURRENCY!r}, "
                f"got {price_currency!r}"
            )
```

> `!= _FORCE_CURRENCY` 單一條件即涵蓋「缺失（None）/非字串/不等於 JPY」三種情況。`price_currency` 不寫入 `ProductVariant`，僅作驗證關卡。

- [ ] **Step 7：執行新測試，確認全部通過**

Run: `.venv/bin/python -m pytest tests/test_shopify.py::test_fetch_product_rejects_non_jpy_price_currency tests/test_shopify.py::test_fetch_product_accepts_jpy_price_currency -v -o addopts=""`
Expected: PASS（4 個 reject 參數 + 1 個 accept 皆通過）。

- [ ] **Step 8：執行整個 shopify 測試檔，確認既有成功路徑測試仍綠燈**

Run: `.venv/bin/python -m pytest tests/test_shopify.py -v -o addopts=""`
Expected: 全數 PASS。重點確認：`test_fetch_product_success_hololive_*`、`test_fetch_product_success_vspo_*`、`test_published_at_*`（透過已更新的 `_product_json`）、`test_fetch_product_json_validation_errors_*`（仍各自 raise `ShopifyJSONError`，不受幣別檢查位置影響）。

- [ ] **Step 9：執行完整測試套件，確認無跨檔影響**

Run: `.venv/bin/python -m pytest`
Expected: 全數 PASS（其他測試檔若間接使用上述 fixtures 亦相容）。

- [ ] **Step 10：型別與 lint**

Run: `.venv/bin/basedpyright estimator_king/`
Expected: production code 0 errors。
Run: `uvx ruff check estimator_king/ tests/`
Expected: 無錯誤。

- [ ] **Step 11：Commit（透過 git-master）**

加入檔案：`estimator_king/crawler/shopify.py`、`tests/test_shopify.py`、`tests/fixtures/product_json_hololive.json`、`tests/fixtures/product_json_vspo.json`
commit message：`feat(crawl): validate shopify variant price_currency is JPY`

---

## Task 3：文件說明（JPY 強制與舊資料修復）

**Files:**
- Modify: `CLAUDE.md`（Gotchas 段，第 42-44 行附近）
- Modify: `docs/local-runbook.md`（§8 Re-index Procedure，第 312-335 行附近）
- Modify: `docs/ops-runbook.md`（§6 Re-index Procedure，第 153-183 行附近）

> 純文件變更，無測試。三檔互相獨立，可拆成多個 commit。

- [ ] **Step 1：在 `CLAUDE.md` Gotchas 段新增一條**

在 `CLAUDE.md` Gotchas 區塊（`## Gotchas` 之下的清單）新增一個 bullet：

```markdown
- **Prices are pinned to JPY at fetch time**: the crawler appends `?currency=JPY` to the Shopify `.json` request (`crawler/shopify.py` `_FORCE_CURRENCY`), because Shopify Markets otherwise returns geo/locale-converted prices. Each variant's `price_currency` is validated to equal `JPY`; a mismatch raises `ShopifyJSONError` (counts as a fetch failure). If prices were crawled before this fix and stored in the wrong currency, they self-heal on the next re-fetch (price participates in the content hash), or run `crawl --force-refetch` to fix every product immediately.
```

- [ ] **Step 2：在 `docs/local-runbook.md` §8 Re-index Procedure 末尾新增小節**

在 `docs/local-runbook.md` 的 `### Re-index after the item-level indexing upgrade` 小節之後新增（下方以四個 backtick 的外層 fence 包裹，內含一個三 backtick 的 `bash` 區塊；該 `bash` 區塊連同其關閉行皆屬於要貼入文件的內容）：

````markdown
### Fixing prices crawled in the wrong currency

Older crawl data may have stored non-JPY prices as if they were JPY (before the
`?currency=JPY` enforcement in `crawler/shopify.py`). Because the variant price is
part of the content hash, re-fetching a product with the correct JPY price changes
its hash and triggers a re-index automatically — natural daily crawls will heal the
catalog over several days. To fix every product at once:

```bash
.venv/bin/python -m estimator_king crawl --force-refetch
```

This does **not** require deleting `chroma/` — only the prices change.
````

- [ ] **Step 3：在 `docs/ops-runbook.md` §6 Re-index Procedure 末尾新增小節**

在 `docs/ops-runbook.md` 的 `### Re-index after the item-level indexing upgrade` 小節之後新增：

```markdown
### Fixing prices crawled in the wrong currency

The crawler now pins Shopify prices to JPY (`?currency=JPY`) and rejects non-JPY
variants. Catalog entries crawled before this fix self-heal: the variant price is
part of the content hash, so the next scheduled in-process crawl re-indexes any
product whose price changes. To force the whole catalog to refresh immediately
without clearing the vector store, trigger a `--force-refetch` crawl (note: in
production, crawling runs in-process inside the single bot — do not start a second
crawl process against the live PVC; see §3 on the single-writer constraint).
```

- [ ] **Step 4：確認文件無壞連結 / 標題層級一致**

Run: `.venv/bin/python -m pytest tests/test_shopify.py -v -o addopts=""`
Expected: 仍 PASS（文件變更不影響測試；此步僅作為 Task 完成前的回歸確認）。

- [ ] **Step 5：Commit（透過 git-master）**

加入檔案：`CLAUDE.md`、`docs/local-runbook.md`、`docs/ops-runbook.md`
commit message：`docs: explain JPY price enforcement and wrong-currency data repair`

---

## 完成準則

- `fetch_product` 對所有 product `.json` 請求附加 `?currency=JPY`，且輸入帶既有 query 時不產生畸形 URL。
- 任一 variant 的 `price_currency` 不等於 `"JPY"`（含缺失/非字串）時，整筆商品 fetch 以 `ShopifyJSONError` 失敗。
- 資料模型、content hash、HTTP client 未改動。
- `.venv/bin/basedpyright estimator_king/` 0 errors；`uvx ruff check estimator_king/ tests/` 無錯誤；`.venv/bin/python -m pytest` 全綠。
- 文件說明 JPY 強制機制與舊資料修復方式。
