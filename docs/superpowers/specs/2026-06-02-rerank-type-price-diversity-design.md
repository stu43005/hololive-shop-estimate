# 檢索 rerank 加入 item_type／price_jpy 多樣性 — 設計規格

日期：2026-06-02

繼 [2026-05-31-chromadb-item-indexing-design.md](2026-05-31-chromadb-item-indexing-design.md)（已實作落地）。

## 1. 目標

`/estimate` 查詢端的候選 rerank 目前只看相似度與新近度（recency），導致 top_k 常被單一「相同類型且相同價格」的近似重複品佔滿，送進 chat model 的參考價格分佈失真。本設計在 rerank 加入 **`(item_type, price_jpy)` 的多樣性偏好**，讓最終參考集在類型／價位上更分散；同時**完整保留**既有 recency 行為。

**範圍**：純查詢端改動。**不需重建向量**、**不改 metadata**（`item_type`／`price_jpy`／`published_at` 皆已存在於 item 向量 metadata，見前設計 §4.2）。改動集中在 [estimator.py](../../../estimator_king/bot/estimator.py) 的 `_rerank`，外加一個結構性 config 旋鈕 `diversity_weight`。`/estimate` 外部介面不變。

## 2. 現況

- 候選池組成（[estimator.py:111-118](../../../estimator_king/bot/estimator.py)）：對 `classify_query` 命中的每個類型各做一次 `where={item_type:T}` 查詢 + 一次純 embedding 查詢，合併去重（by 向量 ID，保留最小 distance）。池大小 ≤ (N+1)×`top_k`。
- 現有 rerank（[estimator.py:130-146](../../../estimator_king/bot/estimator.py)）：

  ```text
  score = (1 - distance) + recency_weight * recency_norm
  ```

  一次性 `sorted(by score)`，呼叫端再 `[: top_k]`（[estimator.py:119](../../../estimator_king/bot/estimator.py)）。
- 實證問題（真實 `chroma/` 取樣 8 條代表查詢，見 §8 附錄）：7/8 條的 top_k 被單一 `(item_type, price_jpy)` 主宰，重複群大小平均 3.64、最大 7（如 `缶バッジ こより` 的 top_k 有 7/10 格皆為 `缶バッジ/¥500`）。

## 3. 演算法：貪婪 MMR（exact `(item_type, price_jpy)` 鍵 + 遞增計數懲罰）

把現有「一次性排序」改為**逐筆貪婪挑選**，base 分數保留現有 recency：

```text
base(h)  = (1 - h.distance) + recency_weight * recency_norm(h)   # 與現況完全一致
selected = []
remaining = list(候選池)
while remaining:
    對 remaining 中每個 h：
        dup   = 已 selected 中與 h 同 (item_type, price_jpy) 的筆數
        score = base(h) - diversity_weight * dup
    取 score 最大者；平手時取 remaining 中索引最小者（穩定、決定性）
    將該筆自 remaining 移出、append 到 selected
return selected                                                   # 已排序的全體；呼叫端仍 [: top_k]
```

關鍵性質：

- **recency 零變動**：`base` 內的 `recency_norm` 沿用現有計算——以本次候選池中 `published_at > 0` 的項取 `min_pub`／`max_pub`，`span = max_pub - min_pub`；`published_at == 0` 或 `span == 0` 時 `recency_norm = 0`。`recency_weight` 仍為注入值（預設 0.05）。
- **第 1 筆永遠 `dup = 0`** → 等同現有最高 base 那筆，與現況相容。
- **同 `(type, price)` 的第 n 筆**被扣 `diversity_weight × (n-1)`：遞增，群越大越往後推。
- **不同價或不同類型不互相懲罰**：價格分佈完整保留；離題類型本來相似度就低、自然排後。
- **`diversity_weight = 0` 時完全退化回現有純 base 排序**（安全閥）。
- key 取值防禦性轉型：`str(h.metadata.get("item_type", "") or "")`、`int(h.metadata.get("price_jpy", 0) or 0)`。
- 複雜度 O(k × pool)，pool ≤ ~30、k = 10，可忽略。

### 3.1 `_rerank` 改寫範圍

`_rerank(self, hits: list[_Hit]) -> list[_Hit]` 簽名不變、回傳「排序後的全體候選」（呼叫端 [estimator.py:119](../../../estimator_king/bot/estimator.py) 的 `[: self._top_k]` 維持不動）。內部：

1. 沿用現有 `pubs`／`positive`／`min_pub`／`max_pub`／`span` 與內部 `base(h)`（即現有 `score` 函式，更名語義為 base）計算。
2. 預先算好 `base_by_id: dict[str, float]`（或對每個 h 即時呼叫 `base`），避免貪婪迴圈內重算 recency 統計。
3. 以上述貪婪迴圈產生並回傳排序後清單。

## 4. 數值決定：`diversity_weight` 預設 0.05

依 §8 真實資料校準（cosine 距離；相鄰 base gap median 0.0052、p90 0.0359；最相關命中與其餘有 ~0.10 斷層）：

- `diversity_weight = 0.05` 剛好完全化解最糟的緊密重複群（`缶バッジ/¥500` 的 distinct 鍵數 4→7），收益在此飽和（再升至 0.10 平均僅 7.38→7.62、且 `缶バッジ` 已飽和於 7）。
- 0.05 **大於** p90 gap（0.036）→ 第 2 筆重複能穩定越過約 90% 的 body 候選；**小於** 0.10 的相關性斷層 → 永不把唯一最相關那筆僅因重複就壓到邊際候選之下。
- 與既有 `recency_weight`（0.05）同值，形成「recency 與 diversity 兩個等權的輕微偏好」的對稱模型。
- ≥0.10 過強（會跨越相關性斷層、過度分散），不採用。合理調校帶 0.02～0.05。

