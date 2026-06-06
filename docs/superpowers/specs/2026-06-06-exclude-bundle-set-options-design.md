# 設計規格：排除整套打包選項（bundle set options）

日期：2026-06-06

## 背景與問題

`decompose_items`（[estimator_king/sync/items.py](../../../estimator_king/sync/items.py)）會把一個
Shopify 商品的所有 variants 拆解成可定價的 `ProductItem`。目前唯一的「set 過濾」是
variant title 前綴判斷（`_strip_prefix` 取出的 prefix 以 `セット` 開頭時排除，
items.py 約 line 124）。這只擋得到 variant **option 前綴**為 `セット` 的情況。

但實際資料中存在另一類未被擋掉的項目：商品的某個選項（或整個商品）本身是
「把整組商品打包成一個 SKU」的**整套打包選項**，其 `item_name` 含「セット」字樣，
價格通常是同商品其他單品的部分總和或全部總和。這類項目不是我們要收錄的定價對象
（hololive 商店過去也曾移除過 prefix 為 `セット` 的產品）。

### 實際資料佐證（ChromaDB 既有 16,423 個 item）

- 含「セット」且非「ステッカーセット」的 item 共 633 個，其中 295 個的
  「set 價 / 同商品中位數」ratio ≥ 2（97 個 ≥ 4）。
- 最強訊號：部分套組價格剛好等於同商品所有單品總和（`price / sum(peers) = 1.00`），
  例如 vspo 的 `誕生日記念グッズセット`、`グッズセット`。
- 整套打包家族：`バースデーグッズセット`、`グッズセット`、`N周年(記念)グッズセット`、
  `応援セット`、`誕生日記念グッズセット`、hololive 語音的 `フルセット` 與
  語言別 `日本語/英語/インドネシア語セット`、`アクリルスタンド … stageN セット`。
- 單品型 `セット`（應保留）：`ステッカー(＆缶バッジ)セット`、各種 `ボイスセット`、
  `缶バッジセット`、`クリアファイルセット`、`カトラリーセット`、`カードセット` 等，
  本身是單一可販售商品，價格多低於同商品中位數（ratio 0.1～0.3）。

### 關鍵資料發現（影響判定規則）

純價格比例門檻不可靠：有 37 個確實是套組的 `グッズセット`／`周年記念グッズセット`／
`バースデーグッズセット`，因商品內混入高價服飾（M/L/XL 尺寸），把同商品中位數拉高，
導致 ratio < 2（最低 0.75，甚至低於中位數）。因此若硬性要求 `ratio ≥ 2`，會漏掉
這些真套組（false negative）。反觀「打包詞」對 `グッズセット/フルセット/応援セット/語セット`
是近乎完美的判別器（270 個命中全是真套組，且不誤殺單品型）。

結論：**名稱關鍵字為主要且權威的判別依據；價格只當補抓無關鍵字套組的 tie-breaker。**

## 目標

在 `decompose_items` 拆解流程中，新增一個排除「整套打包選項」的判定，讓這類項目
不被收錄進向量庫與後續定價。判定規則與門檻以 `stores_config.yaml` 設定驅動，
不寫死於程式碼。

### 非目標

- 不改動既有 variant prefix `セット` 過濾邏輯（`_strip_prefix` 相關，items.py:122-126）。
  **理由（與本功能互補、不重疊）**：既有過濾測的是 `_strip_prefix` 切出的
  **選項群組前綴**（Shopify variant title `選項群組 / 選項值` 的前段），前綴為
  `セット` 時於 per-variant 早期階段（dedup/命名之前）丟棄，且該前綴會被剝除、
  不留在 `item_name`。本功能的判定只看 `item_name`（殘餘的選項值），(B) 還要求
  `item_name` 含「セット」。因此當前綴是 `セット` 但殘餘值不含「セット」時
  （例如 `セット / 全部入り` → 殘餘 `全部入り`），只有既有 prefix 過濾擋得到，
  本功能看不到。兩者作用於不同欄位、不同階段，缺一不可；移除 prefix 過濾會讓
  既有測試 `test_excludes_set_and_zero_price`（tests/test_items.py:16-27）紅燈。
- 不處理跨商品的套組關聯，只在單一商品的拆解結果內判斷。
- 不對 `keep_keywords` 白名單做語意推斷，僅做子字串比對。

## 判定規則

判定在 `decompose_items` 內、所有 `ProductItem` 組好之後，以一個 post-pass 套用。
peer 集合採 **leave-one-out**：對每個受測 item，peer = 同一次 `decompose_items`
呼叫產出的**全部其他 `ProductItem`**（包含後續可能也被判為 bundle 而排除者；
median 基準在過濾前的完整 item 集合上計算，不會因排除順序而改變），每個 item
各自排除自身。peer 價格取 `price_jpy`，僅納入 `price_jpy > 0` 者。

