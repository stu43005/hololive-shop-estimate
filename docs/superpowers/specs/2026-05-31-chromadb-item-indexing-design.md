# 以「品項（item）」為單位的索引與類型感知檢索 — 設計規格

日期：2026-05-31

## 1. 目標

提升 `/estimate` 估價的參考檢索精準度。把 ChromaDB 的索引單位從 **product 改為 item（單一可比價品項）**，並讓檢索能**以品項類型對齊**而非被 talent 名／活動名主導，同時把**真實的單品價格**與**商品新近度**納入估價脈絡。

外部介面（`/estimate` 指令、`crawl`/`run` 行為）不變，改變的是索引內容、檢索策略與送進 chat model 的脈絡品質。

## 2. 現況與問題（已實證）

以實際 DB 取樣（2,654 個 product、23,923 個 variant）驗證出三個結構性根因：

1. **索引粒度與估價粒度錯配**：目前一個 product = 一筆向量（[sync/engine.py::_format_product_document](../../../estimator_king/sync/engine.py)）。但估價對象是**單一品項**。實測 62% 的 product 含多種不同價格的 variant、35% 含「セット」variant、1,525/2,654 個 product 有 ≥5 個 variant。
2. **`price_jpy` metadata 取 `min(所有 variant 價格)`**（[sync/engine.py::_min_variant_price](../../../estimator_king/sync/engine.py)）：混合品項 product 的價格嚴重失真（例：含 ¥995 壓克力盤與 ¥600 包的 product，metadata 只記 ¥140 的語音）。
3. **送進 chat model 的脈絡貧乏**（[bot/estimator.py::_estimate_chunk](../../../estimator_king/bot/estimator.py)）：每筆參考只給 `title | price_jpy | store_id`——只有 product 標題與失真的 min 價格，無 variant 名、無類型。查「ネックレス」時比對的是被 talent／活動名主導的整份文件 embedding，撈回同 talent 商品而非同類型商品。

補充實證發現：

- variant 標題的 `X / Y` option 前綴**不是乾淨的類型欄位**（混有 `グッズ/セット/ボイス` 與 `ホロライブEnglish/0期生/アクリルスタンド/単品販売` 等群組、世代、品項名）。
- size／顏色變體就在 variant 內：每個 size／顏色是獨立 SKU 帶自己的 price（例：vspo Tシャツ option「バリエーション」值為「黒　M／白　XL…」）。**不同 size 不同價時，天生就是不同 variant、各帶各價**。
- Shopify product JSON 含 `created_at`／`published_at` 時間戳（已於 vspo 商品證實，hololive 同為 Shopify、schema 一致）。
- 存在 **¥0 的特典 variant**（贈品）；`数量限定ver.` 多掛在 `セット` 前綴下。

## 3. 目標架構

```text
crawl 路徑（索引時）
  fetch_product → ProductSnapshot
    └─ items.decompose_items(snapshot)            ← 新增：拆品項 + 排除 SET/¥0 + talent-gated 去重
         → list[ProductItem]
    └─ typing.classify_item(item) (逐品項)         ← 新增：兩層分類（受控詞彙 → 小模型 fallback，含快取）
         → item_type
    └─ engine.sync_products(...)                  ← 改寫：逐 item upsert 向量 + 刪除過時 item 向量
         每 item 一筆向量：embedding(品項名+類型+product標題+段落片段)
                          metadata(item_name,item_type,price_jpy(品項價),published_at,...)

estimate 路徑（查詢時）— 逐行（per-line）
  每一行品項文字（含使用者可能附註的 size）
    └─ typing.classify_query(line)                ← 標 0..N 個類型（受控詞彙 → 小模型，含快取）
    └─ 檢索：
         - 對每個命中類型各做一次 where={item_type:T} 向量查詢
         - 另做一次純 embedding 查詢（不過濾）
         - 合併去重
    └─ recency rerank（輕微偏好新商品）
    └─ 組脈絡行：item_name | item_type | ¥price | 日期 | store
  每 CHUNK_SIZE(10) 行打包成一次 chat 呼叫（沿用既有批次，僅省 LLM 呼叫成本）
```

關鍵不變式：
- ChromaDB 仍是單一 collection、單寫入者；改為一個 product 對應**多筆** item 向量。
- `content_hash` 仍只基於來源快照（決定性）；類型標籤與其脫鉤（見 §7）。

## 4. 資料模型

### 4.1 `ProductItem`（新增於 `estimator_king/sync/items.py`）

```python
@dataclass(frozen=True)
class ProductItem:
    product_id: int
    product_title: str           # 來源 product 標題
    item_name: str               # 此品項顯示名（見 §5.3 命名規則）
    price_jpy: int               # 此品項價格（整數 JPY）
    source_variant_ids: tuple[int, ...]   # 合併後對應的原始 variant id（≥1）
    talents: tuple[str, ...]     # 合併時收集到的 talent（可空）
    detail_snippet: str          # 最佳努力擷取的段落片段（可空，見 §5.4）
    published_at: int            # epoch 秒；無法取得時為 0（來源見 §4.3）
```

### 4.2 每 item 向量

- **向量 ID**：`f"{store_id}:{product_id}:{item_slug}"`。`item_slug` = 對 `normalize_text(item_name) + "\x1f" + price_jpy` 取 SHA-256 前 16 hex（價格併入避免同 product 內「同名不同價」的非合併 variant 撞號／互相覆寫）。`normalize_text` 為 `crawler/snapshot.py` 規則（decode HTML entities + collapse whitespace）。`_normalize_text` 目前為 `snapshot.py` 的 module-private 函式，實作時提升為可被 `items.py` import（移除底線或於 `snapshot.py` 匯出）。
- **embedding 文字**（§5.2）。
- **metadata**：
  - `store_id: str`
  - `product_id: str`
  - `product_url: str`
  - `product_title: str`
  - `item_name: str`
  - `item_type: str`（單一純量，見 §6；無法歸類為 `その他`）
  - `price_jpy: int`（此品項價）
  - `published_at: int`（epoch 秒；無法取得時為 `0`）
  - `detail_snippet: str`（§5.4 擷取的規格片段，供查詢端送進 LLM 脈絡；可空）
  - `item_hash: str`（此 item 的內容雜湊，見 §7.2）

