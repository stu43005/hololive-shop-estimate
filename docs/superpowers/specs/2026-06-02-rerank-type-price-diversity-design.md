# 檢索 rerank 加入 item_type／price_jpy 多樣性 + 候選池加深 — 設計規格

日期：2026-06-02

繼 [2026-05-31-chromadb-item-indexing-design.md](2026-05-31-chromadb-item-indexing-design.md)（已實作落地）。數值校準見 [2026-06-02-rerank-calibration-appendix.md](../../2026-06-02-rerank-calibration-appendix.md)。

## 1. 目標

`/estimate` 查詢端的候選 rerank 目前只看相似度與新近度（recency），導致 top_k 常被單一「相同類型且相同價格」的近似重複品佔滿，送進 chat model 的參考價格分佈失真；且每個 where-query 只撈 `top_k` 筆，recency 幾乎只能在窗內重排、無法把池外較新的同類品換進來。本設計：

1. 在 rerank 加入 **`(item_type, price_jpy)` 的多樣性偏好**（`diversity_weight`），讓參考集在類型／價位上更分散；
2. 新增 **候選池加深**旋鈕（`fetch_multiplier`），每個 where-query 改撈 `top_k × fetch_multiplier` 筆進池、rerank 後仍只送 `top_k` 給 LLM——放大 recency 的抗通膨槓桿並降低多樣性的相關度代價。

既有 recency 行為**完整保留**（`recency_weight` 維持 0.05）。

**範圍**：純查詢端改動。**不需重建向量**、**不改 metadata**（`item_type`／`price_jpy`／`published_at` 皆已存在於 item 向量 metadata，見前設計 §4.2）。改動集中在 [estimator.py](../../../estimator_king/bot/estimator.py) 的 `_rerank`（多樣性）與 `_estimate_chunk` 的檢索迴圈（加深），外加兩個結構性 config 旋鈕 `diversity_weight`、`fetch_multiplier`。`/estimate` 外部介面不變、送進 LLM 的參考數仍為 `top_k`（無額外 LLM 成本）。

## 2. 現況

- 候選池組成（[estimator.py:111-118](../../../estimator_king/bot/estimator.py)）：對 `classify_query` 命中的每個類型各做一次 `where={item_type:T}` 查詢 + 一次純 embedding 查詢，每次 `n_results = self._top_k`（[estimator.py:115](../../../estimator_king/bot/estimator.py)，即 fetch 等效 1×），合併去重（by 向量 ID，保留最小 distance）。池大小 ≤ (N+1)×`top_k`。
- 現有 rerank（[estimator.py:130-146](../../../estimator_king/bot/estimator.py)）：

  ```text
  score = (1 - distance) + recency_weight * recency_norm
  ```

  一次性 `sorted(by score)`，呼叫端再 `[: top_k]`（[estimator.py:119](../../../estimator_king/bot/estimator.py)）。
- 實證問題（真實 `chroma/`，見校準附錄 §A）：7/8 條代表查詢的 top_k 被單一 `(item_type, price_jpy)` 主宰，重複群大小平均 3.64、最大 7（如 `缶バッジ こより` 的 top_k 有 7/10 格皆 `缶バッジ/¥500`）。fetch=1× 時 recency 幾乎只在窗內重排（抗通膨 Δage 僅 +21d，見附錄 §B/§C）。

## 3. 演算法

### 3.1 貪婪 MMR（exact `(item_type, price_jpy)` 鍵 + 遞增計數懲罰）

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
- 複雜度 O(k × pool)，pool ≤ (N+1)×top_k×fetch_multiplier（~60）、k = 10，可忽略。

#### `_rerank` 改寫範圍

`_rerank(self, hits: list[_Hit]) -> list[_Hit]` 簽名不變、回傳「排序後的全體候選」（呼叫端 [estimator.py:119](../../../estimator_king/bot/estimator.py) 的 `[: self._top_k]` 維持不動）。內部：

1. 沿用現有 `pubs`／`positive`／`min_pub`／`max_pub`／`span` 與內部 `base(h)`（即現有 `score` 函式，更名語義為 base）計算。
2. 預先算好 `base_by_id: dict[str, float]`（或對每個 h 即時呼叫 `base`），避免貪婪迴圈內重算 recency 統計。
3. 以上述貪婪迴圈產生並回傳排序後清單。

