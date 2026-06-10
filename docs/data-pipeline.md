# 資料管線:從 `stores_config.yaml` 到 ChromaDB,再到 `/estimate`

本文件完整說明這套系統的兩條資料路徑,兩者共用同一份
[stores_config.yaml](../stores_config.yaml)、同一個 ChromaDB collection 與
embedding model:

- **第一部 — 寫入端(crawl → ChromaDB)**:一筆商品從 `stores` 設定開始,經過
  sitemap 枚舉、入隊、抓取、萃取、拆解、分類、嵌入,最終以「每個 item 一個向量」
  寫入 ChromaDB。(階段 0–10)
- **第二部 — 查詢端(`/estimate` → 價格估計)**:使用者在 Discord 輸入商品名,系統
  embed 查詢、type-aware 檢索 ChromaDB、rerank、再請 chat model 產出結構化估價。
  (階段 11–14)

每個階段都標注:**進行的限制/過濾/條件/格式**、**對應的 config 設定**、以及
**對應的 function 名稱(含 file:line)**。

> 來源以 master 分支現行程式碼為準。所有行號為撰寫當下的快照,日後若程式碼變動可能略有偏移,但 function 名稱穩定。

---

## 總覽流程圖

```text
stores_config.yaml (stores / crawler / item_types / talents / bundle_set / estimator)
        │  AppConfig.from_yaml()  → config.validate()(只驗結構)
        ▼
┌─────────────────────────────────────────────────────────────────────┐
│ run_crawl_cycle()  每個 store 依序執行,單一 store 失敗不中止整個 cycle  │
│                                                                       │
│  1. sitemap 枚舉      populate_queue_from_sitemap → enumerate_products │
│        │  過濾: "/products/" 子字串 + locale 比對 → 排序去重           │
│        ▼                                                              │
│  2. 入隊 / budget     新品永遠入隊;剩餘額度給最舊的既有品              │
│        │  enqueue_oldest_products(limit = max_products_per_run - 新品數)│
│        ▼                                                              │
│  3. drain queue       async_process_queue(worker = concurrency_per_domain)│
│        │                                                              │
│        ├─ 3a. 抓取    fetch_product → AsyncHTTPClient.get             │
│        │       rate limit / jitter / 並發 / timeout / retry / 熔斷器  │
│        ├─ 3b. 萃取    _build_snapshot(JSON 商品 + HTML 詳情)          │
│        │       強制 ?currency=JPY;非 JPY 直接報錯                    │
│        ├─ 3c. 變更偵測 compute_content_hash(SHA-256);未變更→skip      │
│        ├─ 3d. 拆 item decompose_items(濾 SET/¥0 → 才能去重 → 命名 → 濾套組)│
│        ├─ 3e. 分類    classify_item(詞表最長子字串 → LLM fallback)    │
│        ├─ 3f. 嵌入    embed_documents(每 item 一段 document)         │
│        └─ 3g. 寫入    VectorStore.upsert(ChromaDB) + repository.upsert(SQLite)│
│                                                                       │
│  4. 失活掃描(跨 store 一次)  mark_inactive_products                  │
│        連續失敗 ≥3 或連續 sitemap miss ≥4 → inactive + 刪除向量        │
└─────────────────────────────────────────────────────────────────────┘
        ▼
   回傳 counters: {discovered, fetched_ok, created, updated, skipped, inactive, errors}


查詢端(/estimate):
Discord modal 輸入(≤10 行) → parse_product_lines
        ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Estimator.estimate_products  以 CHUNK_SIZE=10 分批呼叫 chat           │
│                                                                       │
│  每行商品名:                                                          │
│    embed_query → classify_query(命中 0..N 個 item_type)             │
│        ▼                                                              │
│    type-aware 檢索: 每個 type 跑一次 where 過濾 + 一次無過濾 plain    │
│        │  各取 fetch_n = top_k × fetch_multiplier 筆                  │
│        ▼                                                              │
│    依 id 合併去重(取 distance 最小)→ _rerank → 取 top_k            │
│        │  rerank = (1-distance) + recency_weight·新近度 - 多樣性懲罰  │
│        ▼                                                              │
│    _format_reference → 組成該行的 reference context block             │
│                                                                       │
│  整批 context + 商品名 → ChatProvider.estimate(SYSTEM_PROMPT)        │
│        ▼  結構化輸出 EstimateBatch                                    │
│  _reconcile(以 normalize_text 對齊回每一輸入行,缺漏補 low/¥0)       │
└─────────────────────────────────────────────────────────────────────┘
        ▼
   snap_to_tax_grid(每筆價格 round 到最近 ¥110 倍數)
        ▼
   format_estimates → Discord embeds(超長自動分頁)
```

---

## 第一部 — 寫入端(crawl → ChromaDB)

## 階段 0:設定載入

**進入點**:`AppConfig.from_yaml(path)` → `load_config(path)`
([config_schema.py](../estimator_king/config_schema.py))

### 讀取來源分工

| 來源 | 內容 | 說明 |
| --- | --- | --- |
| **YAML** ([stores_config.yaml](../stores_config.yaml)) | `stores`、`crawler`、`proxy`、`item_types`、`item_types_version`、`talents`、`bundle_set`、`estimator` | 結構化設定 |
| **環境變數** | API 金鑰、base URL、模型名、`CHROMA_PATH`、`DATABASE_PATH`、`DISCORD_TOKEN` 等 | 憑證與路徑 |

- Provider key **cascade**(`build_provider_config()`):
  `embedding_api_key` → `openai_api_key`;`chat_api_key` → `openai_api_key`;
  `typing_api_key` → `chat_api_key` → `openai_api_key`;base URL 同樣層層 fallback。
  把 `OPENAI_BASE_URL` 指向 ollama 的 `/v1` 即可整組換掉 provider。

### 驗證範圍 — 只驗結構,不驗憑證

`config.validate()`(config_schema.py)只檢查 YAML 來源的結構:

- `Store.validate()`:`id` / `base_url` / `sitemap_url` / `locale` 皆須為非空字串。
- `CrawlerPolicy.validate()`:數值欄位須 > 0(`jitter_max`、`max_retries` 允許 = 0)。
- `ProxyConfig.validate()`:`enabled=True` 時須至少設定一個 proxy。
- `BundleSetPolicy.validate()`:`price_ratio` 須 > 0。

憑證(API key、Discord token)**不在此驗證**,由各入口點自行檢查。

### `stores` 各欄位意義

```yaml
stores:
  - id: hololive                                   # DB / 向量 metadata 分層用、log 標籤
    base_url: https://shop.hololivepro.com         # 抓取相對路徑的基準
    sitemap_url: https://shop.hololivepro.com/sitemap.xml
    locale: default                                # 只抓無語系前綴版本(排除 /en/、/ja-al/ 等)
```

> 注意:`base_url` 才是 sitemap 枚舉實際使用的入口(`enumerate_products` 會自行
> `urljoin(base_url, "/sitemap.xml")`),`sitemap_url` 欄位目前在枚舉流程中未被直接讀取。

### 其他 YAML 區塊解析成的結構