> item 向量 metadata **不含** `content_hash`（product 級的 `content_hash` 由 `ProductState` 持有，見 §7.1）；per-item 的重嵌判斷改以 `item_hash` 承載（§7.3）。舊 `_format_product_document` 寫入的 `title`、`content_hash` metadata 鍵在新模型中**不再寫入**。
>
> ChromaDB 1.5.9 實測：metadata 可存 list 但 `where` 無法對 list 成員過濾（`where={"types":"x"}` 對 list 欄位回傳空）；而查詢需 `where={item_type:T}`，故 `item_type` 必須是**單一純量**。複合品項（如「ぬいキーホルダー」）的 recall 由 embedding 文字含全名 + 純 embedding 補查（§8）保住。

### 4.3 來源時間戳擷取（`ProductSnapshot` 與 shopify parser 擴充）

`published_at` 目前**不存在於資料流**：`ProductSnapshot`（[crawler/snapshot.py](../../../estimator_king/crawler/snapshot.py)）只有 `product_id/title/description/variants/html_details`；`crawler/shopify.py::_build_snapshot_from_product_json` 也未解析時間戳。需新增：

- `ProductSnapshot` 新增欄位 `published_at: int = 0`（epoch 秒，帶預設值）。
- **dataclass 欄位排序衝突（必須同步處理）**：`shopify.py::ProductSnapshotWithHash(ProductSnapshot)` 目前宣告 `content_hash: str`（**無預設值**）。基底新增帶預設值的 `published_at` 後，dataclass 規則「有預設欄位後不可接無預設欄位」會在 class 定義時拋 `TypeError: non-default argument 'content_hash' follows default argument`，使 `shopify.py` 無法 import。**修正**：將 `ProductSnapshotWithHash.content_hash` 改為帶預設值 `content_hash: str = ""`（其建構處 [shopify.py:147](../../../estimator_king/crawler/shopify.py) 一律顯式帶入，預設值僅為滿足排序規則）。
- `shopify.py::_build_snapshot_from_product_json` 從 product JSON 的 `published_at`（Shopify 為 ISO8601 字串，如 `2023-06-30T19:00:07+09:00`；缺失則回落 `created_at`，再缺則 `0`）解析為 epoch 秒（`datetime.fromisoformat` → `int(dt.timestamp())`）；解析失敗則 `0`。`_build_snapshot`／`fetch_product` 透傳此欄位至 `ProductSnapshotWithHash(...)`（[shopify.py:147](../../../estimator_king/crawler/shopify.py) 的建構新增 `published_at=snapshot.published_at`）。
- **決定性保證**：`canonicalize_snapshot`／`compute_content_hash`（[snapshot.py](../../../estimator_king/crawler/snapshot.py)）**維持不納入** `published_at`（其註解已明示排除時間戳）。`published_at` 純為 metadata，**不得**進入 `content_hash`，否則商品改版時間會讓 hash 抖動。
- `ProductItem.published_at` 由所屬 product 的 `snapshot.published_at` 帶入（同一 product 的所有 item 共用）。

## 5. 品項拆解模組 `estimator_king/sync/items.py`（新增）

`decompose_items(snapshot: ProductSnapshot, *, talents: frozenset[str]) -> list[ProductItem]`

### 5.1 流程

1. **排除 SET**：variant 標題的 option 前綴（`split(" / ", 1)` 的左段）以「セット」開頭者，整筆丟棄不索引。
2. **排除 ¥0**：`price` 解析為 0 的 variant 丟棄（贈品／特典，非可比價）。
3. **talent-gated 去重**：見 §5.2。
4. **命名**：見 §5.3。

### 5.2 去重規則（talent-gated，取代純結構模板偵測）

> 純結構（LCP/LCS）模板偵測經實證**不可靠**：themed series（如「Start your Journey ポーチ／りんご型プレート」「活動三周年 Tシャツ／トートバッグ」）共享長主題前後綴，結構上與 talent 列舉無法區分，會誤併不同品項。唯一可靠訊號是「變動 token 是否為 talent」，故需 talent 字典。

去重演算法以**等價類（canonical key）** 定義，天然具傳遞性，不需 union-find：

1. 依 `price_jpy` 分組。
2. 對每筆殘餘標題（剝除 option 前綴後）以空白切 token，計算 **canonical key**：
   - 移除標題中**所有屬於 `talents`** 的 token（以 `_normalize_text` 正規化後逐 token 比對 `talents` 集合，移除命中者），其餘 token 依原順序以單一空白接回，即為 canonical key。
3. 同一同價組內 **canonical key 相同**的多筆殘餘標題合併為**一筆 item**；合併需滿足：(a) 被移除的 talent token 至少一筆非空（確認差異確由 talent 造成、而非完全相同字串）；(b) canonical key 非空（避免整串都被當成 talent 而誤併）。
4. 合併的 item：`source_variant_ids` 收集全組、`talents` 收集各筆被移除的 talent token（去重）、`price_jpy` 為該組價格。
5. 不滿足合併（canonical key 互異，如 themed series「ポーチ」vs「りんご型プレート」其 token 非 talent 故 key 不同）者，每個 variant 各自成一筆 item。

不同 size 因價格不同會落在不同價組 → 不會被合併 → 各自成獨立 item（解決 §2 size 議題）。

### 5.3 命名規則（`item_name`）

命名規則依序套用以下判定（殘餘＝§5.2 剝除 option 前綴後的標題；合併項則 name 來源見各分支）：

- **整個 product 合併成單一 item 時**（§5.2 去重後 `items` 只剩 1 筆，且該筆 `len(source_variant_ids) ≥ 2`）：`item_name = product_title`（product 標題即品項名，例：Blue Journey ×23 → 「3Dアクリルスタンド Blue Journey衣装ver.」）。
- **variant 殘餘為純選項值時**（剝除前綴後殘餘判定為 size／顏色而非品項描述）：`item_name = f"{product_title} {殘餘}"`。判定條件（OR）：
  - 殘餘以 `_normalize_text` 正規化後（全形空白→半形）以 `str` 計長度 `< 4`（涵蓋「M」「XL」「黒 M」等短選項值）；**或**
  - 殘餘命中 size／規格 pattern：`re.search(r"(^|[\s/])(XX?[SML]|[SML]|フリー)?サイズ|^(XX?[SML]|[SML])([\s/]|$)|フリーサイズ", 殘餘)`（涵蓋「Lサイズ」「XLサイズ」「フリーサイズ」「M」等）。

  例：「黒　M」命中**長度條件**（`_normalize_text` 後為「黒 M」，len 3 < 4；非 size pattern）→ 落此分支 →「ぶいすぽっ！オリジナルTシャツ 黒 M」。
  - **不再以「與 product 標題 token 交集為空」單獨觸發**（實測會對活動／生日商品誤觸發：活動名與品項名本就無共同 token，導致 `イオフィカラー ショルダーバッグ` 被誤併成 `…誕生日記念2022 イオフィカラー ショルダーバッグ`，反而把活動名灌回品項名、重新引入 talent/活動名主導）。