### 3.2 候選池加深（`fetch_multiplier`）

`_estimate_chunk` 的檢索迴圈（[estimator.py:114-118](../../../estimator_king/bot/estimator.py)）中，每個 where-query 的 `n_results` 由 `self._top_k` 改為 `self._top_k * self._fetch_multiplier`。合併去重邏輯不變；rerank 後 `[: self._top_k]`（[estimator.py:119](../../../estimator_king/bot/estimator.py)）不變——**送進 LLM 的參考數仍為 `top_k`**。

- 更深的池讓 §3.1 的貪婪 MMR 有更多「同相關度但不同 `(type,price)`」候選可選（分散度↑、相關度代價↓），並讓 recency 能把池外較新的同類品換進集合（抗通膨槓桿，見附錄 §C）。
- `fetch_multiplier = 1` 時行為等同現況（pool ≤ (N+1)×top_k）。

## 4. 數值決定（校準見附錄）

依 [校準附錄](../../2026-06-02-rerank-calibration-appendix.md) 真實資料實測，採用 `(fetch_multiplier=2, recency_weight=0.05, diversity_weight=0.05)`：

| 旋鈕 | 值 | 依據 |
| --- | --- | --- |
| `recency_weight` | **0.05**（維持現行） | 附錄 §B：能完全重排近似平手帶、又不越過任一池相關度斷層的最大值；相關度代價趨近零。 |
| `diversity_weight` | **0.05** | 附錄 §A：化解最糟緊密群（`缶バッジ¥500` distinct 4→7）且收益飽和；附錄 §C：平衡操作點。 |
| `fetch_multiplier` | **2** | 附錄 §C：`(2,0.05,0.05)` 全面支配現行 `(1,0.05,0)`——distinct 6.38→8.88、Δage +21→+106d、相關度代價 −0.0048（可忽略），且無額外 LLM 成本。 |

**recency ↔ diversity 是耦合的**（附錄 §C 新發現）：最新品常擠在同一 `(type,price)` 群，diversity 的硬性懲罰蓋過 recency 小加分，「新但重複」輸給「舊但不同」，故 diversity 會把 recency 的抗通膨效益砍掉約一半（2× 下 +230d→+106d）但保住分散度（8.88）。所有組合相關度代價皆 <1% → 相關度不是限制因素，真正取捨是「用最新價格 ↔ 價位分散」。`(2,0.05,0.05)` 取平衡：兩軸都大幅勝現況。`diversity_weight` 不應獨立挑——日後調優先目標時，抗通膨優先可降 dw（→0.03/0），分散優先可升 dw（→0.10）。

三個預設值落在 `stores_config.yaml`，可調；缺鍵時程式回落（`recency_weight`/`diversity_weight` → 0.05、`fetch_multiplier` → 2）。

## 5. 接線（與既有 `recency_weight` 完全同模式）

### 5.1 `diversity_weight`

1. **`AppConfig`**（[config_schema.py:126](../../../estimator_king/config_schema.py)，緊接 `estimator_recency_weight` 後）新增 `estimator_diversity_weight: float = 0.05`。
2. **`load_config`**（[config_schema.py:287](../../../estimator_king/config_schema.py)，緊接 `estimator_recency_weight=...` 後）新增 `estimator_diversity_weight=float(est.get("diversity_weight", 0.05)),`。
3. **`Estimator.__init__`**（[estimator.py:71-82](../../../estimator_king/bot/estimator.py)）新增 keyword 參數 `diversity_weight: float = 0.05`，存為 `self._diversity_weight`（緊接 `self._recency_weight` 後）。
4. **`build_bot`**（[runner.py:47-53](../../../estimator_king/bot/runner.py)）`Estimator(...)` 新增 `diversity_weight=config.estimator_diversity_weight`（緊接 `recency_weight=...` 後）。

### 5.2 `fetch_multiplier`

1. **`AppConfig`** 新增 `estimator_fetch_multiplier: int = 2`（緊接 `estimator_diversity_weight` 後）。
2. **`load_config`** 新增 `estimator_fetch_multiplier=int(est.get("fetch_multiplier", 2)),`（緊接 `estimator_diversity_weight=...` 後）。
3. **`Estimator.__init__`** 新增 keyword 參數 `fetch_multiplier: int = 2`，存為 `self._fetch_multiplier`。
4. **`build_bot`** `Estimator(...)` 新增 `fetch_multiplier=config.estimator_fetch_multiplier`。
5. **`_estimate_chunk` 檢索迴圈**（[estimator.py:115](../../../estimator_king/bot/estimator.py)）：`self._vector_store.query(embedding, self._top_k, where=where)` → `self._vector_store.query(embedding, self._top_k * self._fetch_multiplier, where=where)`。

