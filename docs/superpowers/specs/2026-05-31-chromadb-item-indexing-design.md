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
    item_name: str               # 此品項顯示名（見 §5 命名規則）
    price_jpy: int               # 此品項價格（整數 JPY）
    source_variant_ids: tuple[int, ...]   # 合併後對應的原始 variant id（≥1）
    talents: tuple[str, ...]     # 合併時收集到的 talent（可空）
    detail_snippet: str          # 最佳努力擷取的段落片段（可空，見 §5.3）
```

### 4.2 每 item 向量

- **向量 ID**：`f"{store_id}:{product_id}:{item_slug}"`。`item_slug` = 對 `item_name` 正規化（§5.4 的 `_normalize_text` 同規則）後取 SHA-256 前 16 hex，確保同一 product 內穩定且不撞號。
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

> ChromaDB 1.5.9 實測：metadata 可存 list 但 `where` 無法對 list 成員過濾（`where={"types":"x"}` 對 list 欄位回傳空）；而查詢需 `where={item_type:T}`，故 `item_type` 必須是**單一純量**。複合品項（如「ぬいキーホルダー」）的 recall 由 embedding 文字含全名 + 純 embedding 補查（§8）保住。

## 5. 品項拆解模組 `estimator_king/sync/items.py`（新增）

`decompose_items(snapshot: ProductSnapshot, *, talents: frozenset[str]) -> list[ProductItem]`

### 5.1 流程

1. **排除 SET**：variant 標題的 option 前綴（`split(" / ", 1)` 的左段）以「セット」開頭者，整筆丟棄不索引。
2. **排除 ¥0**：`price` 解析為 0 的 variant 丟棄（贈品／特典，非可比價）。
3. **talent-gated 去重**：見 §5.2。
4. **命名**：見 §5.3。

### 5.2 去重規則（talent-gated，取代純結構模板偵測）

> 純結構（LCP/LCS）模板偵測經實證**不可靠**：themed series（如「Start your Journey ポーチ／りんご型プレート」「活動三周年 Tシャツ／トートバッグ」）共享長主題前後綴，結構上與 talent 列舉無法區分，會誤併不同品項。唯一可靠訊號是「變動 token 是否為 talent」，故需 talent 字典。

去重演算法（剝除 option 前綴後對殘餘標題操作）：

1. 依 `price_jpy` 分組。
2. 對每個 size ≥ 2 的同價組：把各殘餘標題以空白切 token；
   - 若任兩筆之間僅差**恰好一個 token**，且該差異 token ∈ `talents`，視為同品項的 talent 變體；
   - 對整組做傳遞閉包（union-find）：若全組兩兩皆滿足上述條件（殘餘標題移除其 talent token 後完全相同），整組合併為**一筆 item**。
3. 合併的 item：`source_variant_ids` 收集全組、`talents` 收集差異 token、`price_jpy` 為該組價格。
4. 未滿足合併條件者，每個 variant 各自成一筆 item。

不同 size 因價格不同會落在不同價組 → 不會被合併 → 各自成獨立 item（解決 §2 size 議題）。

### 5.3 命名規則（`item_name`）

- **整個 product 合併成單一 item 時**（去重後只剩 1 筆，且來源 ≥2 個 talent 變體）：`item_name = product_title`（product 標題即品項名，例：Blue Journey ×23 → 「3Dアクリルスタンド Blue Journey衣装ver.」）。
- **variant 標題僅為選項值時**（剝除前綴後殘餘為純 size／顏色，如「黒　M」；判定：殘餘不含 product 標題的主要 token，或殘餘長度 < 4 全形字）：`item_name = f"{product_title} {殘餘}"`，使 size／顏色成為品項名一部分。
- **其餘**（混合品項、各自獨立）：`item_name = 剝除前綴後的殘餘標題`。

### 5.4 段落片段擷取（`detail_snippet`，最佳努力、決定性、可空）

**動機**：品項名（甚至 variant 標題）不一定載明 size、材質等規格——這些往往**只**存在於 `description`／section content。缺了它，無論 embedding 或 chat model 都只能靠標題「猜」size／材質。因此需從整 product 的段落中**擷取屬於該 item 的規格行**補足，並同時用於 embedding 與查詢端 LLM 脈絡（§9.1）。

`snapshot.html_details`（如「グッズ詳細」「セット詳細」）常以「・<品項名> …」條列各品項規格（含尺寸、材質）。對每個 item：

- 在各段落內容中，以「・」「◇」或換行切段後，尋找與該 item 主要 token 重疊度最高的一段；命中且重疊度達門檻則取該段為 `detail_snippet`，否則為空字串。
- 擷取不到時**直接略過**（`detail_snippet=""`），不退回整段 description（避免把整 product 噪音灌進單一 item）。
- 此片段**不影響價格**（價格一律取自 variant），僅補充規格語義供檢索與估價判斷。

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

### 6.2 分類流程 `classify(text: str) -> str | None`

- **第一層（零 LLM）**：對輸入文字（品項名 +（索引時）product 標題）做受控詞彙**最長匹配**（詞彙詞為子字串，取最長者）。恰好命中一個 → 回傳該類型。
- **第二層（小模型 fallback）**：第一層**零命中或多重命中**時，呼叫 typing 小模型（§10），要求從受控詞彙表選**一個**，或回 `その他`。輸出以受控詞彙做後驗證；不在表內者一律歸 `その他`。
- 結果寫入快取（§6.3）。

`classify_query(line)` 與 `classify_item(item)` 皆呼叫同一 `classify`；query 端可回傳**多個**命中類型（第一層多重命中時全數保留供 §8 各查一遍），item 端取單一主類型（多重命中走第二層收斂為一）。

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

## 7. 與 `content_hash` 脫鉤與重嵌 gating

### 7.1 `content_hash` 不變

維持只基於來源快照（[crawler/snapshot.py](../../../estimator_king/crawler/snapshot.py)），**不含**類型標籤，保持決定性。

### 7.2 product 級 gating key 擴充

`sync_products` 仍以 product 為處理單位判斷是否需重建其 items：

- 既有判斷：`state.content_hash == content_hash and state.last_indexed_at is not None`（[sync/engine.py](../../../estimator_king/sync/engine.py)）。
- **擴充**：加入 `state.normalizer_version == NORMALIZER_VERSION and state.item_types_version == 當前 item_types_version`。任一不符 → 重建該 product 全部 items。
- `ProductState` 與 `products` 資料表新增欄位 `item_types_version INTEGER`（additive；§11 遷移）。

### 7.3 per-item gating

product 需重建時，逐 item 計算 `item_hash = sha256(embedding 文字 + str(price_jpy) + item_type)`：

- 取得該 product 現存的 item 向量（§8.1 `get_by_product`），比對 `item_hash`：相同則略過 upsert（避免無謂重嵌）。
- 計算「應存在的 item ID 集合」與「現存 ID 集合」的差集，**刪除過時 item 向量**（variant 消失／合併關係改變時）。

## 8. VectorStore 擴充 `estimator_king/vectorstore/store.py`

### 8.1 新增方法

```python
def get_by_product(self, store_id: str, product_id: str) -> list[QueryHit]:
    """以 collection.get(where={"$and":[{store_id},{product_id}]}) 列出某 product 的所有 item 向量（含 id 與 metadata），供 sync 比對/刪除過時項。"""