- **其餘**（混合品項、各自獨立）：`item_name = 剝除前綴後的殘餘標題`。

### 5.4 段落片段擷取（`detail_snippet`，最佳努力、決定性、可空）

**動機**：品項名（甚至 variant 標題）不一定載明 size、材質等規格——這些往往**只**存在於 `description`／section content。缺了它，無論 embedding 或 chat model 都只能靠標題「猜」size／材質。因此需從整 product 的段落中**擷取屬於該 item 的規格行**補足，並同時用於 embedding 與查詢端 LLM 脈絡（§9.1）。

`snapshot.html_details`（如「グッズ詳細」「セット詳細」）常以「・<品項名> …」條列各品項規格（含尺寸、材質）。匹配以 **substring/核心包含**為主（token 交集為輔）——實測日文品項名約 51% 為**無空白單 token**，純 token 交集（≥2）對其結構性失效（命中僅 ~11%），改用核心子字串包含後實體商品命中升至 ~45% 且精準。對每個 item：

- **段落切分**：把各 `html_details` 值以分隔符 `・`／`◇`／換行切成候選段，去空白。
- **匹配核心（cores）**：由 `item_name` 經 `_normalize_text` 正規化後產生候選核心字串：完整 `item_name`；若含 ` - ` 則加其前段與後段；再加「移除 talent token 後的核心」（與 §5.2 同 talent 集合）。
- **評分（取最高分的候選段為 snippet）**：
  - 主：某 core（長度 ≥ 4）以**子字串**出現在候選段 → 分數 = 該 core 長度（取最長者）。
  - 輔（無任何 substring 命中時）：候選段與 `item_name` 長度 ≥ 2 的 token 交集數 ≥ 2 → 分數 = 交集數。
  - 全部候選段皆 0 分 → `detail_snippet=""`。
- 擷取不到時**直接略過**（不退回整段 description，避免把整 product 噪音灌進單一 item）。
- **覆蓋為部分（best-effort）**：實體商品約 45% 命中（其餘多為語音／數位品項本就無尺寸材質規格、或該 product 非逐項條列）；命中時精準。此片段**不影響價格**（價格一律取自 variant），僅補充規格語義供檢索與估價判斷，缺失安全降級。

### 5.5 embedding 文字格式（`_format_item_document`，取代 `_format_product_document`）

依序組合（前移類型／品項名以實現類型對齊檢索）：

```text
{item_type} {item_name}

# {product_title}

{detail_snippet}            # 若非空
```

## 6. 類型分類模組 `estimator_king/sync/typing.py`（新增）

兩層分類，受控詞彙 + 自動擴充。

### 6.1 受控詞彙表

放 `stores_config.yaml`（§9）：`item_types: [...]`（如 タペストリー／アクリルスタンド／アクリルキーホルダー／缶バッジ／ぬいぐるみ／キーホルダー／ネックレス／ポーチ／ボイス／Tシャツ／タオル …）與 `item_types_version: int`。

### 6.2 分類 API（module 函式，編排兩層 + 快取）

分類編排為 `estimator_king/sync/typing.py` 的 **module 函式**（非 `TypingProvider` 方法）；`TypingProvider`（§10.1）只是 LLM 包裝。兩個對外函式：

```python
def classify_item(text, *, item_types, item_types_version, typing_provider, repository) -> str:
    """索引端：必回傳單一類型字串（恰好命中 1 個→直接用；多重命中或零命中→第二層 LLM 擇一，
    無適合者為 'その他'；永不 None）。"""

def classify_query(text, *, item_types, item_types_version, typing_provider, repository=None) -> list[str]:
    """查詢端：回傳 0..N 個類型；第一層多重命中時全數保留供 §9 各查一遍；
    第一層零命中時呼叫第二層得單一類型，包成單元素 list；該類型為 'その他' 時回傳空 list（不做類型過濾，只走純 embedding）。
    repository=None（bot 查詢路徑無 ProductStateRepository）時第二層跳過快取 get/put，直接呼叫 classify_via_llm。"""
```

兩者共用內部分層：

- **第一層（受控詞彙比對，零 LLM）**：對 `text`（item 端＝品項名＋product 標題；query 端＝查詢行）做受控詞彙 `item_types` 比對（詞彙詞為子字串），得命中集合：
  - **恰好命中 1 個**（兩端皆同）→ 直接用，**不**進第二層、**不**寫快取（決定性）。
  - `classify_query` **多重命中**：保留**全部**命中（供 §9 各查一遍），**不**走 LLM。
  - `classify_item` **多重命中**：需單一主類型——**進第二層由 LLM 判斷**（最長匹配在同時命中兩類型詞時會選錯，如「ぬいぐるみポーチ」「タオル&キーホルダー」；交給 LLM 從命中者擇一更準）。
- **第二層（小模型 fallback）**：觸發條件——`classify_item` 的**零命中或多重命中**、`classify_query` 的**零命中**。流程：若 `repository` 非 None 先查快取（§6.3，key 含 `item_types_version`），命中即用；未命中（或 `repository=None`）則呼叫 `typing_provider.classify_via_llm(text, item_types)`（§10.1，傳完整詞彙、LLM 擇一），輸出以 `item_types` 後驗證，不在表內者一律歸 `その他`，`repository` 非 None 時寫入快取。（`classify_item` 索引端一律有 `repository` → 走 LLM 也會被快取，吸收非決定性、不影響 content_hash gating；`classify_query` bot 端為 None → 不快取。`classify_via_llm` 簽名不變，多重命中與零命中共用同一呼叫。）
- **None 協調**：`classify_item` 最終一律有值（`その他` 為下限），與 §4.2「`item_type` 為單一純量字串」一致；`classify_query` 以**空 list** 表示「不過濾」，無 `None`。

### 6.3 類型快取表 `item_type_cache`（新增於 `estimator_king/database/schema.sql`）

