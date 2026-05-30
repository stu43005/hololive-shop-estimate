# 設計規格：vspo 多語系 sitemap 過濾 + crawl_queue 清理腳本

日期：2026-05-31

## 1. 問題與背景

`SitemapEnumerator`（[estimator_king/crawler/sitemap.py](../../../estimator_king/crawler/sitemap.py)）對所有 store 共用。它的過濾邏輯是：

- index 層（`_extract_products_sitemaps`）：只要 sitemap 的 loc 含子字串 `"products"` 就抓取。
- 最終 URL 層（`enumerate_products`）：`"/products/" in url and "/en/" not in url`。

vspo 的 `https://store.vspo.jp/sitemap.xml` 是一個多語系 Shopify 站，sitemap index 含 **402** 個 products sitemap（每個語系一份）：預設日文無語系前綴（`https://store.vspo.jp/sitemap_products_1.xml`），其餘為 `/en/`、`/en-al/`、`/ja-al/`、`/en-dz/`、`/ja-dz/` … 等帶語系前綴的版本。預設日文 product URL 形如 `https://store.vspo.jp/products/<handle>`，語系版本則為 `https://store.vspo.jp/<locale>/products/<handle>`。

因為最終過濾只排除 `/en/`，其餘所有語系（`en-al`、`ja-al`、`en-dz` …）的 product URL 全部漏進 `crawl_queue`，導致 queue 被灌爆。hololive（`shop.hololivepro.com`）只有預設 + `/en/` 兩個語系，因此舊過濾在該站剛好可用。

### 已確認的關鍵事實

- `external_key = "{store_id}:{product_id}"`，`product_id` 為 Shopify 數字 id，跨語系相同。語系版本抓取會 upsert 到**同一個 product 列**（見 [shopify.py](../../../estimator_king/crawler/shopify.py) `_build_snapshot_from_product_json`）。因此 `products` 表**不會**產生重複列，被灌爆的只有 `crawl_queue`（其唯一鍵為 `(store_id, product_url)`，每個語系 URL 都不同）。
- `crawl_queue` 是待辦工作佇列；清空它不會造成資料遺失，product 狀態列在下次正常 crawl 會自然 self-heal（語系前綴的 `product_url` 會在下次抓到預設 URL 時被改回）。

## 2. 目標與非目標

### 目標

1. 修正 sitemap 過濾，使其對任意多語系 Shopify 站只保留**單一語系**（預設為無語系前綴的預設語系）的 sitemap 與 product URL，且該語系可透過 per-store 設定調整。
2. index 層只抓取該語系的 products sitemap（vspo 從 402 次抓取降為 1 次）。
3. 提供一支可重複執行的維護腳本，直接清空 `crawl_queue`，附使用說明文件。

### 非目標