對每個 `ProductItem`，符合下列任一條件即視為整套打包選項並排除：

- **(A) 關鍵字命中**：`item_name` 含 `bundle_set.keywords` 中任一子字串。
  命中即排除，**不看價格**（解決混服飾套組 ratio 偏低的 false negative）。
- **(B) 價格 tie-breaker**：同時滿足以下三者
  1. `item_name` 含「セット」三字（限定在 set 名項目，避免誤殺一般高價單品）；
  2. 該 item 至少有一個同商品 peer，且 `price_jpy / median(peer price_jpy) ≥ bundle_set.price_ratio`；
  3. `item_name` 不含 `bundle_set.keep_keywords` 中任一子字串（單品型白名單保護）。

比對規則：

- `keywords`、`keep_keywords` 一律以**子字串包含**（`in`）比對，不做正規化、不做
  前後綴限制（例如 `応援セット 雪花ラミィver.` 中 `応援セット` 在字首仍命中）。
- 中位數使用 `statistics.median`，peer 價格清單僅納入 `price_jpy > 0` 的 peer
  （與既有「排除 ¥0 變體」一致；正常情況拆解後不會有 0 價 item，此為防呆）。
- 條件 (B) 在「無任何 peer」時不成立（缺少可比較基準）；但條件 (A) 與 peer
  數量無關，故「單一 variant 的整組商品且名稱含關鍵字」仍會被 (A) 排除。

### 預設設定值

```yaml
bundle_set:
  keywords: [グッズセット, フルセット, 応援セット, 語セット]
  price_ratio: 5.0
  keep_keywords: [ステッカーセット, 缶バッジセット, クリアファイルセット, キーホルダーセット,
                  カードセット, ブロマイドセット, ポスターセット, ボイスセット,
                  カトラリーセット, ステーショナリーセット, チャームセット]
```

驗證行為對照（以實際資料）：

- `バースデーグッズセット`（含混服飾、ratio 0.75～1.9）→ (A) 命中 `グッズセット` → 排除。
- `アクリルスタンド hololive stage1 セット`（ratio 27，無關鍵字）→ (B) 命中 → 排除。
- `キービジュアルクリアファイルセット`（ratio 5.5）→ (B) 第 3 款被 `クリアファイルセット`
  白名單擋下 → 保留。
- `ステッカー＆缶バッジセット`（ratio 0.1～0.2，無關鍵字）→ (A)(B) 皆不成立 → 保留。

## 元件設計

### 1. `config_schema.py`：`BundleSetPolicy` 與 `AppConfig.bundle_set`

新增 frozen dataclass：

```python
@dataclass(frozen=True)
class BundleSetPolicy:
    keywords: frozenset[str] = frozenset()
    price_ratio: float = 5.0
    keep_keywords: frozenset[str] = frozenset()

    def validate(self):
        if self.price_ratio <= 0:
            raise ValueError("bundle_set.price_ratio must be > 0")
```

- `AppConfig` 新增欄位 `bundle_set: BundleSetPolicy = field(default_factory=BundleSetPolicy)`。
- yaml 解析的實際編輯點在 `load_config`（`from_yaml` 僅 delegate 到 `load_config`，
  config_schema.py:182-195），與既有 `talents=frozenset(yaml_data.get("talents", []) ...)`
  同一處構造 `AppConfig`。缺少 `bundle_set` 區塊時建立空 `keywords`/`keep_keywords`、
  `price_ratio` 用預設 5.0 的 `BundleSetPolicy`。
- **預設（空清單）行為**：`keywords` 空 → (A) 完全停用；`keep_keywords` 空 →
  (B) 第 3 款 `not any(...)` 恆為 True，故 (B) 仍對任何「含『セット』且
  `price_jpy / median(peer) ≥ 5.0`」的 item 生效——這是刻意保留的價格防線，
  非「完全不過濾」。本專案在 yaml 顯式提供完整三項；預設空清單只影響未設定
  此區塊的其他環境。
- `AppConfig.validate()` 內呼叫 `self.bundle_set.validate()`。

解析細節（於 `load_config` 內構造 `AppConfig` 處）：

```python
bs = yaml_data.get("bundle_set") or {}
bundle_set=BundleSetPolicy(
    keywords=frozenset(bs.get("keywords", []) or []),
    price_ratio=float(bs.get("price_ratio", 5.0)),
    keep_keywords=frozenset(bs.get("keep_keywords", []) or []),
)
```

### 2. `sync/items.py`：bundle 過濾 post-pass