```sql
CREATE TABLE IF NOT EXISTS item_type_cache (
    text_hash          TEXT PRIMARY KEY,   -- sha256(正規化文字 + ':' + item_types_version)
    item_type          TEXT NOT NULL,
    item_types_version INTEGER NOT NULL,
    created_at         TEXT NOT NULL
);
```

- key 含 `item_types_version`：詞彙表改版（bump version）→ 舊 key 自然失效、重新分類。
- 命中快取即視為決定性結果，不再呼叫小模型 → 穩態下 LLM 呼叫量極少。
- `ProductStateRepository` 新增方法：`get_cached_type(text_hash) -> str | None`、`put_cached_type(text_hash, item_type, version)`。

### 6.4 自動擴充

`ProductStateRepository` 新增 `list_other_typed(limit) -> list[str]`：列出快取中歸為 `その他` 的 distinct 文字樣本，供人工檢視後把新類型補進 `item_types`（並 bump `item_types_version`）。本規格只提供查詢能力，補詞由人工執行。

### 6.5 typing system prompt（依 GPT-5.4 prompt guidance 改寫）

第二層分類呼叫的 system prompt 採 GPT-5.4 骨架，但**刻意精簡**（單一分類任務、低延遲、便宜模型）。受控詞彙表由 `classify_via_llm(text, item_types)`（§10.1）以 `", ".join(item_types)` 代入 `{item_types}` 佔位。輸出用 json_object（`{"item_type": "..."}`）+ §6.2 後驗證。骨架（最終以英文撰寫）：

```text
Role: You classify one Japanese merchandise item into exactly one category.

# Goal
Pick the single best category for the given item text.

<constraints>
- Choose EXACTLY ONE value from this allowed list: {item_types}.
- If none clearly fits, output "その他". Never invent a category outside the list.
- Decide from the item name/description tokens; ignore talent names and event titles.
</constraints>

# Output
Return JSON only: {"item_type": "<one allowed value or その他>"}. No prose.
```

- reasoning effort 建議 `none`／`low`（純分類、延遲敏感）；verbosity 最小（僅 JSON）。
- 此 prompt 僅用於**第二層 fallback**；第一層受控詞彙最長匹配（§6.2）零 LLM、不經此 prompt。

## 7. 與 `content_hash` 脫鉤與重嵌 gating

### 7.1 `content_hash` 不變

維持只基於來源快照（[crawler/snapshot.py](../../../estimator_king/crawler/snapshot.py)），**不含**類型標籤，保持決定性。

### 7.2 product 級 gating key 擴充

`sync_products` 仍以 product 為處理單位判斷是否需重建其 items：

- 既有判斷：`state.content_hash == content_hash and state.last_indexed_at is not None`（[sync/engine.py](../../../estimator_king/sync/engine.py)）。
- **擴充**：加入 `state.normalizer_version == NORMALIZER_VERSION and state.item_types_version == 當前 item_types_version`。任一不符 → 重建該 product 全部 items。
- `ProductState` 與 `products` 資料表新增欄位 `item_types_version INTEGER`，連動修改點（缺一即型別不一致）見 §11：
  - `ProductState` dataclass 新增 `item_types_version: int | None = None`（[repository.py](../../../estimator_king/database/repository.py)）。
  - `_row_to_state` 讀取該欄（舊列為 `NULL` → `None`，視為版本不符）。
  - `upsert` 的 INSERT 欄位列、`VALUES` 佔位、`ON CONFLICT DO UPDATE` 子句新增該欄。
  - `sync_products` 寫入 `ProductState` 時帶入當前 `item_types_version`。

### 7.3 per-item gating

product 需重建時，逐 item 計算 `item_hash = sha256(embedding 文字 + str(price_jpy) + item_type)`：

- 取得該 product 現存的 item 向量（§8.1 `get_by_product`），比對其 metadata 的 `item_hash`：相同則略過該 item 的 upsert（避免無謂重嵌）。
- 計算「應存在的 item ID 集合」與「現存 ID 集合」的差集，以既有 `VectorStore.delete(ids)`（[store.py](../../../estimator_king/vectorstore/store.py)，已存在）**刪除過時 item 向量**（variant 消失／合併關係改變時）。

### 7.4 呼叫鏈與簽名變更（接線）

新依賴（talents／item_types／item_types_version／typing provider／type 快取）需從持有 `config` 的 `run_crawl_cycle` 逐層下傳，與既有 `embedder`／`vector_store` 同模式：

- **`TypingProvider` 建構位置**：由 `runtime.build_providers`（§10.3）在建 `embedder`／`vector_store` 時一併建立（crawl 與 serve 皆需），放入 `Providers` 容器；`run_crawl_cycle` 與 `build_bot` 透過參數取得。type 快取讀寫走 `ProductStateRepository.get_cached_type/put_cached_type`（§6.3）——`run_crawl_cycle` 已持有 `repo`（[cycle.py:35](../../../estimator_king/crawler/cycle.py)）。
- **`run_crawl_cycle`**（[cycle.py:24](../../../estimator_king/crawler/cycle.py)）：新增參數 `typing_provider: TypingProvider`；`talents`／`item_types`／`item_types_version` 直接取自其已持有的 `config`。
- **`async_process_queue`**（[async_pipeline.py:33](../../../estimator_king/crawler/async_pipeline.py)）：新增 keyword 參數 `typing_provider`、`talents: frozenset[str]`、`item_types: list[str]`、`item_types_version: int` 轉傳。
- **`sync_products`**（[engine.py:77](../../../estimator_king/sync/engine.py)）新簽名：

  ```python
  def sync_products(
      items, store_id, repository, embedder, vector_store,
      *, typing_provider, talents: frozenset[str],
      item_types: list[str], item_types_version: int,
  ) -> SyncResult: ...
  ```

  內部：對每個 product 走 §7.2 gating；需重建時 `decompose_items(snapshot, talents=talents)` → 逐 item 呼叫 module 函式 `typing.classify_item(text, item_types=item_types, item_types_version=item_types_version, typing_provider=typing_provider, repository=repository)`（§6.2）→ `_format_item_document` → §7.3 的 per-item upsert/刪除。
- `sync_products` 的 `_Embedder`／`_VectorStore` Protocol 旁新增 `_TypingProvider` Protocol（`classify_via_llm(text: str, item_types: list[str]) -> str`，對應 §10.1），維持既有 duck-typing 測試慣例。
- **serve 路徑同樣經此鏈**：`CrawlScheduler`（[scheduler.py:20](../../../estimator_king/crawler/scheduler.py)）是 `run_crawl_cycle` 的**第二個呼叫者**（scheduler.py:34）。其 `__init__` 須新增 `typing_provider` 並於 scheduler.py:34 轉傳給 `run_crawl_cycle`；`serve`（[runtime.py:121](../../../estimator_king/runtime.py)）建構 `CrawlScheduler(...)` 時傳入 `providers.typing`。

