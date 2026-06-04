# Talent 清單探勘：從官方 collection 頁面抓取

## 背景與目標

`stores_config.yaml` 的 `talents:` 是一份日文顯示名清單，供 talent-gated dedup
使用（`estimator_king/sync/items.py`：對 variant title 以空白切 token，
逐 token 比對 `if tok in talents`）。因此每個 talent 名必須是「variant title
裡會出現的、以空白分隔的單一 token」（例如 `兎田ぺこら`、`がうる・ぐら`）。

現有 `scripts/mine_talents.py` 以啟發式從 ChromaDB 的 `products` collection
挖掘 token（找同價變體群組中唯一相異的 token）。此法為間接推測，會漏掉尚無足量
商品的新人，且混入雜訊。

本設計改為**從官方 collection 頁面直接抓取權威 talent 清單**：兩個來源站台
（hololive、vspo）各有「成員一覽」頁面，頁面上每位 talent 都是一個獨立 collection，
而每個 collection 的 `.json` `title` 欄位**就是日文顯示名**。

### 已驗證的關鍵事實

- `GET https://shop.hololivepro.com/pages/talent.json` 的 `body_html` 為 `null`
  → 清單由佈景主題 template 動態渲染，page JSON 拿不到清單。
- `GET https://<base>/collections/<handle>.json` 回傳 `collection.title`：
  - hololive：`gawrgura → がうる・ぐら`、`usadapekora → 兎田ぺこら`
    （**與現有 `talents:` 完全吻合**）。
  - vspo：`akari-yumeno → 夢野あかり`、`beni-yakumo → 八雲 べに`、
    `ren-kisaragi → 如月 れん`（**title 帶內部空白**）。
- vspo 商品 variant title 中，名稱**無空白**（`花芽すみれ`、`小雀とと`）。
  → collection title 的內部空白必須去除，才能對上 dedup 的 token 格式。
- 兩站有重疊成員（如 `花芽すみれ`、`花芽なずな`、`小雀とと`）→ 合併需去重。
- 列表頁的 `/collections/<handle>` 連結混入團體/分類/狀態 collection
  （hololive：`hololive_gen0`、`holostarsen`、`all`、`flow-glow`、`friend-a`、
  `uproar`、`shi-wu-suo-sutatuhu`(事務所スタッフ)、`zu-ye-sheng`(卒業)…；
  vspo：`all`、`goods`、`apparel`、`tapestry-poster`、`members`、`en-members`、
  `voice`…）→ 需以 denylist 過濾。此處列舉非窮舉：列表頁日後可能新增分類/團體
  handle，denylist 需隨之維護，而步驟 5 的人工審視是任何新洩漏的最終防線。
- `requests` 已在 `requirements.txt`，且 `.venv` 可 import（`requests 2.32.5`）。

## 範圍

- **重構** `scripts/mine_talents.py`：新增「從官方 collection 頁面抓取」為**預設主路徑**；
  保留現有 ChromaDB 啟發式 `mine_talents()` 函式（改由 `--chroma` 旗標觸發，非預設）。
- 新增三個純函式的單元測試到 `tests/test_mine_talents.py`。
- 執行腳本產生真實清單後，更新 `stores_config.yaml` 的 `talents:` 區塊（人工套用，
  非腳本自動改寫）。

非本設計範圍：改動 dedup 邏輯、改動 crawler、把 `talents` 改為 per-store。

## 設計決策（已與使用者確認）

1. **舊 miner 去留**：新 fetch-based 路徑為 `main()` 預設；舊 ChromaDB
   `mine_talents()` 保留但非預設，以 `--chroma [path]` 觸發。
2. **輸出方式**：腳本印出排序後的 `talents:` YAML（沿用現有 `main()` 慣例）；
   由人工把結果套用到 `stores_config.yaml` 並驗證。腳本**不**自動改寫 config。
3. **過濾方式**：腳本內建 per-store denylist（精確 handle 集合 + handle 前綴）
   自動過濾團體/分類 collection。
4. **名稱正規化**：去除 title 內所有空白（ASCII 半形、全形空白 `　`、tab、換行），
   使之成為單一 token（`八雲 べに` → `八雲べに`）。
5. **合併**：兩站抓到的 title 收進同一個 set，去重後排序，輸出單一全域 `talents:`
   清單（沿用現有結構）。
6. **HTTP client**：使用 `requests`（同步），符合此一次性 CLI 腳本的性質。

## 架構

### 資料源描述（腳本內常數）

```python
@dataclass(frozen=True)
class StoreSource:
    store_id: str
    base_url: str                    # 無尾斜線，例如 "https://shop.hololivepro.com"
    listing_urls: tuple[str, ...]    # 要抓取候選 handle 的列表頁完整 URL
    denylist_exact: frozenset[str]   # 精確比對要剔除的 handle
    denylist_prefixes: tuple[str, ...]  # handle 以這些字串開頭即剔除

STORE_SOURCES: tuple[StoreSource, ...] = (
    StoreSource(
        store_id="hololive",
        base_url="https://shop.hololivepro.com",
        listing_urls=("https://shop.hololivepro.com/pages/talent",),
        denylist_exact=frozenset({
            "all", "flow-glow", "friend-a", "uproar",
            "shi-wu-suo-sutatuhu", "zu-ye-sheng",
        }),
        denylist_prefixes=("hololive", "holostars"),
    ),
    StoreSource(
        store_id="vspo",
        base_url="https://store.vspo.jp",
        listing_urls=(
            "https://store.vspo.jp/collections/members",
            "https://store.vspo.jp/collections/en-members",
        ),
        denylist_exact=frozenset({
            "all", "members", "en-members", "apparel", "goods", "others",
            "digitalgoods", "event-goods", "goods-accessories", "tapestry-poster",
            "voice",
        }),
        denylist_prefixes=(),
    ),
)
```