### 5.3 `stores_config.yaml`

`estimator:` 區塊（[stores_config.yaml:274-276](../../../stores_config.yaml)）新增兩行：

```yaml
estimator:
  top_k: 10
  recency_weight: 0.05
  diversity_weight: 0.05
  fetch_multiplier: 2
```

## 6. 決定性與邊界

- **決定性**：base 由查詢結果（distance）與 metadata（published_at）決定；貪婪挑選平手以 remaining 索引最小者決勝，候選池順序源自合併時的 dict 插入序（查詢順序 + 各查詢回傳順序，皆決定性）→ 整體可重現。
- **空池 / 單筆**：`remaining` 為空或只剩 1 筆時迴圈自然處理；回傳長度 == 池大小。
- **池內全部同鍵**：遞增懲罰只把同鍵者依 base 由高到低排列（dup 計數對「尚未選入者」一致），等同 base 排序——無害。
- **缺 metadata**：`item_type`／`price_jpy` 缺失時防禦性轉型為 `""`／`0`，視為一個合法鍵參與去重。
- **`diversity_weight = 0`**：退化為純 base 排序。**`fetch_multiplier = 1`**：退化為現有池大小。兩者皆設預設（0.05／2），但允許關閉以回退。
- **`fetch_multiplier` 加深的成本**：vector-store 查詢與 rerank 規模隨倍率線性增長（仍可忽略，pool≤~60、O(k×pool)）；送進 LLM 仍為 top_k，**無 LLM 成本變化**。

## 7. 測試（加進 `tests/test_estimator.py`，沿用既有 fakes 慣例）

- **多樣性分散**：候選含同 `(type, price)` 多筆 + 數筆不同價/不同類；`diversity_weight > 0` 時，rerank 後同鍵的第 2、3 筆被推到不同價/不同類候選之後（斷言 top_k 內 distinct `(type,price)` 鍵數較 `diversity_weight=0` 時多）。
- **不同價同類型不被罰**：同 `item_type` 但價格各異的候選之間相對順序僅由 base 決定（不因彼此而降分）。
- **recency 仍作用於 base**：fake hits 帶不同 `published_at`，在無重複群時排序結果與現有 recency-only 行為一致。
- **`diversity_weight = 0` 退化**：結果等同現有純 base `sorted`。
- **平手決定性**：兩筆 base 相同、鍵不同時，取原池順序在前者。
- **`fetch_multiplier` 加深檢索**：fake `_VectorStore` 記錄收到的 `n_results`，斷言每個 where-query 以 `top_k * fetch_multiplier` 呼叫；`fetch_multiplier = 1` 時以 `top_k` 呼叫（等同現況）；無論倍率，送進 chat 的參考數仍 ≤ `top_k`。
- **`load_config` 解析**：`estimator.diversity_weight`／`estimator.fetch_multiplier` 正確讀入；缺鍵時分別回落 0.05／2（加進 `tests/test_config_schema.py`）。
- 驗證工具鏈（[CLAUDE.md](../../../CLAUDE.md)）：`.venv/bin/basedpyright estimator_king`（prod 0 error）、`uvx ruff check`、相關 `pytest -o addopts=""`。

## 8. 校準

完整數據與推導見 [2026-06-02-rerank-calibration-appendix.md](../../2026-06-02-rerank-calibration-appendix.md)（§A diversity_weight、§B recency_weight、§C fetch×recency×diversity 聯合網格）。驗證腳本置於 `scripts/analysis/`（與維護腳本分開），可重跑：`set -a; source .env; set +a; PYTHONPATH=. .venv/bin/python scripts/analysis/calibrate_rerank_grid.py`。

## 9. 遷移

- 資料／向量：**無**（不改 metadata、不重嵌）。
- 設定：`stores_config.yaml` 新增 `diversity_weight: 0.05`、`fetch_multiplier: 2`；既有設定缺該鍵時程式回落（0.05／2），無痛升級。