## 8. VectorStore 擴充 `estimator_king/vectorstore/store.py`

### 8.1 新增方法

```python
def get_by_product(self, store_id: str, product_id: str) -> list[QueryHit]:
    """以 collection.get(where={"$and":[{store_id},{product_id}]}) 列出某 product 的所有 item 向量（含 id 與 metadata），供 sync 比對/刪除過時項。"""
```

- 實測 chromadb 1.5.9：`collection.get(where={...})` 可用、`where` 純量等值與 `$in`／`$and` 可用。
- **刪除沿用既有 API**：§7.3 的過時向量刪除直接用既有 `VectorStore.delete(ids)`（[store.py](../../../estimator_king/vectorstore/store.py)），本節不新增刪除方法。

### 8.2 `query` 既有 `where` 參數沿用

§9 的 `min_type_hits` 移除後，查詢端不再需要門檻參數；`query(embedding, n_results, where)` 介面不變。

## 9. Estimator 查詢端 `estimator_king/bot/estimator.py`

逐行（per-line）檢索；`CHUNK_SIZE` 僅為 chat 呼叫批次，維持不變。

**`_Hit` Protocol 擴充**：step 2 去重需 `hit.id`、step 3 需 `hit.distance`，但現有 `_Hit` Protocol（[estimator.py:32](../../../estimator_king/bot/estimator.py)）只宣告 `metadata`。須擴充為下列（對齊 `QueryHit`，[store.py:12](../../../estimator_king/vectorstore/store.py)），否則 `h.id`／`h.distance` 觸發 `reportAttributeAccessIssue`、未過 §15 prod 0-error 型別閘：

```python
class _Hit(Protocol):
    id: str
    metadata: dict[str, Any]
    distance: float
```

### 9.1 每行流程（取代 `_estimate_chunk` 內的單一 query）

1. `types = typing.classify_query(line, item_types=self._item_types, item_types_version=self._item_types_version, typing_provider=self._typing_provider, repository=None)`（0..N 個類型；bot 端無 repository → 第二層不快取）。`Estimator` 於建構時收下 `typing_provider`、`item_types`、`item_types_version`、`recency_weight`（§10.2），分別存為 `self._typing_provider`／`self._item_types`／`self._item_types_version`／`self._recency_weight`。
2. 檢索並合併（去重 by 向量 ID，保留最小 distance 者）：
   - 對 `types` 中每個類型各做一次 `vector_store.query(emb, n_results=estimator_top_k, where={"item_type": T})`；
   - 另做一次 `vector_store.query(emb, n_results=estimator_top_k)`（純 embedding，不過濾）；
   - 若 `types` 為空 → 只做純 embedding 查詢。
   - 候選池大小 ≤ (N+1)×`estimator_top_k`，於 step 3 rerank 後截為前 `estimator_top_k` 筆送入脈絡。
3. **recency rerank（輕微偏好）**：對合併後候選依下式排序，取前 `estimator_top_k` 筆：

   ```text
   score            = cosine_similarity + λ * recency_norm
   cosine_similarity = 1 - distance
   recency_norm     = (pub - min_pub) / (max_pub - min_pub)   # 以「本次候選集合」的 published_at 為基準
   ```

   - `min_pub`／`max_pub` 取自**本次候選集合中 `published_at > 0`** 的項（排除缺失值，非全 collection）。
   - 缺失（`published_at == 0`）的候選其 `recency_norm` 固定為 `0`（無 recency 加成），且不參與 `min_pub`／`max_pub` 計算——避免缺失值（1970）把分母撐大、稀釋真實日期的 recency 訊號。
   - 當 `published_at > 0` 的候選 < 2 個（無法形成區間）或 `max_pub == min_pub` 時，全體 `recency_norm = 0`（避免除零，退化為純相似度排序）。
   - `λ = self._recency_weight`（建構時由 `config.estimator_recency_weight` 注入，§10.2；預設小值，僅在相似度接近時影響排序）。
4. 組脈絡行（讀新 metadata 鍵，**不再讀**舊 `title`）：`- {item_name} | {item_type} | ¥{price_jpy} | {YYYY-MM} | {store_id}`；其中 `YYYY-MM` 由 `published_at` 換算（`0` → 顯示 `?`）。若 `detail_snippet` 非空，再接一行縮排的規格片段（截斷至約 120 全形字）。讓 chat model 能依規格（size／材質）對齊比價，而非只靠品項名。

### 9.2 size 註記

查詢行的原始文字（含使用者可能附註的 size，如「ぬいぐるみ L」）同時用於 embedding 與送進 chat 脈絡。size／材質對齊由 chat model 依參考行的 `item_name` **與 `detail_snippet`**（§5.4 擷取的規格）判斷，**無需額外解析**。

### 9.3 estimate system prompt（依 GPT-5.4 prompt guidance 改寫）

改寫 [SYSTEM_PROMPT](../../../estimator_king/bot/estimator.py)，採 GPT-5.4 建議骨架（Role → Goal → Success criteria → Constraints → Output → Stop rules），規則塊用 XML 標籤，僅對真正不變量用 must/never，描述「目的地」而非逐步流程。輸出仍由既有 `EstimateBatch` structured output（[llm/chat.py](../../../estimator_king/llm/chat.py)）約束，prompt 負責欄位語義與取材紀律。骨架（最終以英文撰寫，內容固定如下語義）：

```text
Role: You are the Estimator King, a price estimator for Japanese hololive/vspo
merchandise. You price one item per input line using only the provided references.

# Goal
For each product line, output a JPY price estimate grounded in the reference items.

# Success criteria
- One estimate per input line, in the same order; none skipped.
- suggested_price and price_range are integer JPY justified by the references.
- confidence reflects match quality (see constraints).

<constraints>
- Ground every estimate ONLY in the provided reference context; never invent
  prices or products not present in it.
- Prefer references of the SAME item_type as the queried line; use cross-type
  references only as weak signal.
- When references of comparable type span different dates, weight more RECENT
  prices higher (merchandise prices drift upward over time).
- Match size/material using each reference's item_name and detail line when present.
- Prices are integer JPY. Include up to 3 reference_products actually drawn from
  the context.
</constraints>

# Output
Return an estimate object per line (product_name, suggested_price_jpy,
price_range_jpy, confidence ∈ {high, medium, low}, rationale, reference_products).
confidence: high = direct/near-exact same-type match; medium = same-type but
size/variant differs; low = only cross-type or weak matches.

<stop_rules>
- If no strong match exists, still return an estimate with confidence "low" and a
  rationale stating the limitation — do NOT fabricate a closer match.
</stop_rules>
```