### 純函式（可單元測試、零網路）

**`extract_collection_handles(html: str) -> set[str]`**
- 以 regex 抽出 `href="/collections/<handle>"` 中的 `<handle>`。
- handle 字元集：`[a-z0-9._-]+`（與實測一致）。
- 排除圖片：丟棄以 `.png`/`.jpg`/`.jpeg`/`.webp`/`.gif` 結尾的 handle
  （CDN 圖片路徑也含 `/collections/` 會誤抓）。
- 回傳去重後的 set。

**`filter_handles(handles, denylist_exact, denylist_prefixes) -> set[str]`**
- 簽名：`(handles: set[str], denylist_exact: frozenset[str],
  denylist_prefixes: tuple[str, ...]) -> set[str]`。
- 剔除：`h in denylist_exact`，或 `h.startswith(prefix)` 對任一 prefix 成立。
- 回傳保留的 set。

**`normalize_talent_name(title: str) -> str`**
- 去除字串內所有空白（ASCII 半形、全形空白 `　` U+3000、tab、換行；含首尾與中間），
  合併為單一 token。
- 實作：`"".join(title.split())`。Python 無參數 `str.split()` 會切所有 Unicode
  空白（含全形空白 U+3000），一次去除所有空白並合併。回傳結果。
- 範例：`"八雲 べに"` → `"八雲べに"`；`"如月　れん"`（全形空白）→ `"如月れん"`；
  `"がうる・ぐら"` → `"がうる・ぐら"`（不變）。

### IO 函式（`# pragma: no cover`）

**`fetch_text(url: str) -> str`**：`requests.get(url, timeout=...)`，
`raise_for_status()`，回傳 `resp.text`。

**`fetch_collection_title(base_url: str, handle: str) -> str | None`**：
GET `f"{base_url}/collections/{handle}.json"`，解析 JSON 取
`payload["collection"]["title"]`；缺欄位或非 str 時回傳 `None`（容錯：個別
collection 失敗不應中斷整體）。

**`mine_talents_from_stores(sources: tuple[StoreSource, ...]) -> set[str]`**：
對每個 source：對每個 `listing_urls` 抓 HTML → `extract_collection_handles`
聯集 → `filter_handles` → 對每個保留 handle `fetch_collection_title` →
`normalize_talent_name` → 收進全域 set（跳過 `None`/空字串）。回傳合併 set。

### CLI（`main()`）

- 預設（無 `--chroma`）：`names = sorted(mine_talents_from_stores(STORE_SOURCES))`，
  印出 `talents:` YAML 區塊（每行 `  - <name>`）。
- `--chroma [path]`：走舊路徑
  `sorted(mine_talents(_load_docs_from_chroma(path)))`，path 預設 `"chroma"`。
- 以 `argparse` 解析；舊行為（位置參數 `chroma_path`）改為 `--chroma` 的選用值。

### 保留的舊程式碼（不改行為）

- `mine_talents(docs, *, min_freq=20)`：完全保留，供 `--chroma` 路徑與既有測試使用。
- `_load_docs_from_chroma(path)`：保留。

## 資料流

```
listing_urls ──fetch_text──▶ HTML
                              │ extract_collection_handles (聯集多頁)
                              ▼
                          候選 handles
                              │ filter_handles (denylist)
                              ▼
                          保留 handles
                              │ 逐一 fetch_collection_title
                              ▼
                          collection.title
                              │ normalize_talent_name
                              ▼
                          全域 talent 名 set ──sorted──▶ 印出 YAML
```

## 錯誤處理

- 單一 collection `.json` 抓取或解析失敗：`fetch_collection_title` 回傳 `None`，
  該 handle 略過，不中斷整體（容錯，符合「一次性探勘工具盡量多收」精神）。
- 列表頁抓取失敗：直接拋出（列表頁是該站的根入口，失敗代表該站整批無法處理，
  應讓使用者看到錯誤）。
- 名稱正規化後為空字串：略過不收。

## 測試策略

於 `tests/test_mine_talents.py` 新增（既有兩個測試不動）：

1. `extract_collection_handles`：
   - 從含 `href="/collections/azki"`、`href="/collections/gawrgura"`
     及一個圖片 `href="/collections/foo.png"` 的 HTML 片段，斷言抽出
     `{"azki", "gawrgura"}` 且不含 `foo.png`。
2. `filter_handles`：
   - 輸入 `{"azki", "hololive_gen0", "all", "holostarsen"}`，
     denylist_exact=`{"all"}`、prefixes=`("hololive", "holostars")`，
     斷言只剩 `{"azki"}`。
3. `normalize_talent_name`：
   - `"八雲 べに"` → `"八雲べに"`；`"夢野あかり"` → `"夢野あかり"`；
     含全形空白 `"如月　れん"` → `"如月れん"`。

純函式測試零網路。IO 函式標 `# pragma: no cover`。

## 驗證（實作完成後）

1. 型別：`.venv/bin/basedpyright scripts/mine_talents.py`（prod-code 0 錯誤門檻；
   `scripts/` 視為 production code）。
2. Lint：`uvx ruff check scripts/mine_talents.py tests/test_mine_talents.py`。
3. 測試：`.venv/bin/python -m pytest tests/test_mine_talents.py -v -o addopts=""`。
4. 實跑（連網）：`.venv/bin/python -m scripts.mine_talents` 產生真實 YAML。
5. 人工審視輸出（檢查 denylist 是否漏剔團體、是否誤剔個人），套用到
   `stores_config.yaml` 的 `talents:` 區塊。
```