- `DecomposeResult` 新增 `excluded_bundle: int` 欄位。
- `decompose_items` 簽名在既有 `talents` 之後新增**三個 keyword-only 基本型別參數**
  `bundle_keywords: frozenset[str]`、`bundle_price_ratio: float`、
  `bundle_keep_keywords: frozenset[str]`（**不**傳入 `BundleSetPolicy` 物件）。
  原因：`items.py` 既有風格即以 `talents: frozenset[str]` 等基本型別為介面，
  且避免 `items.py` 依賴 `config_schema` 造成循環 import。拆解工作由呼叫端
  （`_rebuild_product_items`，見「3. 參數傳遞鏈」）從 `BundleSetPolicy` 取出三項傳入。
- 在組好 `items: list[ProductItem]` 之後、回傳之前，套用過濾：

```python
def _is_bundle(item: ProductItem, peers: list[ProductItem],
               keywords: frozenset[str], price_ratio: float,
               keep_keywords: frozenset[str]) -> bool:
    name = item.item_name
    if any(k in name for k in keywords):
        return True
    if "セット" in name and not any(k in name for k in keep_keywords):
        peer_prices = [p.price_jpy for p in peers if p.price_jpy > 0]
        if peer_prices:
            med = statistics.median(peer_prices)
            if med > 0 and item.price_jpy / med >= price_ratio:
                return True
    return False
```

過濾迴圈（peers 為同商品其他 item）：

```python
kept_items: list[ProductItem] = []
excluded_bundle = 0
for item in items:
    peers = [other for other in items if other is not item]
    if _is_bundle(item, peers, bundle_keywords, bundle_price_ratio, bundle_keep_keywords):
        excluded_bundle += 1
        continue
    kept_items.append(item)
```

- `DecomposeResult` 以 `items=kept_items`、新增 `excluded_bundle=excluded_bundle`
  回傳；既有 `excluded_set`、`excluded_zero` 維持不變。
- 在 `items.py` 頂部 import `statistics`。

### 3. 參數傳遞鏈

`BundleSetPolicy` 物件**整路以 keyword 參數傳遞**（沿用 `talents` 既有路徑），
只在最末端的 `_rebuild_product_items` 拆成三個基本型別參數交給 `decompose_items`。
型別註記一律沿用各檔既有的 `TYPE_CHECKING` import 慣例（`async_pipeline.py`、
`cycle.py` 已用 `if TYPE_CHECKING: from estimator_king.config_schema import ...`，
搭配 `from __future__ import annotations` 的字串化註記）：

- `crawler/cycle.py`：在 `TYPE_CHECKING` 區塊既有 `AppConfig` import 即足夠（不需新增）；
  呼叫 `async_process_queue(..., talents=config.talents, ...)` 時新增
  `bundle_set=config.bundle_set`（cycle.py:62 附近）。
- `crawler/async_pipeline.py`：`TYPE_CHECKING` 區塊新增 `BundleSetPolicy`；
  `async_process_queue` 簽名（async_pipeline.py:53）新增 keyword-only
  `bundle_set: BundleSetPolicy`；呼叫 `sync_products(...)`（async_pipeline.py:85）時下傳
  `bundle_set=bundle_set`。
- `sync/engine.py`：在 `TYPE_CHECKING` 區塊新增
  `from estimator_king.config_schema import BundleSetPolicy`（engine.py 目前未 import
  config_schema；config_schema 不 import engine，故無循環風險，但仍以 TYPE_CHECKING
  保持與專案慣例一致並零執行期成本）。
  - `sync_products` 簽名（engine.py:127）新增 keyword-only `bundle_set: BundleSetPolicy`。
  - `sync_products` 呼叫 `_rebuild_product_items(...)`（engine.py:166-169）時，於參數列
    末端傳入 `bundle_set`。
  - **`_rebuild_product_items` 是 `decompose_items` 的真正呼叫點**（`sync_products` 不直接
    呼叫 `decompose_items`）。`_rebuild_product_items` 簽名（engine.py:212）新增
    `bundle_set: BundleSetPolicy`；其內呼叫 `decompose_items`（engine.py:228）時改為
    `decompose_items(snapshot, talents=talents, bundle_keywords=bundle_set.keywords,
    bundle_price_ratio=bundle_set.price_ratio, bundle_keep_keywords=bundle_set.keep_keywords)`。

#### `excluded_bundle` 觀測彙整鏈（明確做，非選用）

- `RebuildReport`（engine.py:70-75）新增欄位 `excluded_bundle: int`；
  `_rebuild_product_items` 回傳時（engine.py:272-276）帶入
  `excluded_bundle=decomposed.excluded_bundle`。