- 若使用 GPT-5.x chat 模型，reasoning effort 建議 `low`～`medium`（估價需少量推理，非高度 agentic）；verbosity 偏精簡（rationale 一兩句）。此為旋鈕建議，非硬性。

### 9.4 回應可靠性（estimate → format → Discord）

現行 `/estimate` 回應路徑（[estimator.py](../../../estimator_king/bot/estimator.py) → [commands.py::format_estimates](../../../estimator_king/bot/commands.py) → `followup.send`）的傳輸/長度層大致可靠（`defer(thinking=True)` 處理 3 秒 ACK、每 embed 各自 send 避開「單訊息 10 embeds」限制、2000 字切頁保守於 4096 上限），但有三個須修正的缺陷：

#### 9.4.1 結果對帳（最關鍵）

LLM 僅被「請求」依序回傳每行，無任何保證。新增**對帳**，使 `format_estimates` 永遠收到與輸入**等量、同序**的估價：

- 在 `Estimator.estimate_products`（[estimator.py:51](../../../estimator_king/bot/estimator.py)）收集完 `all_estimates` 後，對齊回 `product_names`：
  - 以 `_normalize_text`（§4.2 同函式）正規化後的 `product_name` 為 key，建 `估價 by name` 映射（重複則保留第一個）。
  - 逐一走 `product_names`：命中映射 → 用該估價；未命中 → 插入**佔位估價**（`product_name=該行原文`、`suggested_price_jpy=0`、`price_range_jpy=PriceRange(min=0, max=0)`（pydantic 不接受 positional，須具名）、`confidence="low"`、`rationale="No estimate returned for this item."`、`reference_products=[]`）。
  - 未對應到任何輸入行的多餘估價 → 丟棄並 `logger.warning`（記錄數量）。
  - 回傳長度 == `len(product_names)`、順序同輸入。
- 此對帳純後處理（不改 `EstimateBatch`／`ProductEstimate` schema，[chat.py](../../../estimator_king/llm/chat.py) 不動），對 OpenAI structured 與 ollama json_object 兩路皆適用。

#### 9.4.2 頁碼分母 bug