```

- 實測 chromadb 1.5.9：`collection.get(where={...})` 可用、`where` 純量等值與 `$in`／`$and` 可用。

### 8.2 `query` 既有 `where` 參數沿用

§9 的 `min_type_hits` 移除後，查詢端不再需要門檻參數；`query(embedding, n_results, where)` 介面不變。

## 9. Estimator 查詢端 `estimator_king/bot/estimator.py`

逐行（per-line）檢索；`CHUNK_SIZE` 僅為 chat 呼叫批次，維持不變。

### 9.1 每行流程（取代 `_estimate_chunk` 內的單一 query）

1. `types = typing.classify_query(line)`（0..N 個類型）。
2. 檢索並合併（去重 by 向量 ID）：
   - 對 `types` 中每個類型：`vector_store.query(emb, top_k, where={"item_type": T})`；
   - 另做一次 `vector_store.query(emb, top_k)`（純 embedding，不過濾）；
   - 若 `types` 為空 → 只做純 embedding 查詢。
3. **recency rerank（輕微偏好）**：對合併後候選依下式排序後取前 `top_k` 筆：

   ```
   score = cosine_similarity + λ * recency_norm
   cosine_similarity = 1 - distance
   recency_norm ∈ [0,1]，以 published_at 線性映射（最舊→0，最新→1；published_at==0 視為最舊）
   λ = recency_weight（§10，預設小值，僅在相似度接近時影響排序）
   ```

4. 組脈絡行：`- {item_name} | {item_type} | ¥{price_jpy} | {YYYY-MM} | {store_id}`；若 `detail_snippet` 非空，再接一行縮排的規格片段（截斷至約 120 全形字）。讓 chat model 能依規格（size／材質）對齊比價，而非只靠品項名。

### 9.2 size 註記

查詢行的原始文字（含使用者可能附註的 size，如「ぬいぐるみ L」）同時用於 embedding 與送進 chat 脈絡。size／材質對齊由 chat model 依參考行的 `item_name` **與 `detail_snippet`**（§5.4 擷取的規格）判斷，**無需額外解析**。

### 9.3 system prompt 微調

更新 [SYSTEM_PROMPT](../../../estimator_king/bot/estimator.py)：說明參考行格式新增 `item_type` 與日期，並指示「同類型優先、相近日期的價格更具參考性」。

## 10. Provider 設定擴充

### 10.1 typing 小模型（`estimator_king/llm/config.py`）

`ProviderConfig` 新增：

```python
    # Typing (item-type classification; small/cheap model)
    typing_model: str = "gpt-4o-mini"
    typing_base_url: str | None = None   # cascade: typing → chat → openai
    typing_api_key: str = ""             # cascade: typing → chat → openai