- `sync_products` 累加（engine.py:178）改為
  `result.excluded += report.excluded_set + report.excluded_zero + report.excluded_bundle`。
  （`SyncResult.excluded` 是單一總數，因此 `PipelineResult.excluded` 與
  `async_pipeline._aggregate_lines` 不需改動即可把 bundle 併入「excluded」總計。）
- `_format_product_tree`（engine.py:100-108）簽名新增 `excluded_bundle: int`，
  `excluded = excluded_set + excluded_zero + excluded_bundle`，head 字串改為
  `… excluded (SET×{excluded_set}, ¥0×{excluded_zero}, bundle×{excluded_bundle})`；
  其唯一呼叫端（engine.py:185-187）一併傳入 `report.excluded_bundle`。

### 4. 設定檔與重新索引

- `stores_config.yaml` 新增上述 `bundle_set` 區塊（顯式三項）。
- 因 decomposition 輸出改變（套組 item 不再收錄），既有 ChromaDB 已索引的套組
  item 會 stale。依 [CLAUDE.md](../../../CLAUDE.md) gotcha，需強制全量 re-index：
  將 `stores_config.yaml` 的 `item_types_version` +1，下次 crawl 會自動觸發
  re-index。交付時提示使用者執行 `crawl --force-refetch`。

## 資料流

```text
AppConfig.from_yaml(stores_config.yaml)  [parse in load_config]
  → AppConfig.bundle_set: BundleSetPolicy
  → cycle.async_process_queue(bundle_set=config.bundle_set)
  → async_pipeline.async_process_queue(bundle_set=...)
  → engine.sync_products(bundle_set=...)
  → engine._rebuild_product_items(bundle_set=...)        # decompose_items 的真正呼叫點
  → decompose_items(snapshot, talents=...,
        bundle_keywords=bundle_set.keywords,
        bundle_price_ratio=bundle_set.price_ratio,
        bundle_keep_keywords=bundle_set.keep_keywords)
  → post-pass _is_bundle 過濾 → DecomposeResult(items=kept, excluded_bundle=N, ...)
  → RebuildReport(excluded_bundle=...) → SyncResult.excluded(+bundle) → log tree (bundle×N)
```

## 錯誤處理

- `price_ratio <= 0`：`BundleSetPolicy.validate()` 拋 `ValueError`，在
  `AppConfig.validate()` 階段即失敗，不進入 crawl。
- `bundle_set` 區塊缺失或欄位缺失：以空 `frozenset` 與預設 `price_ratio=5.0`
  容錯，不拋例外。
- 拆解時 peer 全為 0 價或無 peer：條件 (B) 自然不成立，不會除以 0
  （`peer_prices` 為空時略過、`med > 0` 再做除法）。

## 測試

於 `tests/`（沿用既有 `ProductSnapshot`／variant 假資料建構慣例與測試風格）：

`decompose_items` 行為：

1. 關鍵字命中 → 該 item 被排除，`excluded_bundle` 計數正確、其他 item 保留。
2. `keep_keywords` 白名單保護：名稱含 `クリアファイルセット` 且 ratio 高（>price_ratio）
   → 仍保留。
3. 價格 tie-breaker：名稱含「セット」、無關鍵字、不在白名單、ratio ≥ price_ratio
   （模擬 stage セット）→ 排除。
4. 邊界：關鍵字命中但無同商品 peer（單一 item）→ 仍被 (A) 排除。
5. 反例：名稱含「セット」、ratio < price_ratio、非關鍵字、單品型 → 保留。
6. `excluded_bundle` 與既有 `excluded_set`／`excluded_zero` 計數互不干擾。

`config_schema` 行為：

7. 解析含 `bundle_set` 區塊的 yaml → `keywords`/`keep_keywords` 為 frozenset、
   `price_ratio` 為 float。
8. 缺少 `bundle_set` 區塊 → 取得空 `keywords`/`keep_keywords`、`price_ratio=5.0`，
   `validate()` 通過。
9. `price_ratio <= 0` → `validate()` 拋 `ValueError`。

驗證工具鏈（依 [CLAUDE.md](../../../CLAUDE.md)）：

- Type check：`.venv/bin/basedpyright estimator_king/sync/items.py estimator_king/config_schema.py estimator_king/sync/engine.py estimator_king/crawler/cycle.py estimator_king/crawler/async_pipeline.py`（生產碼 0 errors）
- Lint：`uvx ruff check <改動檔案>`
- 測試：`.venv/bin/python -m pytest <相關測試檔> -v -o addopts=""`

## 開放決策（已於 brainstorming 確認）

- 判定規則：名稱關鍵字為主、價格只當 tie-breaker。✓
- 設定位置：`stores_config.yaml` + `config_schema.py`。✓
- 預設空清單 = 不啟用（本專案 yaml 顯式開啟）。✓
- 重新索引：bump `item_types_version`。✓