[commands.py:97](../../../estimator_king/bot/commands.py#L97) 把總頁數寫死成 `1 if … else 2`：≥3 頁時非末頁分母固定為 2（應為實際總頁數），例如 3 頁會顯示「page 1/2」「page 2/2」「page 3/3」（分母不一致且錯誤）。**修正**：先把所有頁內容組成 `list[str]`（沿用 2000 字切頁邏輯），再以 `total = len(pages)` 統一產生標題 `f"Price Estimates (page {i}/{total})"`。

#### 9.4.3 `rstrip` 誤用

[commands.py:98](../../../estimator_king/bot/commands.py#L98)、[110](../../../estimator_king/bot/commands.py#L110) 的 `current_content.rstrip("\n---\n\n")` 把參數當**字元集合**，會剝掉結尾所有 `\n`／`-`（rationale 結尾若有破折號會被誤刪）。**修正**：改用 `str.removesuffix("\n\n---\n\n")` 移除已知分隔後綴（product_block 結尾固定為 `"\n\n---\n\n"`，[commands.py:83](../../../estimator_king/bot/commands.py#L83)），或在組頁時不對最後一塊附加分隔。

## 10. Provider 設定擴充

### 10.1 typing 小模型（`estimator_king/llm/config.py`）

`ProviderConfig` 新增：

```python
    # Typing (item-type classification; small/cheap model)
    typing_model: str = "gpt-4o-mini"
    typing_base_url: str | None = None   # cascade: typing → chat → openai
    typing_api_key: str = ""             # cascade: typing → chat → openai
```

- env：`TYPING_MODEL`、`TYPING_BASE_URL`、`TYPING_API_KEY` 於 `load_config`（[config_schema.py:181](../../../estimator_king/config_schema.py)）以 `os.getenv` 讀入 `AppConfig.typing_*`；cascade（未設定回落 chat，再回落 openai）在 `build_provider_config` 組 `ProviderConfig` 時套用（見 §10.2）。
- 新增 `TypingProvider`（`estimator_king/llm/typing_provider.py`）：建構子 `TypingProvider(config: ProviderConfig)` **只存 `config`，不立即建 OpenAI client**。OpenAI client 於首次 `classify_via_llm` 呼叫時 **lazy 建構**（以 `typing_api_key`/`typing_base_url`/`typing_model`，建後快取於實例）。
  - **為何 lazy（阻斷級理由）**：`build_providers` 對 crawl 與 serve 皆無條件建 `TypingProvider`（§10.3）。crawl 路徑常只設 `EMBEDDING_API_KEY`（local-embed split／ollama），此時 `typing_api_key` cascade 後為 `""`，**eager** `OpenAI(api_key="")` 會 raise `OpenAIError` 使 `crawl` exit 1——這正是姊妹設計 `decouple-bot-crawl` 已以 `with_chat=False` 堵掉的回歸（[runtime.py](../../../estimator_king/runtime.py) 對 `ChatProvider` 同理）。lazy 建構讓 crawl 穩態（第一層受控詞彙命中即零 LLM）永不觸發 client 建構。
  - 唯一方法 `classify_via_llm(text: str, item_types: list[str]) -> str`：lazy 取得 client，以 §6.5 prompt（`{item_types}` 代入）呼叫 `typing_model`，json_object 模式取 `item_type` 字串回傳（後驗證在 §6.2 module 函式做，與 [llm/chat.py](../../../estimator_king/llm/chat.py) 的 fallback 模式一致，相容 ollama）。
  - `TypingProvider` **不**持有 `item_types`／不碰快取——兩層編排與快取由 §6.2 module 函式負責。

- `build_provider_config`（[config_schema.py:146](../../../estimator_king/config_schema.py)）新增傳入：`typing_model=self.typing_model`、`typing_base_url=self.typing_base_url or self.chat_base_url or self.openai_base_url`、`typing_api_key=self.typing_api_key or self.chat_api_key or self.openai_api_key or ""`。

### 10.2 新增 structural 設定（YAML，非 env）

以下為 `stores_config.yaml` 新增的**結構性**設定（與 §10.1 的 env credential 不同路徑），需在 `AppConfig` 新增欄位並於 `load_config` 解析：

```yaml
item_types: [タペストリー, アクリルスタンド, 缶バッジ, ぬいぐるみ, キーホルダー, ネックレス, ポーチ, ボイス, Tシャツ, タオル]
item_types_version: 1
talents: [博衣こより, 白銀ノエル, 尾丸ポルカ]      # §12，mining + 人工審核產出
estimator:
  top_k: 10
  recency_weight: 0.05
```

`AppConfig`（[config_schema.py:90](../../../estimator_king/config_schema.py)）新增欄位（型別 + 預設）：

```python
    item_types: List[str] = field(default_factory=list)
    item_types_version: int = 0
    talents: frozenset[str] = field(default_factory=frozenset)
    estimator_top_k: int = 10
    estimator_recency_weight: float = 0.05
```

`load_config`（[config_schema.py:181](../../../estimator_king/config_schema.py)）新增解析（structural，從 `yaml_data` 讀，非 env）：

```python
    est = yaml_data.get("estimator", {}) or {}
    # 傳入 AppConfig(...)：
    item_types=list(yaml_data.get("item_types", []) or []),
    item_types_version=int(yaml_data.get("item_types_version", 0) or 0),
    talents=frozenset(yaml_data.get("talents", []) or []),
    estimator_top_k=int(est.get("top_k", 10)),
    estimator_recency_weight=float(est.get("recency_weight", 0.05)),
```

- typing env（`TYPING_MODEL`／`TYPING_BASE_URL`／`TYPING_API_KEY`）於 `load_config` 以 `os.getenv` 讀入 `AppConfig.typing_*` 欄位（同 §10.1，AppConfig 亦新增對應 `typing_model: str = "gpt-4o-mini"`、`typing_base_url: str | None = None`、`typing_api_key: str | None = None`）。
- 注入：`Estimator.__init__`（[estimator.py:44](../../../estimator_king/bot/estimator.py)，已有 `top_k=10`）新增 `typing_provider`、`item_types: list[str]`、`item_types_version: int`、`recency_weight: float = 0.05`（查詢端 §9.1 step 1 `classify_query` 需要前三者；bot 端 `repository=None` 不需 repo）。唯一實例化點在 **`build_bot`（[bot/runner.py:45](../../../estimator_king/bot/runner.py)）**（非 `runtime`），該處持有 `config` 與 `providers.typing`：改為 `Estimator(embedder, chat, vector_store, typing_provider, item_types=config.item_types, item_types_version=config.item_types_version, top_k=config.estimator_top_k, recency_weight=config.estimator_recency_weight)`。`decompose_items`／索引端 typing 的 `talents`／`item_types`／`item_types_version` 走 §7.4 的 crawl 呼叫鏈。`build_bot` 需新增 `typing_provider` 參數，由 `serve` 傳入 `providers.typing`（§10.3）。
- `AppConfig.validate` 為結構驗證；`item_types`／`talents` 允許為空（空 `talents` → 不去重；空 `item_types` → 第一層永不命中、全走第二層或 `その他`），不在 `validate` 強制。

### 10.3 `build_providers`／`Providers` 擴充（[runtime.py](../../../estimator_king/runtime.py)）

- `Providers` 容器新增 `typing: TypingProvider`（crawl 與 serve 皆需）。
- `build_providers(config, *, with_chat=...)` 在建 `embedder`／`vector_store` 後，以 `config.build_provider_config()` 產出的 `ProviderConfig`（含 §10.1 typing 欄位）**無條件**建構 `TypingProvider` 放入容器。無條件建構安全，因 `TypingProvider.__init__` 不建 client（lazy，§10.1）——即使 crawl 路徑 `typing_api_key=""` 也不會在此 raise。
- CLI `crawl` 路徑：`__main__` 的 crawl 指令傳 `providers.typing` 給 `run_crawl_cycle`（§7.4）。
- serve 路徑有**兩條**接線（[runtime.py:114](../../../estimator_king/runtime.py) `serve`）：
  - 查詢端（index 已建好的向量）：`build_bot` 接 `providers.typing` → 注入 `Estimator`（§10.2）。
  - 索引端（程序內排程爬取）：`CrawlScheduler(config, db_path, embedder, vector_store, typing_provider=providers.typing)`（§7.4），由 scheduler 轉傳 `run_crawl_cycle`。兩端共用同一 `providers.typing` 實例。

## 11. 遷移

- **向量重建**：向量 ID 規則與 embedding 文字皆改變 → 需 `rm -rf chroma/` 後 `crawl --force-refetch`。更新 [CLAUDE.md](../../../CLAUDE.md) Gotchas 與 [docs/local-runbook.md](../../../docs/local-runbook.md)／[docs/ops-runbook.md](../../../docs/ops-runbook.md)。
- **SQLite**（現況：[repository.py::_ensure_schema](../../../estimator_king/database/repository.py) 只 `executescript(schema.sql)`，schema 全為 `CREATE TABLE IF NOT EXISTS`，**對既有表不會加欄**；[schema.sql](../../../estimator_king/database/schema.sql) 標明 greenfield/no-migrations）：
  - `schema.sql` 的 `products` CREATE 新增 `item_types_version INTEGER`（供全新資料庫）；另新增 `item_type_cache` 表（§6.3，`CREATE TABLE IF NOT EXISTS`）。
  - **既有資料庫加欄遷移**：在 `_ensure_schema` 內 `executescript` 之後，加入冪等 ALTER：以 `PRAGMA table_info(products)` 取現有欄名，若不含 `item_types_version` 則執行 `ALTER TABLE products ADD COLUMN item_types_version INTEGER`。舊列該欄為 `NULL` → `_row_to_state` 讀為 `None` → §7.2 視為版本不符 → 下次 crawl 自然重建。
  - 連動修改（§7.2 已列）：`ProductState` 欄位、`_row_to_state`、`upsert` 的 INSERT／VALUES／ON CONFLICT 子句、`sync_products` 寫入值，全部同步加入 `item_types_version`。
- **talent 字典 seed**：實作期執行一次性 mining 腳本 `scripts/mine_talents.py`（同價組內單一差異 token、頻次門檻、濾除含 `ver.`／`限定`／純數字者），產出初版 `talents` 清單供人工審核後寫入 `stores_config.yaml`（`talents: [...]`）。

## 12. talent 字典來源與設定

- 來源：自動挖掘 + 人工審核種子，之後可手動擴充（同「受控詞彙 + 自動擴充」哲學）。
- 設定：`stores_config.yaml` 新增 `talents: [博衣こより, 白銀ノエル, ...]`，由 `config_schema` 讀為 `frozenset[str]` 注入 `decompose_items`。

## 13. 錯誤處理

- **typing 小模型失敗**：第二層的 `classify_via_llm`（含 lazy client 建構，§10.1）任何例外（含空 key 的 `OpenAIError`、網路、解析）由 §6.2 module 函式 catch，記錄並回傳 `その他`（不阻斷索引／查詢）。索引端沿用 [sync/engine.py](../../../estimator_king/sync/engine.py) 既有「embed/vector 失敗 fire-and-forget、不前進 `last_indexed_at`」策略。
- **去重輸入異常**（價格無法解析）：該 variant 比照 ¥0 規則略過。
- **`published_at` 缺失**：metadata 記 `0`；recency rerank 時不參與 `min_pub`／`max_pub`、`recency_norm` 固定為 `0`（無加成），規則見 §9.1 step 3。
- **查詢端類型過濾後零命中**：純 embedding 查詢必定執行，保證脈絡不空。

## 14. 測試（pytest，沿用 fakes 慣例）

- `tests/test_items.py`：
  - 四種情境分類正確：單品／同種多 talent 變體（合併，命名用 product 標題）／系列無 SET（各自獨立）／混合多品項（各自獨立）。
  - talent-gated 去重：Blue Journey ×N 合併；themed series（同主題不同品項）**不**合併（反例）；同價但不同品項不合併。
  - SET 與 ¥0 排除。
  - 命名規則三分支：whole-product 合併→product 標題；短選項值（`< 4` 或 size pattern，如「黒 M」「Lサイズ」）→併入 product 標題；混合品項殘餘→原樣（驗證活動/生日商品的長品項名如「イオフィカラー ショルダーバッグ」**不**被誤併入活動名）。
  - `detail_snippet` 擷取（substring/core 為主）：section 條列含品項名核心時取對應規格行（含單 token 日文名以子字串命中，如「Eternity アクリルジオラマスタンド」→ 其 サイズ/素材 行）；無對應行（語音/數位、或非條列）時為空字串（不退回整段 description）。
- `tests/test_typing.py`：第一層恰好命中 1 個→直接用、不呼叫 LLM；`classify_item` **多重命中→第二層 LLM 擇一**、**零命中→第二層→`その他`**（永不 None，fake provider 計數驗證有呼叫）；`classify_query` 多重命中回多元素、不呼叫 LLM、`その他`→空 list；第二層後驗證歸 `その他`；快取命中不呼叫 `classify_via_llm`（含 `item_types_version` 失效）。
- `tests/test_engine_items.py`：逐 item upsert；過時 item 向量刪除；`item_hash` 相同略過重嵌；gating key（含 `item_types_version`）。
- `tests/test_estimator.py`：`Estimator` 以注入的 `item_types`/`item_types_version`/fake `typing_provider` 及 `repository=None` 呼叫 `classify_query`（驗證關鍵字參數）；逐行多類型各查 + 純查合併去重（同 ID 保留最小 distance）；零類型只純查；recency rerank 排序（fake hits 帶 `published_at`）；recency 邊界：候選含 `published_at==0` 時該項 `recency_norm=0` 且不參與 min/max、`>0` 候選 < 2 時退化純相似度；脈絡行格式（含 `0` → `?` 日期）；**對帳（§9.4.1）**：LLM 回傳數量不足／重排／多餘時，`estimate_products` 回傳長度 == 輸入、同序，缺項為佔位估價（confidence `low`），多餘估價被丟棄並記 warning（fake chat 刻意錯位）。
- `tests/test_config_schema.py`：`load_config` 解析 `item_types`／`item_types_version`／`talents`／`estimator.top_k`／`estimator.recency_weight`（含預設值回落）；typing env cascade 經 `build_provider_config`。
- `tests/test_runtime.py`／`tests/test_scheduler.py`：`build_providers` 產出含 `typing` 的 `Providers`；空 `typing_api_key`（僅設 `EMBEDDING_API_KEY`）時 `build_providers` 不 raise（lazy client，§10.1）、crawl 第一層命中路徑不建 client；`CrawlScheduler.__init__` 接 `typing_provider` 並於 `run_once` 轉傳給 `run_crawl_cycle`（serve 索引端接線；fake provider 驗證傳遞）。
- `tests/test_shopify.py`（或既有）：`published_at` 解析（ISO8601 → epoch、缺失回落 `created_at`、再缺 `0`）；`ProductSnapshotWithHash` 帶 `published_at` 仍可 import/建構（驗證 dataclass 排序修正）。
- `tests/test_commands.py`（§9.4.2／9.4.3）：≥3 頁時頁碼分母正確（`page i/total`）；`removesuffix` 不誤刪 rationale 結尾破折號（含 rationale 以 `-` 結尾的案例）。
- 驗證工具鏈（[CLAUDE.md](../../../CLAUDE.md)）：`.venv/bin/basedpyright estimator_king`（prod 0 error）、`uvx ruff check`、相關 `pytest -o addopts=""`。

## 15. 驗收標準

1. `RIONA ON THE ステージタペストリー`：參考以同類型 `タペストリー` 品項為主，估價落在同類型合理區間。
2. `リオナとおそろいネックレス`：類型對齊到 `ネックレス`（若目錄存在同類型品項則命中；否則 fallback 純 embedding，不捲空）。
3. `くしゃみ連発ぬいキーホルダー`：類型對齊到 `キーホルダー`／`ぬいぐるみ` 相關品項。
4. 混合品項 product 的每個 item 帶**自身價格**（不再是 product min）。
5. Blue Journey 類同種多 talent 商品合併為單筆，themed series 不被誤併。
6. `/estimate` 回應：輸入 N 行 → 回應恰含 N 筆、同序；LLM 漏項時對應行顯示佔位（confidence `low`）而非靜默缺漏；多頁時頁碼 `i/total` 正確。
7. 驗證工具鏈全綠。