預設值落在 `stores_config.yaml`，可調；缺鍵時程式回落 0.05。

## 5. 接線（與既有 `recency_weight` 完全同模式）

1. **`AppConfig`**（[config_schema.py:126](../../../estimator_king/config_schema.py)，緊接 `estimator_recency_weight` 後）新增：

   ```python
   estimator_diversity_weight: float = 0.05
   ```

2. **`load_config`**（[config_schema.py:287](../../../estimator_king/config_schema.py)，緊接 `estimator_recency_weight=...` 後）新增解析：

   ```python
   estimator_diversity_weight=float(est.get("diversity_weight", 0.05)),
   ```

3. **`stores_config.yaml`** 的 `estimator:` 區塊（[stores_config.yaml:274-276](../../../stores_config.yaml)）新增：

   ```yaml
   estimator:
     top_k: 10
     recency_weight: 0.05
     diversity_weight: 0.05
   ```

4. **`Estimator.__init__`**（[estimator.py:71-82](../../../estimator_king/bot/estimator.py)）新增 keyword 參數 `diversity_weight: float = 0.05`，存為 `self._diversity_weight`（緊接 `self._recency_weight` 後）。

5. **`build_bot`**（[runner.py:47-53](../../../estimator_king/bot/runner.py)）構建 `Estimator(...)` 時新增 `diversity_weight=config.estimator_diversity_weight`（緊接 `recency_weight=...` 後）。

## 6. 決定性與邊界

- **決定性**：base 由查詢結果（distance）與 metadata（published_at）決定；貪婪挑選平手以 remaining 索引最小者決勝，候選池順序源自合併時的 dict 插入序（查詢順序 + 各查詢回傳順序，皆決定性）→ 整體可重現。
- **空池 / 單筆**：`remaining` 為空或只剩 1 筆時迴圈自然處理；回傳長度 == 池大小。
- **池內全部同鍵**：遞增懲罰只是把同鍵者依 base 由高到低排列（懲罰對同鍵各筆的相對順序無影響，因 dup 計數對「尚未選入者」一致），等同 base 排序——無害。
- **缺 metadata**：`item_type`／`price_jpy` 缺失時防禦性轉型為 `""`／`0`，視為一個合法鍵參與去重。
- **`diversity_weight = 0`**：退化為現有純 base 排序。

## 7. 測試（加進 `tests/test_estimator.py`，沿用既有 fakes 慣例）

- **多樣性分散**：候選含同 `(type, price)` 多筆 + 數筆不同價/不同類；`diversity_weight > 0` 時，rerank 後同鍵的第 2、3 筆被推到不同價/不同類候選之後（斷言 top_k 內 distinct `(type,price)` 鍵數較 `diversity_weight=0` 時多）。
- **不同價同類型不被罰**：同 `item_type` 但價格各異的候選之間相對順序僅由 base 決定（不因彼此而降分）。
- **recency 仍作用於 base**：fake hits 帶不同 `published_at`，在無重複群時排序結果與現有 recency-only 行為一致。
- **`diversity_weight = 0` 退化**：結果等同現有純 base `sorted`。
- **平手決定性**：兩筆 base 相同、鍵不同時，取原池順序在前者。
- **`load_config` 解析**：`estimator.diversity_weight` 正確讀入；缺鍵時回落 0.05（加進 `tests/test_config_schema.py`）。
- 驗證工具鏈（[CLAUDE.md](../../../CLAUDE.md)）：`.venv/bin/basedpyright estimator_king`（prod 0 error）、`uvx ruff check`、相關 `pytest -o addopts=""`。

## 8. 校準附錄（真實資料實測，2026-06-02）

工具：`scripts/calibrate_diversity_weight.py`（忠實重現 `_estimate_chunk` 檢索：`classify_query` → N 個 type-filtered query + 1 個 plain query → 合併去重 → base 分數）。資料源：當前 `chroma/`（cosine 距離，[store.py:36](../../../estimator_king/vectorstore/store.py)）。8 條代表查詢。

**相鄰 base gap 分佈（n=99）**：median 0.0052、p25 0.0014、p75 0.0134、p90 0.0359、mean 0.0130；最相關命中與其餘有 ~0.10–0.115 的明顯斷層。

**重複情形**：7/8 條 top_k 含 ≥1 個 `(type, price)` 重複群；群大小 `[7,6,6,4,4,3,2,2,2,2,2]`（max 7、mean 3.64）。

**各 `diversity_weight` 下 top_k 的 distinct `(type,price)` 鍵數（越高越分散，top_k=10）**：

| diversity_weight | 平均 distinct | `缶バッジ¥500×7`（最糟） | `アクキー`（無重複） |
|---|---|---|---|
| 0（現狀） | 6.38 | 4 | 10 |
| 0.02 | 7.00 | 5 | 10 |
| 0.03 | 7.12 | 6 | 10 |
| **0.05** | **7.38** | **7** | **10** |
| 0.10 | 7.62 | 7（飽和） | 10 |

讀法：0.05 完全化解最糟緊密群且收益飽和；無重複的池在所有值維持 10（不會過度分散）；pool≈top_k 的池（無多餘候選）早早飽和，證明懲罰只在池中真有多餘候選時生效。據此定預設 0.05。

## 9. 遷移

- 資料／向量：**無**（不改 metadata、不重嵌）。
- 設定：`stores_config.yaml` 新增一行 `diversity_weight: 0.05`；既有設定缺該鍵時程式回落 0.05，無痛升級。
