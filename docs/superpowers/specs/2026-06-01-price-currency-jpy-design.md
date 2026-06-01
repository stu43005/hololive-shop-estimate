# 設計規格：強制並驗證爬蟲價格幣別為 JPY

- 日期：2026-06-01
- 狀態：已核准，待產出實現計畫
- 影響範圍：爬蟲價格抓取（`estimator_king/crawler/shopify.py`）、相關文件

## 1. 問題與 Root Cause

系統一律以 JPY 計算與顯示價格（`ProductItem.price_jpy: int`、estimator 以 `¥` 呈現）。但爬蟲抓到的價格在某些情境下幣值錯誤。

**Root Cause（已對真實 endpoint 驗證）：Shopify Markets 多幣別。**

`/products/<handle>.json` 回傳的 `variant.price` 是「依市場偵測到的幣別」換算後的金額，市場偵測由 geo-IP 或 `localization` cookie 驅動。現有程式碼 `crawler/shopify.py` 只取 `variant.price` 字串，`sync/items.py` 的 `_price_to_int` 直接 `int(float(price))` 當成 JPY。因此在非日本 region（如 k8s 叢集）或任何被設了 `localization` 的情境下，例如 `15.00 USD` 會被當成 `15 JPY` 寫入，產生默默的資料錯誤。

### 實測證據（`hololive-summerfes-acrylic-panel`）

| 請求條件 | price | price_currency |
| --- | --- | --- |
| 預設（日本可達 IP） | `2200` | `JPY` |
| cookie `localization=US` | `15.00` | `USD` |
| `?currency=USD` | `15.00` | `USD` |
| `?currency=JPY` | `2200` | `JPY` |
| cookie `cart_currency=JPY`（單獨） | `2200`（無效果，跟著 geo 走） | `JPY` |
| `?currency=USD` + cookie `cart_currency=JPY` | `15.00`（query param 勝出） | `USD` |

vspo store（`vspo-comicmarket101`）行為一致：預設 `10000`/`JPY`、`?currency=JPY` → `10000`/`JPY`、`?currency=USD` → `88.00`/`USD`。

### 關鍵結論

1. **`variant.price_currency` 欄位每次都存在**，值反映該筆 price 的實際幣別 → 可作可靠的防禦性檢查。
2. **`cart_currency` cookie 對 `.json` endpoint 無效**（原始假設有誤）。真正能強制幣別的是 **`?currency=<CODE>` query param**，且其優先級高於 cookie 與 geo 偵測。
3. 兩個 store（hololive、vspo）行為一致，`?currency=JPY` 對兩者皆穩定回傳 JPY。

## 2. 目標與非目標

### 目標

- 爬蟲對 product `.json` 的請求一律強制以 JPY 回傳價格。
- 解析時防禦性驗證每個 variant 的幣別確為 JPY；若不符則該商品整筆 fetch 失敗，絕不寫入錯誤幣別的價格。
- 文件說明既有髒資料的修復方式。

### 非目標（YAGNI）

- 不支援多幣別儲存或換算（系統設計上即 JPY-only，`price_jpy` 遍佈各處）。
- 不新增 store 層級 currency 設定。
- 不新增一次性資料清理指令（既有 `crawl --force-refetch` 已足夠）。

## 3. 設計

落點集中在 price 的唯一來源 `crawler/shopify.py`。不更動 HTTP client、資料模型、content hash。

### 3.1 強制 JPY（`fetch_product`）

- 在模組層新增常數 `_FORCE_CURRENCY = "JPY"`。
- `fetch_product` 建構 JSON 請求 URL 時，於 `.json` 後附加 `?currency=<_FORCE_CURRENCY>`，即請求 `f"{canonical_url}.json?currency=JPY"`。
- HTML fetch（`canonical_url`，無 `.json`、無 query）**維持不變**：它只供商品詳情區塊（グッズ詳細等）抽取，不含 price，無需附加幣別參數。
- `canonical_url` 在 `fetch_product` 中已先 strip 掉 `.json` 與結尾 `/`，因此附加 query 時不會與既有 query 衝突。

### 3.2 防禦性驗證（`_build_snapshot_from_product_json`）

- 在逐一解析 variant 的迴圈中，於現有 `price` 驗證之後，額外取出 `price_currency = v_obj.get("price_currency")`。
- 驗證規則：若 `price_currency` **缺失、非字串、或不等於 `"JPY"`**，`raise ShopifyJSONError`（沿用既有例外型別，訊息需含實際取得的幣別值以利除錯）。
- `price_currency` 不寫入 `ProductVariant`，僅作為驗證關卡（系統維持 JPY-only，無需保存幣別）。

### 3.3 失敗行為

- `ShopifyJSONError` 由 `_build_snapshot` → `fetch_product` 上拋，被 crawl cycle 當成一次 fetch 失敗：`consecutive_failures` 累加，達 `inactive_failure_threshold` 才標記商品 inactive。
- 因已強制 `?currency=JPY`，此驗證在正常情況不會觸發，屬 defense-in-depth；只有當 Shopify 行為改變、強制失效時才會攔截，避免錯誤幣別價格進入資料庫。

### 3.4 資料模型與 content hash

- 不新增 currency 欄位：`ProductVariant`、`ProductItem`、SQLite schema、ChromaDB metadata 皆不變。
- `compute_content_hash` 不變：幣別在驗證後恆為 JPY（常數），納入 hash 無意義。

### 3.5 既有髒資料修復（文件）

- `variant.price` 已參與 content hash，修正後對既有商品 re-fetch 時，正確的 JPY 價格會使 hash 變化並觸發 re-index，自動修復。
- 受每次爬取的 daily budget（`max_products_per_run`）限制，全量自然修復需數天。
- 文件須註明：要立即全量修復，可執行一次 `crawl --force-refetch`。
- 更新位置：`CLAUDE.md` Gotchas 段、`docs/local-runbook.md` 與 `docs/ops-runbook.md` 中與重新索引/資料修復相關的段落。

## 4. 測試

新增單元測試（沿用既有 pytest 慣例與 fake getter 模式）：

### `_build_snapshot_from_product_json` 幣別驗證

- `price_currency="JPY"`：正常建構 `ProductSnapshot`，variants 解析成功。
- `price_currency="USD"`：`raise ShopifyJSONError`。
- `price_currency` 欄位缺失：`raise ShopifyJSONError`。
- `price_currency` 非字串（如 `null`/數字）：`raise ShopifyJSONError`。

### `fetch_product` 強制幣別

- 使用記錄被請求 URL 的 fake getter，斷言 JSON 請求的 URL 為 `…/products/<handle>.json?currency=JPY`。
- 斷言 HTML 請求的 URL 維持為 `…/products/<handle>`（不含 `.json`、不含 `currency` query）。

## 5. 驗證 Toolchain

依專案規範，變更後須通過：

- Type check：`.venv/bin/basedpyright estimator_king/`（production code 0 errors）
- Lint：`uvx ruff check <paths>`
- 相關測試：`.venv/bin/python -m pytest <path> -v -o addopts=""`（單檔）或完整 `.venv/bin/python -m pytest`