- 不清理 / 不修改 `products` 表或 ChromaDB（語系抓取未造成重複列；`product_url` 會 self-heal）。
- 清理腳本**不**做 per-store / per-locale 的選擇性刪除——直接全清（使用者決策：全刪不會造成資料遺失）。
- 不調整 daily budget、rate limit 等既有 crawler policy。
- **不支援單一 store 同時保存多語系商品內容**。資料模型 `external_key = "{store_id}:{product_id}"` 不含 locale 維度，且 `product_id` 跨語系相同，故多語系抓取會在 upsert 時互相覆蓋（`content_hash` 非 COALESCE，最後抓取者勝，見 [repository.py:122-126](../../../estimator_king/database/repository.py#L122-L126)），同一 ChromaDB 向量亦被覆蓋。因此一個 store 只保存一個語系。跨語言查詢由多語系 embedding model（預設 `text-embedding-3-large`）的語意相似度處理，不需要儲存多語系副本。基於此，per-store 語系設定採**單一字串**而非清單，以誠實反映此限制並避免使用者誤設多語系造成每輪 re-embed 的 thrashing。

## 3. 設計

### 3.1 共用 locale 判定 helper（`sitemap.py`）

在 `sitemap.py` 新增模組層級常數與純函式，作為「URL 屬於哪個語系」的**唯一真相來源**：

```python
from urllib.parse import urlparse

DEFAULT_LOCALE = "default"


def locale_of_url(url: str) -> str:
    """Return the locale segment of a Shopify store URL, or DEFAULT_LOCALE.

    Multi-locale Shopify stores prefix localized paths with a locale segment,
    e.g. ``/en/products/x`` or ``/ja-al/sitemap_products_1.xml``. Default-locale
    paths start directly with a structural segment (``products`` or
    ``sitemap_...``). The first path segment is therefore the locale unless it is
    one of those structural segments.
    """
    path = urlparse(url).path.lstrip("/")
    first = path.split("/", 1)[0].lower()
    if first == "products" or first.startswith("sitemap"):
        return DEFAULT_LOCALE
    return first
```

判定規則：取 `urlparse(url).path`，去除前導 `/`，取第一個 path segment，**正規化為小寫**。

- 第一個 segment 為 `products` 或以 `sitemap` 開頭 → 視為 `DEFAULT_LOCALE`。
- 否則第一個 segment（小寫）即語系字串（`en`、`en-al`、`ja-al` …）。

回傳值一律為小寫，使其與小寫化後的 `locale` 參數可直接做等值比對。同一函式同時涵蓋 sitemap-index 的 loc 與 product URL 兩種輸入；`urlparse().path` 會自動去除 query string（`?from=...&to=...`、`?variant=red`），故帶查詢參數的 loc 也能正確判定。

### 3.2 enumerator 改用單一 locale 等值過濾

`SitemapEnumerator.enumerate_products` 新增參數：

```python
async def enumerate_products(
    self, base_url: str, locale: str = DEFAULT_LOCALE
) -> list[str]:
```

- 參數 `locale` 為單一語系字串，預設 `DEFAULT_LOCALE`。此預設確保**向後相容**：現有呼叫端與測試不傳此參數時，行為等同「只留預設語系」，`/en/` 等語系一律排除。
- `enumerate_products` 在進入過濾前先把 `locale` 正規化為小寫（`locale = locale.lower()`），與 `locale_of_url` 回傳的小寫值對齊。
- index 層（`_extract_products_sitemaps`）改為同時收 `locale`，條件由 `"products" in url` 改為 `"products" in url and locale_of_url(url) == locale`。`locale` 由 `enumerate_products` 傳入 `_extract_products_sitemaps`。對 vspo 而言，只有無前綴的預設 products sitemap 會被抓取（402 → 1）。
- 最終 URL 層由 `"/products/" in url and "/en/" not in url` 改為 `"/products/" in url and locale_of_url(url) == locale`（防禦性過濾，移除寫死的 `/en/`）。

`_extract_products_sitemaps` 簽名調整為接收 `locale: str`。回傳維持 sorted、去重的 list。class docstring 中描述 `/en/` 過濾的步驟同步更新為描述單一 locale 過濾。

### 3.3 設定：per-store `locale`（單一語系）

`Store` dataclass（[config_schema.py](../../../estimator_king/config_schema.py)）新增單一字串欄位：

```python
@dataclass
class Store:
    id: str
    base_url: str
    sitemap_url: str
    locale: str = "default"
```

- YAML 省略 `locale` → 預設 `"default"`（程式端維持此預設行為）。
- `Store.validate()` 新增檢查：`locale` 必須為非空字串；否則 `raise ValueError`。此檢查確實會執行：`AppConfig.validate()`（config_schema.py 既有流程，line 134-135）會 `for store in self.stores: store.validate()`，而 `from_yaml` 在結尾呼叫 `config.validate()`（line 266）。
- `AppConfig.from_yaml` 解析 store 時讀取 `s.get("locale", "default")`。
- **不變式**：`Store.locale` 代表預設語系時其值必須恰好等於 `DEFAULT_LOCALE`（`"default"`）。`stores_config.yaml` 的 `locale: default`、dataclass 的預設值 `"default"`、以及 `locale_of_url` 的 `DEFAULT_LOCALE` 三者必須保持同步——若改動 `DEFAULT_LOCALE` 字面值，須一併更新另兩處，否則預設語系 URL 會比對失敗而被全數丟棄。
- 單一字串（而非清單）為刻意設計：資料模型只能保存一個語系（見 §2 非目標），單值可避免使用者誤設多語系。

store 的 `locale` 由 `populate_queue_from_sitemap`（[crawler/pipeline.py](../../../estimator_king/crawler/pipeline.py)）直接傳給 `enumerate_products`，呼叫端 `cycle.py` **不需改動**。`populate_queue_from_sitemap` 目前已接收 `store` 並呼叫 `enumerator.enumerate_products(store.base_url)`；改為：

```python
# 在 populate_queue_from_sitemap 內
sitemap_urls = await enumerator.enumerate_products(store.base_url, store.locale)
```

`enumerate_products` 內部會把 `locale` 正規化為小寫（見 §3.2），`locale_of_url` 回傳值亦為小寫（見 §3.1），故等值比對 case-correct。

`stores_config.yaml` 為兩個 store 顯式加上 `locale`：

```yaml
stores:
  - id: hololive
    base_url: https://shop.hololivepro.com
    sitemap_url: https://shop.hololivepro.com/sitemap.xml
    # 只抓預設語系（無語系前綴）；排除 /en/ 等所有語系版本
    locale: default

  - id: vspo
    base_url: https://store.vspo.jp
    sitemap_url: https://store.vspo.jp/sitemap.xml
    # 只抓預設日文（無語系前綴）；排除 /en/、/en-al/、/ja-al/ 等所有語系版本
    locale: default
```

### 3.4 清理維護腳本 `scripts/clean_crawl_queue.py`

可重複執行的維護工具（非 dated migration），直接清空 `crawl_queue`：

```
.venv/bin/python -m scripts.clean_crawl_queue [--db PATH] [--store STORE_ID] [--dry-run]
.venv/bin/python scripts/clean_crawl_queue.py [--db PATH] [--store STORE_ID] [--dry-run]
```

行為：

- 沿用 [migrate_2026_05_30_fix_product_urls.py](../../../scripts/migrate_2026_05_30_fix_product_urls.py) 的 `sys.path` 注入手法，使 `python scripts/x.py` 與 `python -m scripts.x` 兩種執行方式皆可。
- db 路徑解析：`--db` → `$DATABASE_PATH` → `./estimator_king.db`。
- 透過 `ProductStateRepository` 開啟資料庫，複用既有方法：
  - `repo.queue_size(store_id)` 取得將被刪除的列數。
  - `repo.clear_queue(store_id)` 執行刪除（`store_id is None` → 全清；指定 → 只清該 store）。兩者皆已存在於 [repository.py](../../../estimator_king/database/repository.py)。
- `--store` 省略 → 全清（預設行為，符合「全刪就好」）。
- `--dry-run` → 只印出將被刪除的列數，**不**執行刪除。
- 印出摘要：刪除前 queue 大小、實際刪除列數（dry-run 時標明未刪除）。
- 退出碼 0 表成功。
- 需在 bot 停止時執行（single DB writer；以 docstring 與文件提示，不在程式中強制）。

腳本結構對齊既有 migration 慣例：模組 docstring 說明用途與 usage、`from __future__ import annotations`、`main(argv)` 解析參數（使用 `argparse`）、`if __name__ == "__main__": raise SystemExit(main(sys.argv))`。

### 3.5 文件 `docs/scripts/clean-crawl-queue.md`

內容涵蓋：

- 用途：清空 crawl_queue 待辦佇列的維護腳本。
- 何時用：queue 異常膨脹、或修正 sitemap 過濾後要清掉殘留的語系 URL。
- 前置條件：停止 bot（single writer）。
- 為何安全：queue 為待辦工作、非權威狀態；product 列在下次 crawl 自然 self-heal，不會遺失資料。
- 參數說明：`--db`、`--store`、`--dry-run`，以及 db 路徑解析順序。
- 範例：`--dry-run` 預覽輸出、實際清除輸出、只清單一 store。
- 與 sitemap 語系過濾修正的關係（先修過濾再清 queue，避免下次 crawl 再灌爆）。

## 4. 測試計畫

### 4.1 `tests/test_sitemap.py`

- `locale_of_url` 單元測試：預設 product URL（`/products/x` → `default`）、sitemap loc（`/sitemap_products_1.xml` → `default`）、語系 product URL（`/en/products/x` → `en`、`/ja-al/products/x` → `ja-al`）、語系 sitemap loc（`/en-dz/sitemap_products_1.xml` → `en-dz`）。
- 新增多語系 fixture（含 `default`、`en`、`ja-al`、`en-dz` 的 sitemap index 與 product URL），驗證 `enumerate_products` 預設只回傳 `default` 語系的 URL。
- 驗證 index 層不抓取語系 sitemap：檢查 `client.call_urls` 不含語系前綴的 sitemap loc。
- 驗證傳入自訂 `locale`（例如 `"en"`）時，只回傳 `en` 語系的 URL，`default` 與其他語系被排除。
- 既有測試（`test_enumerate_products_excludes_en_paths` 等）在不傳 `locale` 時仍應通過（預設 = `default`，`/en/` 仍被排除）。

### 4.2 清理腳本測試

- 以暫存 DB（套用 `schema.sql`）塞入混合 locale 的 `crawl_queue` 列。
- 跑 `clean_crawl_queue` 主函式：驗證 queue 被清空、回傳摘要正確。
- `--dry-run`：驗證列數正確回報且 queue **未**被刪除。
- `--store`：驗證只清指定 store、其他 store 的列保留。

### 4.3 驗證指令（完成前全數通過）

- Type check：`.venv/bin/basedpyright estimator_king/ scripts/`（production code 0 errors）。
- Lint：`uvx ruff check estimator_king/ scripts/ tests/`。
- 相關測試：`.venv/bin/python -m pytest tests/test_sitemap.py tests/test_config.py <清理腳本測試> -v -o addopts=""`。
- 全套件：`.venv/bin/python -m pytest`。

## 5. 驗收條件

1. 對 vspo sitemap，`enumerate_products` 在預設設定下只回傳 `https://store.vspo.jp/products/...`（無語系前綴）的 URL，且 index 層只抓取 1 份預設 products sitemap。
2. 對 hololive，行為與修正前一致（只留預設、排除 `/en/`）。
3. `Store.locale` 省略時預設 `"default"`；`stores_config.yaml` 兩 store 均顯式標註 `locale: default`。
4. `scripts/clean_crawl_queue.py` 可用 `-m` 與直接執行兩種方式運行，能全清或依 `--store` 清除 `crawl_queue`，`--dry-run` 不刪除。
5. `docs/scripts/clean-crawl-queue.md` 完整說明用途、前置、參數與範例。
6. basedpyright（production 0 errors）、ruff、pytest 全數通過。