```

- env：`TYPING_MODEL`、`TYPING_BASE_URL`、`TYPING_API_KEY`，於 [config_schema.py::build_provider_config](../../../estimator_king/config_schema.py) 讀取並做 cascade（未設定時回落 chat，再回落 openai）。
- 新增 `TypingProvider`（`estimator_king/llm/typing_provider.py`）：以 OpenAI SDK 呼叫 `typing_model`，輸入受控詞彙表與待分類文字，回傳單一類型字串（json_object 模式 + 後驗證，與 [llm/chat.py](../../../estimator_king/llm/chat.py) 的 fallback 模式一致，相容 ollama）。

### 10.2 檢索參數（`stores_config.yaml` 的 `estimator` 區段，新增）

```yaml
estimator:
  top_k: 10              # 既有預設沿用
  recency_weight: 0.05   # λ，輕微偏好
```

由 `config_schema` 讀入並注入 `Estimator`。

## 11. 遷移

- **向量重建**：向量 ID 規則與 embedding 文字皆改變 → 需 `rm -rf chroma/` 後 `crawl --force-refetch`。更新 [CLAUDE.md](../../../CLAUDE.md) Gotchas 與 [docs/local-runbook.md](../../../docs/local-runbook.md)／[docs/ops-runbook.md](../../../docs/ops-runbook.md)。
- **SQLite**：
  - 新增 `item_type_cache` 表（`CREATE TABLE IF NOT EXISTS`，自動建立）。
  - `products` 表新增 `item_types_version INTEGER`：以 `CREATE TABLE IF NOT EXISTS` 既有結構 + 啟動時 `ALTER TABLE ... ADD COLUMN`（若不存在）的 additive 遷移；舊列預設 `NULL`，視為「版本不符」→ 下次 crawl 自然重建。
- **talent 字典 seed**：實作期執行一次性 mining 腳本 `scripts/mine_talents.py`（同價組內單一差異 token、頻次門檻、濾除含 `ver.`／`限定`／純數字者），產出初版 `talents` 清單供人工審核後寫入 `stores_config.yaml`（`talents: [...]`）。

## 12. talent 字典來源與設定

- 來源：自動挖掘 + 人工審核種子，之後可手動擴充（同「受控詞彙 + 自動擴充」哲學）。
- 設定：`stores_config.yaml` 新增 `talents: [博衣こより, 白銀ノエル, ...]`，由 `config_schema` 讀為 `frozenset[str]` 注入 `decompose_items`。

## 13. 錯誤處理

- **typing 小模型失敗**：第二層呼叫例外時，記錄並回傳 `その他`（不阻斷索引／查詢）。索引端沿用 [sync/engine.py](../../../estimator_king/sync/engine.py) 既有「embed/vector 失敗 fire-and-forget、不前進 `last_indexed_at`」策略。
- **去重輸入異常**（價格無法解析）：該 variant 比照 ¥0 規則略過。
- **`published_at` 缺失**：metadata 記 `0`，recency_norm 視為最舊。
- **查詢端類型過濾後零命中**：純 embedding 查詢必定執行，保證脈絡不空。

## 14. 測試（pytest，沿用 fakes 慣例）

- `tests/test_items.py`：
  - 四種情境分類正確：單品／同種多 talent 變體（合併，命名用 product 標題）／系列無 SET（各自獨立）／混合多品項（各自獨立）。
  - talent-gated 去重：Blue Journey ×N 合併；themed series（同主題不同品項）**不**合併（反例）；同價但不同品項不合併。
  - SET 與 ¥0 排除。
  - 命名規則三分支（product 標題／選項值併入／殘餘標題）。
  - `detail_snippet` 擷取：section 含「・<品項名> …」時取對應規格行；無對應行時為空字串（不退回整段 description）。
- `tests/test_typing.py`：第一層最長匹配（唯一命中／多重命中／零命中）；第二層後驗證歸 `その他`；快取命中不呼叫小模型（fake provider 計數）。
- `tests/test_engine_items.py`：逐 item upsert；過時 item 向量刪除；`item_hash` 相同略過重嵌；gating key（含 `item_types_version`）。
- `tests/test_estimator.py`：逐行多類型各查 + 純查合併去重；零類型只純查；recency rerank 排序（fake hits 帶 `published_at`）；脈絡行格式。
- 驗證工具鏈（[CLAUDE.md](../../../CLAUDE.md)）：`.venv/bin/basedpyright estimator_king`（prod 0 error）、`uvx ruff check`、相關 `pytest -o addopts=""`。

## 15. 驗收標準

1. `RIONA ON THE ステージタペストリー`：參考以同類型 `タペストリー` 品項為主，估價落在同類型合理區間。
2. `リオナとおそろいネックレス`：類型對齊到 `ネックレス`（若目錄存在同類型品項則命中；否則 fallback 純 embedding，不捲空）。
3. `くしゃみ連発ぬいキーホルダー`：類型對齊到 `キーホルダー`／`ぬいぐるみ` 相關品項。
4. 混合品項 product 的每個 item 帶**自身價格**（不再是 product min）。
5. Blue Journey 類同種多 talent 商品合併為單筆，themed series 不被誤併。
6. 驗證工具鏈全綠。