| YAML 區塊 | 解析後型別 / 欄位 | 用途 |
| --- | --- | --- |
| `item_types` | `list[str]` | item-type 分類詞表(最長子字串匹配) |
| `item_types_version` | `int` | 版本號;遞增即強制下次 crawl 重新分類 / 重新索引 |
| `talents` | `frozenset[str]` | 才能名;用於 variant 去重(talent-gated dedup) |
| `bundle_set` | `BundleSetPolicy(keywords, price_ratio, keep_keywords)` | 整套組商品排除政策 |
| `estimator` | `estimator_top_k` / `recency_weight` / `diversity_weight` / `fetch_multiplier` | `/estimate` 檢索與排序參數(不影響 crawl 寫入) |

---

## 階段 1:Sitemap 枚舉

**對應 function**:
`populate_queue_from_sitemap()`([pipeline.py:16](../estimator_king/crawler/pipeline.py#L16))
→ `SitemapEnumerator.enumerate_products()`([sitemap.py:56](../estimator_king/crawler/sitemap.py#L56))

### 三層解析

1. **抓 sitemap index**:`urljoin(base_url, "/sitemap.xml")`
   ([sitemap.py:73](../estimator_king/crawler/sitemap.py#L73)),以
   `xml.etree.ElementTree` 解析,命名空間
   `SITEMAP_NS = {"sitemap": "http://www.sitemaps.org/schemas/sitemap/0.9"}`。
2. **挑出 products 子 sitemap**:`_extract_products_sitemaps()`
   ([sitemap.py:96](../estimator_king/crawler/sitemap.py#L96))。遍歷
   `<sitemap><loc>`,**過濾條件**:URL 含 `"products"` 子字串 **且**
   `locale_of_url(url) == locale`([sitemap.py:114](../estimator_king/crawler/sitemap.py#L114))。
3. **取出商品 URL**:`_extract_product_urls()`
   ([sitemap.py:119](../estimator_king/crawler/sitemap.py#L119)),遍歷每個
   `<url><loc>`。

### Locale 過濾邏輯

`locale_of_url()`([sitemap.py:17](../estimator_king/crawler/sitemap.py#L17)):取
URL path 的**第一段**並轉小寫;若是 `products` 或以 `sitemap` 開頭 → 視為
`DEFAULT_LOCALE`("default"),否則第一段就是 locale 代碼。

| URL | 判定 locale |
| --- | --- |
| `/products/item` | `default` |
| `/en/products/item` | `en` |
| `/ja-al/products/item` | `ja-al` |
| `/sitemap_products_1.xml` | `default` |

`locale: default` 因此**只保留無前綴版本**,排除所有語系版本。

> **設計理由**
>
> - **為何用「第一段是否為結構性 segment」判定,而非寫死語系白名單**:vspo 的
>   `sitemap.xml` index 內有 **數百份** 語系版本 products sitemap(`/en/`、`/en-al/`、
>   `/ja-al/`、`/ja-dz/`…),語系數量無法窮舉;寫死「只排除 `/en/`」這種清單在
>   hololive(僅預設 + `/en/`)剛好可用,但在 vspo 會讓其餘所有語系灌爆 `crawl_queue`。
>   改判「第一段是否為 `products` / `sitemap*` 結構性 segment」後,任何非結構性的第一段
>   一律當語系排除,對站點數量免疫。
> - **為何 `locale` 是單一字串而非清單**:資料模型 `external_key = "{store_id}:{product_id}"`
>   不含 locale 維度,且同一商品的 `product_id` 跨語系相同。若一個 store 同時抓多語系,
>   會在 SQLite upsert 與 ChromaDB 互相覆寫(最後抓取者勝),每輪反覆 re-embed。因此一個
>   store 只能綁一個語系;跨語言查詢交由多語系 embedding model 的語意相似度處理。

### 最終過濾與輸出格式

```python
filtered = [url for url in all_product_urls
            if "/products/" in url and locale_of_url(url) == locale]
return sorted(filtered)          # sitemap.py:85-89
```

- **去重**:中間以 `set[str]` 累積([sitemap.py:80](../estimator_king/crawler/sitemap.py#L80))。
- **排序**:`sorted()` 保證穩定順序。
- **無數量上限**:所有符合 locale 的商品 URL 全數回傳(數量限制發生在下一階段的入隊)。
- **輸出**:`list[str]`(完整商品 URL 清單)。

---

## 階段 2:入隊與每次抓取額度(budget)

**對應 function**:`run_crawl_cycle()`
([cycle.py:25](../estimator_king/crawler/cycle.py#L25));入隊輔助
`enqueue_oldest_products()`([pipeline.py:72](../estimator_king/crawler/pipeline.py#L72))。

### 新品 vs 既有品的區分(發生在 sitemap 枚舉內)

`populate_queue_from_sitemap()` 逐一處理每個 sitemap URL
([pipeline.py:49-55](../estimator_king/crawler/pipeline.py#L49-L55)):

- `repo.get_by_product_url()` 回傳 `None`(**新品**)→ `repo.enqueue_url()` 入隊,
  `enqueued += 1`。
- 已存在(**既有品**)→ `repo.record_sitemap_seen()` 更新 `last_seen_in_sitemap_at`。

入隊使用 `INSERT OR IGNORE`([repository.py](../estimator_king/database/repository.py)
`enqueue_url`),重複 URL 不會重覆入隊。

**Sitemap miss 偵測**([pipeline.py:57-63](../estimator_king/crawler/pipeline.py#L57-L63)):
所有活躍品若不在本次 sitemap URL 集合中,`repo.increment_sitemap_miss()` 累加。
**邊界**:sitemap 回傳 0 個 URL 時直接 return 0,**不**把任何商品標記為 miss
([pipeline.py:41-43](../estimator_king/crawler/pipeline.py#L41-L43))。

### Budget 分配

```python
if force_refetch:                                   # cycle.py:52-54
    for state in repo.list_active(store.id):
        repo.enqueue_url(store.id, state.product_url)   # 所有活躍品全入隊,忽略額度
else:                                               # cycle.py:55-57
    remaining = max(0, config.crawler.max_products_per_run - new_count)
    enqueue_oldest_products(store, repo, limit=remaining)
```

- **新品永遠被抓**:已在階段 1 入隊,計為 `new_count`。
- **剩餘額度** = `max_products_per_run - new_count`,花在**最舊的既有品**。
- **「最舊」定義**:`get_oldest_active_products()` 以
  `ORDER BY last_fetch_success_at ASC` 取出
  ([repository.py:189](../estimator_king/database/repository.py#L189)),即最久沒成功抓取的優先。
- `--force-refetch`([`__main__.py`](../estimator_king/__main__.py))→
  `force_refetch=True`,**所有活躍品無條件入隊,忽略每次額度**。

| Config | 預設 | 作用 |
| --- | --- | --- |
| `max_products_per_run` | 32 | 每個 store 每次抓取額度(新品 + 最舊既有品) |

### 錯誤隔離

每個 store 的 sitemap 階段與 drain 階段各自包在 `try/except`
([cycle.py:44-50](../estimator_king/crawler/cycle.py#L44-L50)、
[cycle.py:59-74](../estimator_king/crawler/cycle.py#L59-L74)):單一 store 失敗
`counters["errors"] += 1` 後 `continue`,不影響其他 store。失活掃描同樣獨立包裹
([cycle.py:76-85](../estimator_king/crawler/cycle.py#L76-L85))。

### 排程觸發

`CrawlScheduler`([scheduler.py](../estimator_king/crawler/scheduler.py)):
`run_forever()` 以 `crawl_schedule_hours * 3600` 秒為間隔呼叫 `run_once()`;
`run_once()` 用 `_running` 旗標防止重疊觸發(若上一輪仍在跑就跳過本次)。

| Config | 預設 | 作用 |
| --- | --- | --- |
| `crawl_schedule_hours` | 24.0 | in-process 排程器的 crawl 間隔(小時) |

---

## 階段 3:抓取(AsyncHTTPClient)

**對應 function**:`async_process_queue()`
([async_pipeline.py:53](../estimator_king/crawler/async_pipeline.py#L53))
驅動 worker pool,每個 entry 由 `_handle()`
([async_pipeline.py:78](../estimator_king/crawler/async_pipeline.py#L78))處理,
HTTP 經 `AsyncHTTPClient.get()`
([async_http_client.py](../estimator_king/crawler/async_http_client.py))。

### Worker 並發

- queue 以 `asyncio.Queue` 裝載本 store 待處理 entry,worker 數
  `max(1, policy.concurrency_per_domain)`
  ([async_pipeline.py:131](../estimator_king/crawler/async_pipeline.py#L131)),
  `asyncio.gather` 並行 drain。
- `sync_products()` 寫入屬同步阻塞,以 `asyncio.to_thread` 丟到執行緒執行
  ([async_pipeline.py:83](../estimator_king/crawler/async_pipeline.py#L83))。

### 抓取限制(全部由 `CrawlerPolicy` 控制)

| 機制 | Config 欄位 | 預設 | 實作 |
| --- | --- | --- | --- |
| 速率限制 | `rate_limit_rps` | 1.5 | 每 domain 基礎延遲 `1/rps`,`AsyncDomainRateLimiter` |
| 抖動 | `jitter_max` | 0.5 | 延遲再加 `uniform(0, jitter_max)` |
| 每 domain 並發 | `concurrency_per_domain` | 3 | 每 domain 一個 `asyncio.Semaphore` |
| 連線逾時 | `timeout_connect` | 10 | `aiohttp.ClientTimeout(sock_connect=...)`([async_http_client.py:295](../estimator_king/crawler/async_http_client.py#L295)) |
| 讀取逾時 | `timeout_read` | 30 | `aiohttp.ClientTimeout(sock_read=...)`([async_http_client.py:296](../estimator_king/crawler/async_http_client.py#L296)) |
| 重試次數 | `max_retries` | 3 | `tenacity` 重試 `RateLimitError` / `ServerError`,指數退避 |

- **User-Agent**:`Mozilla/5.0 (compatible; EstimatorKing/{__version__})`
  ([async_http_client.py:263](../estimator_king/crawler/async_http_client.py#L263))。
- **HTTP 狀態處理**([async_http_client.py:313-317](../estimator_king/crawler/async_http_client.py#L313)):
  `403`/`430`(WAF 阻擋)→ 觸發熔斷器並丟 `WAFBlockedError`;`429` → 解析
  `Retry-After` 後丟可重試的 `RateLimitError`;`5xx` → `ServerError`(可重試);
  `4xx` → `ClientError`(不重試)。
- **熔斷器** `AsyncDomainCircuitBreaker`:連續失敗達 `failure_threshold`(預設 3)
  即開路 `open_timeout_seconds`(預設 60 秒)
  ([async_http_client.py:166-202](../estimator_king/crawler/async_http_client.py#L166));
  開路期間該 domain 請求直接丟 `CircuitBreakerOpenError`。

> 規範:任何對 live store 的抓取都必須走 `AsyncHTTPClient`,不可用裸 `urllib`/
> `requests`/`aiohttp`,否則會繞過速率限制與熔斷,招致 WAF `403`。

### 抓取失敗處理([async_pipeline.py:108-117](../estimator_king/crawler/async_pipeline.py#L108))

- 任一例外 → `repo.increment_consecutive_failures()` 累加該品連續失敗數,`result.failed += 1`。
- 若例外是 `ClientError` 且 `status_code ∈ {404, 410}`(商品確定消失)→
  `delete_queue_entry()`,**從 queue 移除不再重抓**;其餘暫時性錯誤保留在 queue 下輪重試。

---

## 階段 4:內容萃取(Shopify JSON + HTML)

**對應 function**:`fetch_product()`
([shopify.py:143](../estimator_king/crawler/shopify.py#L143))。

### 抓取兩個端點

1. 正規化 URL:去掉 `.json` 尾碼與 query/fragment。
2. **JSON 端點**:`{canonical_url}.json?currency=JPY`
   ([shopify.py:154](../estimator_king/crawler/shopify.py#L154))。
   常數 `_FORCE_CURRENCY = "JPY"`([shopify.py:19](../estimator_king/crawler/shopify.py#L19))
   強制 Shopify Markets 回傳 JPY,而非依地理/語系換算後的價格。
3. **HTML 端點**:`{canonical_url}`(**不**加 currency),用於萃取規格詳情區塊。

> **設計理由(為何強制 JPY)**
>
> - **Shopify Markets 會回傳換算後價格**:`/products/<handle>.json` 的 `variant.price`
>   是依「偵測到的市場幣別」換算後的金額,市場由 **geo-IP 或 `localization` cookie** 決定。
>   在非日本 region(例如 k8s 叢集)會把 `15.00 USD` 默默當成 `15 JPY` 寫進 DB —— 屬靜默
>   資料污染。
> - **為何用 `?currency=` query param 而非 cookie/header**:實測 `cart_currency` cookie 對
>   `.json` endpoint **無效**;唯一能強制幣別的是 `?currency=<CODE>` query param,且其優先級
>   **高於** cookie 與 geo 偵測。hololive 與 vspo 對 `?currency=JPY` 皆穩定回傳 JPY。
> - **為何 HTML 端點不加 currency**:HTML 只供詳情區塊抽取,**不含 price**,故無須附加幣別。
> - **為何仍要驗證 `price_currency == "JPY"`**:屬 defense-in-depth。已強制 query param,正常
>   不會觸發;只有 Shopify 行為變更導致強制失效時才攔下,避免錯誤幣別進 DB。失敗使整筆 fetch
>   失敗 → `increment_consecutive_failures` → 達閾值由失活掃描標記 inactive。幣別驗證後恆為
>   常數 JPY,故**不**納入 content hash、也**不**存進 metadata。

### JSON → `ProductSnapshot`

`_build_snapshot_from_product_json()`([shopify.py:74](../estimator_king/crawler/shopify.py#L74)):

| 欄位 | 來源 | 限制 / 格式 |
| --- | --- | --- |
| `product_id` | `product["id"]` | 須為 int |
| `title` | `product["title"]` | 須為 str |
| `description` | `product["body_html"]` | 經 `_clean_body_html()`(移除 `<img>/<style>/<script>`,markdownify 成 Markdown) |
| `variants[]` | `product["variants"]` | 每個 variant 取 `variant_id`(int)、`title`(str)、`price`(str)、`sku`(str∣null) |
| `published_at` | `published_at` → `created_at` | `_parse_published_at()`,轉 epoch 秒,皆無則 0 |

- **幣別驗證**:variant 的 `price_currency` 必須等於 `"JPY"`,否則丟 `ShopifyJSONError`
  ([shopify.py:117-121](../estimator_king/crawler/shopify.py#L117))。

### HTML 詳情萃取

`extract_detail_sections()`([html_extractor.py](../estimator_king/crawler/html_extractor.py)):
搜尋標題或 `<details><summary>` 中含
`("セット詳細", "グッズ詳細", "Set Details", "Merch details")` 的區段,回傳
`dict[str, str]`(section → 內容);找不到回傳空 dict。此結果存入
`snapshot.html_details`,供後續 item snippet 比對。

---

## 階段 5:內容雜湊與變更偵測

**對應 function**:`compute_content_hash()`
([snapshot.py](../estimator_king/crawler/snapshot.py))。

- 先 `canonicalize_snapshot()` 正規化(解碼 HTML entity、折疊空白、dict key 排序、
  variants 依 `variant_id` 排序),再算 **SHA-256**。
- 納入雜湊:`NORMALIZER_VERSION`(目前 = 2)、`product_id`、`title`、`description`、
  `variants`、`html_details`。**排除** `published_at` 與各時間戳(非決定性)。

**跳過條件**(`sync_products` 內,4 項全成立才跳過)
([engine.py:158-164](../estimator_king/sync/engine.py#L158)):

```python
unchanged = (
    state is not None
    and state.content_hash == content_hash
    and state.normalizer_version == NORMALIZER_VERSION       # = 2
    and state.item_types_version == item_types_version       # 來自 config
    and state.last_indexed_at is not None
)
```

任一不符 → 重建 item(重新嵌入、重新 upsert)。因此:
**改 `NORMALIZER_VERSION`(改了正規化/格式)或 bump
`item_types_version`(改了詞表)都會強制全量重新索引。**

> **設計理由**
>
> - **為何把 `published_at`、時間戳排除在雜湊外**:它們純為 metadata、不影響估價,且會隨商品
>   改版抖動;納入會讓無意義的變動觸發整品 re-embed。content_hash 只覆蓋「會影響向量內容」
>   的欄位,保持決定性。
> - **為何 item-type 標籤不進 content_hash**:分類結果非決定性(可能走 LLM),與 hash 脫鉤可
>   保持 gating 的決定性;分類版本改動改由獨立的 `item_types_version` 條件觸發重建。
> - **第三個重建觸發(換 embedding model)**:不同模型/維度的向量不相容,且本版改了向量 ID
>   方案與 document 格式。換 model 無法靠 hash 偵測,須手動 `rm -rf chroma/` 後
>   `crawl --force-refetch`(詳見 [CLAUDE.md](../CLAUDE.md) Gotchas)。

---

## 階段 6:拆解為 priceable items

**對應 function**:`decompose_items()`
([items.py:144](../estimator_king/sync/items.py#L144)),由
`_rebuild_product_items()`([engine.py:224](../estimator_king/sync/engine.py#L224))呼叫。
輸出 `ProductItem`([items.py:22](../estimator_king/sync/items.py#L22))與
`DecomposeResult`(含三種排除計數)。

> **設計理由(為何索引單位是 item 而非 product)**
>
> 估價的對象是「單一可比價品項」,但商品頁常把多個不同價品項塞進一個 product 的 variant
> (實測逾六成 product 含多種價格、約三成含「セット」variant)。舊的「一 product 一向量」
> 方案只能取 `min(所有 variant 價格)` 當代表,嚴重失真(例:含 ¥995 壓克力盤的商品被記成
> ¥140 語音價);整份 embedding 也被 talent / 活動名主導,查「ネックレス」會撈回同 talent
> 而非同類型商品。改成「每 item 一向量」後,粒度才與估價對象對齊,這也是後續 document 把
> `{item_type}` 前移(階段 8)、ID slug 含價格(階段 9)的根因。

四階段流水線:

### Step 1+2 — variant 過濾([items.py:152-165](../estimator_king/sync/items.py#L152))

- `_strip_prefix()` 取 Shopify `"X / Y"` 中 `/` 前綴([items.py:42](../estimator_king/sync/items.py#L42))。
- **排除 SET**:前綴以「セット」開頭 → `excluded_set += 1`。
- **排除 ¥0**:`_price_to_int()` 解析失敗或價格為 0 → `excluded_zero += 1`。
- 其餘保留為 `(residual, price_int, variant_id)`。

### Step 3 — talent-gated 去重([items.py:167-202](../estimator_king/sync/items.py#L167))

- 先依**價格分組**(同價才可能合併)。
- `_canonical_key()`([items.py:66](../estimator_king/sync/items.py#L66)):以
  `[\s（）()]+` 切 token,**貪婪最長 n-gram(上限 4 token)**移除才能名,得到
  canonical key 與被移除的才能;才能比對為**忽略空白**(`normalize_text(t).replace(" ", "")`)。
- **合併規則**:同一價格、同一 canonical key 的群組,若**成員數 ≥ 2 且至少一個移除過才能**
  → 併成單一 item(name = key,才能取聯集,`variant_ids` 全納入);否則各 variant 各自獨立。

合併的目的是把「同一商品被 talent 拆成數十筆」收斂成一筆(例:某語音包 17 個 talent voice
variant 原各成一向量、污染檢索;某周邊 25 個 talent 變體合併成 1 筆)。

> **設計理由**
>
> - **為何用 token 化 + n-gram 串接,而非字串 `replace`**:talent 在 variant 名有兩種寫法會
>   打敗單純比對 —— *黏著修飾詞*(`ときのそら（日本語）`:不把全形括號當分隔符就切不出
>   `ときのそら`)與*內部空白*(`小雀 とと` 被 `.split()` 切成兩 token,皆 ≠ 字典裸名
>   `小雀とと`)。必須先 token 化、再跨 token 串接後比對才命中。
> - **為何 n-gram 上限 4 token**:`姓 名` 空格形本身是 2 token,上限 4 留 buffer 涵蓋罕見更長
>   的空格名,同時避免無上限串接的 O(n²) 退化與虛假命中。
> - **為何忽略空白比對**:才能字典存「無空白形」(`小雀とと`),但 variant 實務上會寫成
>   `小雀 とと`(全/半形空白皆有);`normalize_text` 先收斂全形空白再 `.replace(" ", "")`,
>   讓兩種寫法收斂到同一字典項。
> - **為何同價才合併**:合併 item 的 `price_jpy` 取該組共同價格;同價是前提保證即使發生非預期
>   合併,組內每筆價格仍正確、估價不失真。不同價格結構上不可能進同一群組。
> - **為何要「至少一個移除過才能」(`removed_any`)**:這是放寬「空 key 防呆」後唯一的守門人。
>   含非 talent token 的殘餘必得非空 key、被分到自己的群組;只有「整串就是 talent 名」或
>   「整串空白」才會落入空 key 群組。`removed_any` 確保兩個純空白退化 variant 不會被誤併。
> - **為何需要 talent 字典而非純結構模板偵測**:themed series(共享長主題前後綴的系列商品)在
>   結構上(LCP/LCS)與 talent 列舉無法區分,純結構偵測會誤併不同品項;唯一可靠訊號是「變動的
>   token 是否為 talent」。

### Step 4 — 命名 + snippet([items.py:204-220](../estimator_king/sync/items.py#L204))

- 合併 item:name = canonical key(若 key 為空則用商品標題)。
- 獨立 item:name = `normalize_text(residual)`。
- `_extract_snippet()`([items.py:88](../estimator_king/sync/items.py#L88)):盡力從
  `html_details` 找最相關段落 —— 核心字串(≥4 字)子字串命中,或 fallback 至
  **共享 ≥2 個有意義 token**(長度 ≥2)才算命中。

> **設計理由**
>
> - **為何合併 item 用 canonical key 命名而非一律商品標題**:item ID slug 由
>   `(item_name, price)` 決定;同一商品同一價格可能有多個合併組(例:語音的 `日本語` /
>   `英語` / `インドネシア語` 同為 1000 円)。若都 fallback 成商品標題,slug 會相同而互相
>   覆寫;用共同部分命名才能讓它們得到相異 slug。
> - **為何 snippet 用子字串而非純 token 交集**:日文品項名約半數是無空白的單一 token,純 token
>   交集(≥2)命中率僅約一成;改用核心子字串包含後,實體商品命中率提升到約四成五。snippet 補上
>   variant 名常缺載的 size/材質,讓 embedding 與 chat model 不必靠標題猜。

### Post-pass — 套組(bundle)過濾([items.py:222-230](../estimator_king/sync/items.py#L222))

`_is_bundle()`([items.py:119](../estimator_king/sync/items.py#L119)),對每個 item
以「留一法」拿其餘 item 當 peers 比較:

- **(A) 關鍵字直接排除**:item_name 含 `bundle_set.keywords` 任一子字串 → 排除(不論價格)。
- **(B) 高價套組排除**:item_name 含「セット」**且**不在 `keep_keywords`**且**
  `price_jpy / median(peer 價格) >= price_ratio` → 排除。

排除數計入 `excluded_bundle`。

> **設計理由**
>
> - **為何分 A(關鍵字)/ B(價格)兩條,而非單靠價格**:資料分析顯示純價格門檻會漏掉大量真
>   套組(其價格比例甚至低於中位數),反觀 `グッズセット` 等關鍵字幾乎是完美判別器。故**關鍵字
>   為權威依據(A,不看價格)**,**價格只當補抓的 tie-breaker(B)**。
> - **為何 (B) 額外要求名稱含「セット」**:限定在 set 名項目,避免把一般高價單品(如限定服飾)
>   誤判成套組砍掉。
> - **為何用中位數而非平均**:真套組常混入高價服飾尺寸(M/L/XL)把基準價拉高,中位數對這種離群
>   值較不敏感。peer 價格只納入 `price_jpy > 0`(防呆,避免 ¥0 拉低基準)。
> - **為何用 leave-one-out peers、且 median 在「過濾前完整集合」上計算**:每個 item 各自以「其餘
>   全部 item(含後續也會被排除者)」為 peers、各自排除自身;基準在排除前的完整集合上算,**與
>   排除順序無關**,是穩定性不變量 —— 維護時切勿改成「邊過濾邊縮 peer 集合」。
> - **`keep_keywords` 為空時 (B) 仍生效**:此時白名單判斷恆為真,(B) 仍對任何「含『セット』且
>   ratio ≥ `price_ratio`」的 item 生效,並非「完全不過濾」。
> - **與 Step 1 的 SET prefix 過濾互補**:Step 1 擋的是 variant 選項前綴(剝除前綴後不留痕);
>   本 post-pass 只看殘餘 `item_name`。前綴為 `セット` 但殘餘不含「セット」(如 `セット / 全部入り`
>   → 殘餘 `全部入り`)只有 Step 1 擋得到,故兩道關卡缺一不可。

| Config(`bundle_set`) | 預設 | 作用 |
| --- | --- | --- |
| `keywords` | グッズセット / フルセット / 応援セット / 各語言セット | 無條件排除的整套組關鍵字 |
| `price_ratio` | 5.0 | 含「セット」且 ≥ 中位數 × 此倍數 → 視為套組排除 |
| `keep_keywords` | ステッカーセット / 缶バッジセット … | 白名單,即使含「セット」也保留 |

---

## 階段 7:item-type 分類

**對應 function**:`classify_item()`
([typing.py:72](../estimator_king/sync/typing.py#L72)),由
`_rebuild_product_items()` 以 `f"{item.item_name} {item.product_title}"` 為輸入呼叫
([engine.py:252-256](../estimator_king/sync/engine.py#L252))。兩層策略:

### Tier 1 — 詞表最長子字串(零 LLM、決定性)

`_vocab_hits()`([typing.py:39](../estimator_king/sync/typing.py#L39)):
`normalize_text` 後,取 `item_types` 中所有出現於文字的詞,**依長度由長到短排序**
(更具體者勝,例如「アクリルキーホルダー」勝過「キーホルダー」)。

- **恰好 1 個命中** → 直接回 `TypeDecision(hit, "vocab")`。

### Tier 2 — 快取 + 小模型 fallback

`_llm_classify()`([typing.py:51](../estimator_king/sync/typing.py#L51)):當命中 0 個
**或** 多個時:

- 先查 SQLite `item_type_cache`(key = `SHA-256(normalize_text(text):version)`,
  [typing.py:47](../estimator_king/sync/typing.py#L47))→ 命中回 `source="cache"`。
- 否則呼叫 `TypingProvider.classify_via_llm()`
  ([typing_provider.py](../estimator_king/llm/typing_provider.py),模型
  `TYPING_MODEL`,預設 `gpt-4o-mini`,`response_format=json_object`),結果寫回快取。
- **Floor / fallback**:LLM 失敗或回傳不在 `item_types` 內 → 一律
  `その他`([typing.py:64-66](../estimator_king/sync/typing.py#L64))。

注意:`classify_item`(寫入端)永遠回傳**恰好一個** type(`その他` 為底);
`classify_query`(查詢端)則可回 0..N 個 type。

> **設計理由**
>
> - **為何 `item_type` 必須是單一純量(而非存 list)**:ChromaDB 雖能在 metadata 存 list,但
>   `where` 過濾**無法**比對 list 成員;查詢端需要 `where={"item_type": T}`,故寫入端必須把
>   每個 item 定為單一 type。複合品項(如「ぬいキーホルダー」)的 recall 由 embedding 含全名 +
>   純 embedding 補查保住。
> - **為何 LLM client 採 lazy 建構**:crawl 路徑常只設 `EMBEDDING_API_KEY`,此時 `typing_api_key`
>   cascade 後為空字串;若 eager 建 `OpenAI(api_key="")` 會直接 raise 使 crawl exit。穩態下第一層
>   詞彙命中即零 LLM,lazy 化讓正常 crawl 永不觸發 client 建構。

| Config | 作用 |
| --- | --- |
| `item_types` | 分類詞表(YAML 陣列) |
| `item_types_version` | 快取 key 一部分;bump 即讓舊快取失效並強制重新分類 |

---

## 階段 8:嵌入(embedding)

**對應 function**:`_format_item_document()`
([engine.py:88](../estimator_king/sync/engine.py#L88)) →
`EmbeddingProvider.embed_documents()`
([embeddings.py](../estimator_king/llm/embeddings.py))。

### Document 格式([engine.py:88-92](../estimator_king/sync/engine.py#L88))

```text
{item_type} {item_name}

# {product_title}

{detail_snippet}        ← 僅在 snippet 非空時附上
```

### 嵌入限制

- **doc prefix**:每段前面加上 `embedding_doc_prefix`(預設空)。
- **批次**:預設每批 100 段送出。
- **截斷** `_truncate()`:超過 `embedding_max_tokens`(預設 8192)以 tokenizer 截斷
  (無 tokenizer 時退化為字元上限);因價格相關內容置前,尾端截斷影響小。

| Config(環境變數) | 預設 | 作用 |
| --- | --- | --- |
| `EMBEDDING_MODEL` | `text-embedding-3-large` | 嵌入模型 |
| `EMBEDDING_DIMENSIONS` | 1024 | 向量維度(空字串 → None) |
| `EMBEDDING_MAX_TOKENS` | 8192 | 單段截斷上限 |
| `EMBEDDING_DOC_PREFIX` | "" | 文件側前綴 |

---

## 階段 9:寫入 ChromaDB 與 SQLite

### 向量 ID — 每個 item 一個向量

`_item_slug()`([engine.py:81](../estimator_king/sync/engine.py#L81)):
`SHA-256(normalize_text(item_name)\x1f{price_jpy})` 取前 16 碼。
完整 ID([engine.py:260](../estimator_king/sync/engine.py#L260)):

```text
{store_id}:{product_id}:{16碼 slug}
```

> **設計理由**:slug 把價格併入,使同商品內「名稱相同但價格不同」的非合併 variant 得到相異
> ID、不互相覆寫。不同 size 因價格不同會落在不同價組 → 不合併 → 各自成獨立 item,正是靠這點
> 區分。

### item 層級的增量寫入([engine.py:262-279](../estimator_king/sync/engine.py#L262))

- `_item_hash() = SHA-256(document\x1f{price_jpy}\x1f{item_type})`
  ([engine.py:95](../estimator_king/sync/engine.py#L95))。
- 若 ChromaDB 既有同 ID 的 `item_hash` 相同 → `embed_status="skipped(unchanged)"`,
  **不重新嵌入**;否則才嵌入並 `VectorStore.upsert()`。
- **清除陳舊向量**:本次 `desired_ids` 以外的同商品舊向量 →
  `vector_store.delete(stale)`([engine.py:289-291](../estimator_king/sync/engine.py#L289))。
  這道差集刪除是必要的:variant 消失、或**合併關係改變**(例:新增 variant 觸發/解除合併)時,
  舊 item 的向量 ID 會與新的不同,不刪會殘留為幽靈結果。

### ChromaDB collection 與 metadata

`VectorStore`([store.py:27](../estimator_king/vectorstore/store.py#L27)):單一
collection `"products"`,距離度量 `{"hnsw:space": "cosine"}`。
`upsert()` 寫入的 metadata([engine.py:266-277](../estimator_king/sync/engine.py#L266)):

```text
store_id, product_id, product_url, product_title, item_name,
item_type, price_jpy(int), published_at(epoch), detail_snippet, item_hash
```

### SQLite `products` 表 — 成功路徑的唯一 writer

`sync_products()`([engine.py:134](../estimator_king/sync/engine.py#L134))是成功路徑上
**唯一寫 product row 的地方**。無論索引成功與否,**最後一定 `repository.upsert()`**
([engine.py:205-220](../estimator_king/sync/engine.py#L205)):

| 情況 | `content_hash` | `last_indexed_at` | `consecutive_failures` | `last_fetch_success_at` |
| --- | --- | --- | --- | --- |
| 未變更(skip) | 不變 | 維持舊值 | 重設 0 | now |
| 變更且重建成功 | 更新 | **推進到 now** | 重設 0 | now |
| 變更但嵌入/向量失敗 | 更新 | 維持舊值(fire-and-forget) | 重設 0 | now |
| 抓取失敗(在 snapshot 之前) | — | 維持舊值 | **+1**(於 async_pipeline) | 維持舊值 |

- 嵌入/向量失敗時記 log 並 `result.failed += 1`,但**不**推進 `last_indexed_at`,
  row 仍以 `consecutive_failures=0` upsert,且 sitemap 追蹤欄位
  (`last_seen_in_sitemap_at`、`consecutive_sitemap_misses`)沿用舊值,確保 fetch 不會
  覆寫掉 sitemap 狀態。
- **schema migration**:`item_types_version` 欄位以 idempotent `ALTER TABLE ... ADD COLUMN`
  追加,既有 DB 可加性升級([repository.py](../estimator_king/database/repository.py))。

---

## 階段 10:失活掃描(跨 store 一次)

**對應 function**:`mark_inactive_products()`
([inactive.py:35](../estimator_king/sync/inactive.py#L35)),在所有 store 處理完後執行一次。

對每個活躍品(`inactive = 0`):

- `consecutive_failures >= inactive_failure_threshold`(預設 3)→
  reason `fetch_failure_threshold_exceeded`(失敗優先判定)。
- 否則 `consecutive_sitemap_misses >= inactive_sitemap_miss_threshold`(預設 4)→
  reason `sitemap_miss_threshold_exceeded`。
- 命中者標記 `inactive=True` + `inactive_since=now`,並**刪除其向量**
  `vector_store.delete(deactivated)`。

| Config | 預設 | 作用 |
| --- | --- | --- |
| `inactive_failure_threshold` | 3 | 連續抓取失敗達此值 → 失活 |
| `inactive_sitemap_miss_threshold` | 4 | 連續 sitemap miss 達此值 → 失活 |

---

## 回傳 counters

`run_crawl_cycle()` 回傳 `dict[str, int]`
([cycle.py:35](../estimator_king/crawler/cycle.py#L35));CLI `crawl` 以
`json.dumps` 印到 **stdout**:

| 欄位 | 累加來源 |
| --- | --- |
| `discovered` | `populate_queue_from_sitemap` 新入隊數 |
| `fetched_ok` | `PipelineResult.processed`(成功處理數) |
| `created` / `updated` | `sync_products` 新建 / 更新的商品數 |
| `skipped` | 未變更而跳過數(`PipelineResult.sync_skipped`) |
| `inactive` | 本輪標記失活數 |
| `errors` | 各階段例外 + `PipelineResult.failed`(抓取/同步失敗) |

> `PipelineResult`([async_pipeline.py:37](../estimator_king/crawler/async_pipeline.py#L37))
> 另含更細的統計(`items` / `excluded` / `detail_hits` / `typing_vocab|cache|llm` /
> `embed_indexed`),會寫入 log,但不進入頂層 counters。

---

## 第二部 — 查詢端(`/estimate` → 價格估計)

查詢端與寫入端共用同一個 ChromaDB collection、同一個 embedding model 與同一份
`item_types` 詞表。Bot 啟動時 `build_bot()`
([runner.py:33](../estimator_king/bot/runner.py#L33))把 config 的 `estimator_*`
參數注入 `Estimator`,並註冊 `/estimate` slash command。

---

## 階段 11:Discord 入口與輸入解析

**對應 function**:`setup_commands()` / `ProductInputModal`
([commands.py:185](../estimator_king/bot/commands.py#L185)、
[commands.py:114](../estimator_king/bot/commands.py#L114))。

1. `/estimate` → `interaction.response.send_modal(ProductInputModal(...))`
   彈出多行輸入框(`TextStyle.paragraph`)。
2. `parse_product_lines()`([commands.py:20](../estimator_king/bot/commands.py#L20)):
   依換行切分 → 各行 `strip()` → 濾掉空行。
3. **輸入限制**:
   - modal `max_length = MAX_INPUT_LENGTH = 2000`(Discord 端硬限制)。
   - 解析後行數須 **≥ 1**,否則回 `❌ Please enter at least 1 product name`。
   - 行數 **≤ `MAX_PRODUCTS = 10`**,超過回 `❌ Maximum 10 products allowed`。
4. 通過後 `interaction.response.defer(thinking=True)`,呼叫
   `Estimator.estimate_products(product_list, "discord-{user_id}")`,結果交
   `format_estimates()` 後逐頁 `followup.send`。`EstimationError` → 顯示估價失敗;
   其他例外 → 顯示未預期錯誤。

---

## 階段 12:分批 + 每行 type-aware 檢索

**對應 function**:`Estimator.estimate_products()`
([estimator.py:88](../estimator_king/bot/estimator.py#L88)) →
`_estimate_chunk()`([estimator.py:106](../estimator_king/bot/estimator.py#L106))。

- **分批**:以 `CHUNK_SIZE = 10` 把輸入行切批,每批一次 chat 呼叫
  ([estimator.py:95-100](../estimator_king/bot/estimator.py#L95))。
- **每行查詢**([estimator.py:108-123](../estimator_king/bot/estimator.py#L108)):
  1. `embed_query(name)`([embeddings.py:71](../estimator_king/llm/embeddings.py#L71)):
     套用 `embedding_query_prefix`(預設空),回單一查詢向量。
  2. `classify_query()`([typing.py:85](../estimator_king/sync/typing.py#L85)):詞表
     最長子字串命中回 **全部**(0..N 個)type;命中 0 個時走 LLM,落 `その他` 則回空 list。
  3. **type-aware 檢索**:`queries = [{"item_type": t} for t in types] + [None]` —— 每個
     命中 type 各跑一次 `where` 過濾查詢,**再加一次無過濾 plain query**;每次取
     `fetch_n = top_k × fetch_multiplier` 筆(`VectorStore.query`,cosine 距離,
     [store.py:75](../estimator_king/vectorstore/store.py#L75))。
  4. **合併去重**:以 `hit.id` 為鍵合併,保留 **distance 最小** 的那筆
     ([estimator.py:121-123](../estimator_king/bot/estimator.py#L121))。

> **設計理由**
>
> - **為何要 type-aware(per-type `where` 過濾)**:純語意查詢的候選池常被「同類型且同價的
>   近似重複品」佔滿,擠掉其他同類型參考。對每個分類命中的 type 各跑一次 `where` 過濾,保證
>   候選池一定含真正同 `item_type` 的品項(再由 SYSTEM_PROMPT 指示優先採用同類型)。
> - **為何永遠多跑一次 plain query**:`classify_query` 回空(分類落 `その他`)時,type 查詢
>   沒有候選;plain query 是這種情況的保底,也能撈出 type 過濾會排除、但語意很近的跨類型參考
>   (prompt 視為 weak signal)。
> - **為何合併取 distance 最小**:同一向量會同時出現在多個 type 查詢與 plain query、距離不同;
>   取最小者確保候選以「最相關的那次命中」計分。去重以向量 id 為單位,因寫入端是「每 item 一
>   向量」,故等同 item 粒度去重。
> - **為何 over-fetch(`top_k × fetch_multiplier`)後才 rerank 取 top_k**:若只取 1×,recency
>   與 diversity 只能在既有視窗內重排,無法把視窗外更新/更分散的同類型品換進來。校準顯示 fetch
>   倍率才是抗價格通膨的主槓桿(純 recency 在 1× 只把參考平均日期推 +21 天,2× 時躍升至
>   +230 天)。**LLM 仍只收到 top_k 行**,故 over-fetch 只增加向量查詢與 rerank 成本(候選池
>   約數十筆,可忽略),不增加 LLM 成本。

---

## 階段 13:Rerank(recency 加權 + diversity MMR)

**對應 function**:`Estimator._rerank()`
([estimator.py:135](../estimator_king/bot/estimator.py#L135)),回排序後清單再
`[: top_k]`([estimator.py:124](../estimator_king/bot/estimator.py#L124))。

- **基礎分數** `base()`([estimator.py:142-149](../estimator_king/bot/estimator.py#L142)):

  ```text
  base = (1 - distance) + recency_weight × recency_norm
  ```

  - `1 - distance`:cosine 距離轉相似度(約 0..1)。
  - `recency_norm`:對候選池中 `published_at > 0` 者做 min-max 正規化
    `(pub - min_pub) / span`;`span == 0` 或 `pub == 0` 時為 0。
- **diversity 貪婪 MMR**([estimator.py:155-171](../estimator_king/bot/estimator.py#L155)):
  逐筆挑選,每次選 `adjusted = base - diversity_weight × dup` 最大者,其中 `dup` =
  **已選清單中相同 key 的數量**,key = `(item_type, price_jpy)`
  ([estimator.py:151-153](../estimator_king/bot/estimator.py#L151))。
- **參考格式** `_format_reference()`([estimator.py:173](../estimator_king/bot/estimator.py#L173)):
  每筆輸出 `item_name | item_type | product_title | ¥price | 年-月 | store`,並附最多
  120 字的 `detail_snippet`。日期由 `published_at` 轉 `YYYY-MM`,缺則 `?`。

| Config(`estimator`) | 預設 | 作用 |
| --- | --- | --- |
| `top_k` | 10 | 每行最終送進 prompt 的參考數 |
| `fetch_multiplier` | 2 | over-fetch 倍率(每次查詢取 `top_k × 此值`) |
| `recency_weight` | 0.05 | 新近度在 base 分數的權重 |
| `diversity_weight` | 0.05 | `(item_type, price)` 重複的每筆懲罰強度 |

> **設計理由(含校準結論)**
>
> - **為何 recency 用 per-pool min-max 正規化**:把新近度壓到與相似度同量級的 0..1,單一權重即可
>   平衡兩者;且基準相對於「本次查詢的候選池」,絕對發行日期不影響。
> - **為何 `recency_weight` 小到 0.05**:這是能完全重排「相似度近乎平手帶」又**不越過任何候選池
>   top-1 相似度斷崖**的最大權重(相似度成本平均僅 −0.0006)。真正的抗通膨主力是 SYSTEM_PROMPT
>   的「偏好近期價」指示 + 每筆參考可見的日期,rerank recency 只是低風險的次要微調。
> - **為何 diversity key 取 `(item_type, price_jpy)`、用線性「已選同 key 數」懲罰**:失敗模式是
>   top_k 被「同類型且同價」近似重複品塞滿(實測某查詢 10 格有 7 格是 `缶バッジ/¥500`)。對精確
>   的 `(type, price)` 配對懲罰能把價格分佈攤開,讓 LLM 看到多元價格點;不同價或不同類型互不懲罰,
>   保留完整價格跨度。第一筆 `dup=0` 確保最相關者必先入選(`diversity_weight=0` 退化為純 base 排序)。
> - **校準結論**:採用點 `(fetch=2, recency=0.05, diversity=0.05)` 在各指標上全面優於舊生產設定
>   `(1, 0.05, 0)`(distinct `(type,price)` 6.4→8.9、參考平均日期 +21d→+106d),相似度成本 <1%。
>   **recency 與 diversity 互相耦合**:最新品常聚在同一 `(type,price)` 群,diversity 會把
>   recency 的抗通膨增益約砍半(+230d→+106d)卻保住幾乎全部跨度 —— 故兩者不可獨立調:重抗通膨就
>   調低 `diversity_weight`,重價格多元就調高。

---

## 階段 14:Chat 估價、對齊與輸出

**對應 function**:`_estimate_chunk()` 末段 + `ChatProvider.estimate()`
([chat.py:58](../estimator_king/llm/chat.py#L58))+ `_reconcile()`
([estimator.py:252](../estimator_king/bot/estimator.py#L252))+ `_snap_estimate()`
([estimator.py:94](../estimator_king/bot/estimator.py#L94))+ `format_estimates()`
([commands.py:36](../estimator_king/bot/commands.py#L36))。

1. **組 prompt**([estimator.py:171-189](../estimator_king/bot/estimator.py#L171)):每行
   參考組成 `### Query: {name}\n{refs}`(無命中則 `(no matches)`),整批拼成 user prompt;
   `SYSTEM_PROMPT`([estimator.py:16](../estimator_king/bot/estimator.py#L16))以 XML 區塊要求:
   每行一筆估價、同序不漏、**只能**用提供的參考(禁止引用參考以外的一般「相場」行情)、
   references 採嚴格優先序 **item_type > size/材質 > recency**(recency 僅作 tie-breaker、
   不得蓋過更接近的同類比對)、帶參考所無的溢價特徵/素材(温感、もこもこ／あったか、加大、
   なりきり等)時錨定同類參考**上端**、價格為含稅且必為 **¥110 整數倍**、price_range 約
   **±25–30% 且偏上**、confidence `high` 需同名/同型近似 exact 且 suggested 落在同類參考
   價格跨度內、最多 3 筆 `reference_products`、無強匹配仍給 `low` 估價而非捏造。
   輸出欄位不在 prompt 重述,由 `response_format=EstimateBatch` schema 強制。
2. **結構化輸出**([chat.py:58-100](../estimator_king/llm/chat.py#L58)):
   `chat_structured_output=True`(預設)→ `chat.completions.parse` 綁
   `EstimateBatch` schema;否則走 `json_object` 模式手動驗證(供無嚴格 schema 的 endpoint,
   如 ollama)。模型 refusal 或無法解析 → 丟 `EstimationError`。
   輸出型別:`EstimateBatch{ estimates: [ProductEstimate{ product_name,
   suggested_price_jpy, price_range_jpy{min,max}, confidence, rationale,
   reference_products[] }] }`([chat.py:15-37](../estimator_king/llm/chat.py#L15))。
3. **對齊回輸入** `_reconcile()`([estimator.py:252](../estimator_king/bot/estimator.py#L252)):
   以 `normalize_text` 正規化後當鍵,把每筆估價對回原輸入行並保持原順序;**缺漏的行**補上
   `confidence="low"`、`¥0` 的佔位估價(確保輸出行數 == 輸入行數);LLM 多回的重複估價以
   `setdefault` 保留首筆、其餘丟棄並 log warning。
4. **含稅格點正規化(snap)**([estimator.py:94](../estimator_king/bot/estimator.py#L94)):
   `_reconcile` 之後,對每筆估價的 `suggested_price_jpy` 與 `price_range_jpy{min,max}`
   各自 round 到最近的 **¥110** 倍數(`snap_to_tax_grid` / `_snap_estimate`);
   平手(餘 55)往上;非正數的「無估價」哨兵維持 `0`;snap 後強制 `min ≤ suggested ≤ max`。
5. **輸出 Discord** `format_estimates()`:每筆組成價格 / 區間 / 信心 / rationale(截斷 300 字)/
   參考清單;依 `max_length = 2000` 自動分頁成多個 embed(標題標 `page i/total`)。

> **設計理由**
>
> - **為何以 `CHUNK_SIZE=10` 分批**:限制單次 chat 的 payload(10 個查詢區塊 × 每塊至多 top_k 筆
>   參考),也讓「每行一筆、同序、不漏」這條成功準則在單次呼叫內可控。
> - **為何 reconcile 用 `normalize_text` 對齊**:LLM 回傳的 `product_name` 可能有空白/全半形/大小寫
>   差異;兩側都正規化後才能精準對回輸入行與順序,缺漏補佔位、多餘靜默丟棄,保證輸出可預期。
> - **prompt 與檢索設計呼應**:「優先同 `item_type`」對應 per-type `where` 檢索;
>   **recency 在 prompt 端已降為僅 tie-breaker**——同類比對接近度優先,rerank 的
>   `recency_weight` 因此維持很小,只做同等可比時的微調而非主導;「依 item_name/detail
>   比對 size/材質」正是 `_format_reference` 要輸出 `item_name`、`item_type`、
>   `product_title`、價格、日期、store 與 snippet 這幾個欄位的原因。
> - **為何 snap 到 ¥110 格點**:日本零售價皆為含稅價＝稅前(¥100 整數倍)×1.1,必為 ¥110 整數倍。觀測 12 筆實際定價 12/12 落在此格點、模型自然只 5/12;deterministic 後處理保證輸出落點正確,與 prompt `<price_format>` 形成雙保險。

---

## 設定速查表

| Config 欄位 | 預設 | 影響階段 |
| --- | --- | --- |
| `stores[].id / base_url / locale` | — | 1 枚舉 / 9 metadata |
| `rate_limit_rps` | 1.5 | 3 抓取 |
| `jitter_max` | 0.5 | 3 抓取 |
| `concurrency_per_domain` | 3 | 3 抓取(同時也是 worker 數) |
| `timeout_connect` / `timeout_read` | 10 / 30 | 3 抓取 |
| `max_retries` | 3 | 3 抓取 |
| `max_products_per_run` | 32 | 2 budget |
| `crawl_schedule_hours` | 24.0 | 排程 |
| `inactive_failure_threshold` | 3 | 10 失活 |
| `inactive_sitemap_miss_threshold` | 4 | 10 失活 |
| `item_types` / `item_types_version` | — / 4 | 5 變更偵測 / 7 分類 |
| `talents` | — | 6 去重 |
| `bundle_set.keywords / price_ratio / keep_keywords` | — / 5.0 / — | 6 套組過濾 |
| `_FORCE_CURRENCY`(程式常數) | "JPY" | 4 萃取 |
| `NORMALIZER_VERSION`(程式常數) | 2 | 5 變更偵測 |
| `EMBEDDING_*`(環境變數) | 見階段 8 | 8 嵌入 / 12 查詢(`EMBEDDING_QUERY_PREFIX`) |
| `estimator.top_k` | 10 | 12/13 檢索 |
| `estimator.fetch_multiplier` | 2 | 12 over-fetch |
| `estimator.recency_weight` | 0.05 | 13 rerank |
| `estimator.diversity_weight` | 0.05 | 13 rerank |
| `CHAT_*`(環境變數) | 見階段 14 | 14 估價 |

---

## 相關文件

- [本地操作流程](local-runbook.md)
- [Kubernetes 維運](ops-runbook.md)
- [rerank 權重校準附錄](2026-06-02-rerank-calibration-appendix.md)(階段 13 採用點的實驗數據)
- 專案 README 與 [CLAUDE.md](../CLAUDE.md) 的「Architecture / Gotchas」段落
