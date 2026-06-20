# 估價準確率優化（第三輪）— selective deterministic anchor floor

日期：2026-06-20
範圍：`/estimate` 估價結果的第三輪準確率優化。以**設定驅動的後處理上錨（anchor floor）**消除系統性低估，全部集中在 [estimator.py](../../../estimator_king/bot/estimator.py) 的後處理層 + [config_schema.py](../../../estimator_king/config_schema.py) + [stores_config.yaml](../../../stores_config.yaml)，外加既有量測腳本的對齊。延續 [2026-06-10 第一輪](2026-06-10-estimate-accuracy-design.md) 與 [2026-06-16 第二輪](2026-06-16-estimate-accuracy-anchoring-design.md)。

## 背景與證據

前兩輪以 prompt 處理低估：第一輪加含稅格點 snap + 重寫 prompt，第二輪加 `<anchoring>`（median-to-upper）、`<set_and_count>`、`<range_and_confidence>`。用 [eval_estimate.py](../../../scripts/analysis/eval_estimate.py)（25 筆有正解價的 fixture、排除本尊後估價）量測：

- **MAPE 18.8% → 14.2%**（精度顯著改善）。
- range 覆蓋率 88%、no-estimate 0%（皆良好）。
- **但 mean signed err 從 −9.9% 到 −10.2%，系統性低估完全沒解** —— 兩輪 prompt 對方向性偏差免疫。

### 根因（已確認，非本輪重新調查）

第二輪的 retrieval 實測已證明：失誤品項中多數的「正確/上端比價本來就在 refs 裡」，模型卻仍估在同類 refs 中位數以下（`ポーチ` 估 2750、refs 中位 ≈3400、實價 4400）。最嚴重的幾筆（`ボイス1種`、`ピンバッジ2個セット`、`温感マグカップ`）正是 prompt 規則正面瞄準、卻仍失效的案例。**結論：低估是 gpt-5.4-mini 不可靠遵循軟性 anchoring 指令所致，與 retrieval / recency / 通膨無關。** 因此本輪改用 deterministic guardrail，把 prompt 失效的軟規則用程式硬化。

### Quick verify（決定本輪設計的關鍵實測）

以 [experiment_anchor_floor.py](../../../scripts/analysis/experiment_anchor_floor.py)（重現 eval retrieval + 本尊排除，對**同一批 chat 輸出**配對套用各種 floor policy，零額外 API 成本、policy 間零噪音）量測：

1. **Blanket median floor 證明上錨有真實訊號**：把 signed err 從 −10.3% 砍到 −3.9%（6.4pp，遠超 ±1～2pp 抽樣雜訊），但 MAPE 升到 15.9% —— 因為它把少數「本來就該便宜、模型也估對」的品項（`ハート型缶バッジ`、`ストラップ`）過度抬高。p75 更過矯正到 +6.9%（把低估換成高估）。

2. **單一百分位掃描，按一般品/溢價品分組**（溢價品 = 命中 `温感`/`もこもこ`/`あったか`/`なりきり` 的 4 筆，其餘 21 筆為一般品）：

   | 旋鈕 | 全體 signed | 一般品 signed | 一般品 MAPE | 溢價品 signed | 溢價品 MAPE |
   | --- | --- | --- | --- | --- | --- |
   | none | −11.0% | −11.2% | 16.9% | −10.1% | 10.1% |
   | p50 | −4.5% | −4.0% | 18.7% | −6.7% | 9.2% |
   | p55 | −1.7% | −1.1% | 18.5% | −5.0% | 10.0% |
   | **p60** | −0.9% | **−0.5%** | 18.4% | −3.3% | 10.8% |
   | p65 | +0.8% | +1.4% | 18.7% | −2.0% | 9.5% |
   | **p70** | +3.2% | +3.8% | 19.6% | **+0.1%** | 7.4% |
   | p75 | +6.5% | +7.4% | 21.4% | +1.9% | 5.6% |

   （baseline 因 chat 非決定性在不同 run 介於 −10.3%～−11.0%；掃描內部為配對、趨勢可信。）

**核心發現——單一旋鈕無法同時服務兩組**：一般品的最佳點在 ~p60（signed −0.5%），溢價品的最佳點在 ~p70（signed +0.1%），兩者相差約 10 個百分位。且 MAPE 走勢相反：旋鈕升高時一般品 MAPE **單調變差**（floor 誤傷便宜正確品），溢價品 MAPE 反而**單調變好**（溢價品本就該更貴，往上錨是修正）。因此正解是**分層 floor**：一般品 p60、溢價品 p70，各取各的最佳點。這是任何單一旋鈕做不到的。

### 取捨基準（已與使用者確認）

驗收**優先殺低估**：以 signed err 趨近 0 為首要目標，接受少數便宜品被小幅抬高、整體 MAPE 升約 1～2pp（落在抽樣雜訊帶）。

## 設計總覽

新增一個**設定驅動的分層後處理上錨**，套在 `estimate_products` 的 `_reconcile` 之後、`_snap_estimate` 之前：對每筆估價，用「送進模型的同類 refs」算出一個百分位 floor，`suggested = max(suggested, floor)`（只抬不壓）。floor 的百分位依 query 是否命中設定中的溢價關鍵字分層決定。

改動範圍：

1. [stores_config.yaml](../../../stores_config.yaml)：新增 `estimator.anchor_floor`（一般品旋鈕 + 溢價分層清單，含關鍵字）。
2. [config_schema.py](../../../estimator_king/config_schema.py)：新增 `AnchorTier` / `AnchorFloorConfig` dataclass + 解析 + 驗證。
3. [estimator.py](../../../estimator_king/bot/estimator.py)：`_estimate_chunk` 多回傳同類 ref 價格；新增純函式 `_percentile` 與 `_anchor_floor`；`estimate_products` 串接；`Estimator.__init__` 收 config。
4. [runner.py](../../../estimator_king/bot/runner.py)：建 `Estimator` 時傳入 `config.estimator_anchor_floor`。
5. [eval_estimate.py](../../../scripts/analysis/eval_estimate.py)：套用 `_anchor_floor`，使 before/after 量到上線行為。
6. [docs/data-pipeline.md](../../../docs/data-pipeline.md)：同步新階段。

## 元件設計

### §1 Config 形狀（[stores_config.yaml](../../../stores_config.yaml)）

於現有 `estimator:` 區塊新增：

```yaml
estimator:
  top_k: 10
  recency_weight: 0.05
  diversity_weight: 0.05
  fetch_multiplier: 2
  anchor_floor:
    general_percentile: 60          # 一般品旋鈕（0–100 整數）；整段省略 = 關閉 floor
    min_refs: 3                     # 同類 ref 數 < 此值 → 不上錨（稀疏/噪音守門）
    full_percentile_min_refs: 5     # 同類 ref 數 < 此值 → effective_pct 夾為 ≤ 50（小樣本安全預設）
    premium_tiers:                  # 清單：每組各自的旋鈕 + 關鍵字
      - percentile: 70
        keywords: ["温感", "もこもこ", "あったか", "なりきり"]
```

- **一般品旋鈕** = `general_percentile`（單一 0–100 整數）。
- **稀疏守門** = `min_refs`（正整數，預設 3）：同類 ref 數**少於** `min_refs` → floor **no-op**（見 §2）。防止 first-run / 罕見 type / `その他` 噪音下，單一貴 ref 被當權威硬抬。
- **小樣本安全夾（runtime 不變式）** = `full_percentile_min_refs`（正整數，預設 5，須 ≥ `min_refs`）：同類 ref 數 **`[min_refs, full_percentile_min_refs)`** 時，`effective_pct` 一律**夾為 ≤ 50（median）**；只有 ref 數 **≥ `full_percentile_min_refs`** 才套用 config 的 general/premium 百分位。理由：小樣本下 p70 幾乎由最高 ref 決定，median 是抗離群的中央統計。此為**程式強制的安全預設**——預設組態下根本無法在小樣本套激進百分位（machine-enforced fail-closed，非僅流程承諾）；校準若有 powered 證據證明小樣本可安全套高百分位，才把此值調低放開（見 §5）。
- **清單** = `premium_tiers`，每組 = `{percentile: int, keywords: list[str]}`，可任意多組、各設不同旋鈕與不同關鍵字。
- 關鍵字清單**完全在 config**，程式不寫死。
- 整段 `anchor_floor` 省略（或缺）→ floor **關閉**（向後相容；回滾 = 刪掉這段）。
- 百分位採 0–100 整數（`p60` 寫 `60`），與「百分位」語意一致；不與 `recency_weight`（0–1 weight）強求格式一致，因兩者語意不同。
- 起始值 `general=60 / premium=70 / min_refs=3` 取自 sweep 與稀疏守門考量，上線前再校準（見 §5）。

### §2 Floor 演算法

對每筆估價（query 字串 + 它的同類 ref 價格清單）：

1. **取數基準**：同類 refs = 送進模型的 **top_k context 中 `item_type ∈ classify_query(query)` 且 `price_jpy > 0`** 的 ref 價格清單。這些是模型實際被 grounding 的同類比價。
   - **`その他` query 一律 no-op（明確契約）**：`classify_query` 對 `その他` 回傳 **`[]`**（[typing.py:94](../../../estimator_king/sync/typing.py)），故 `type_set` 為空 → 同類集合必為空 → 經 1b 直接 no-op。**`その他` 永不套 floor**，維持模型原估價（最保守，符合 `その他` 是噪音桶）。不為 `その他` 另立「OTHER 桶」或改 retrieval 契約去撈 `その他` refs（避免在最噪音的桶上加 guardrail）。
1b. **稀疏守門（必要前置）**：若同類 ref 數 `< cfg.min_refs`（含空清單）→ floor **no-op**，直接信任模型估價。理由：樣本太少時百分位不可靠，單一貴 ref（first-run / 罕見 type / `その他` 噪音）會被當權威把 suggested 硬抬、再圍它撐寬 range，把 retrieval 噪音變成不可逆上錨。
   - **不宣稱百分位本身免疫離群**：須明確認知在**小樣本邊界**（例如 `n == 3`）下，p70 仍幾乎由最高那筆 ref 決定，`min_refs` + 百分位**並不**保證避免單 ref 過錨。因此 `min_refs` 的安全值（與各百分位在小樣本下是否安全）**由分層校準實證決定，不由本 spec 斷言**（見 §5：校準須按同類 ref 數分桶回報，floor 僅在小樣本桶不退步時才上線）。
   - **小樣本安全夾（machine-enforced、預設開）**：見步驟 2.5——同類 ref 數 `< full_percentile_min_refs` 時 `effective_pct` 自動夾為 ≤ 50（median）。這是**runtime 不變式**，不靠流程紀律：預設組態下小樣本永遠拿不到 p70。校準有 powered 證據才把 `full_percentile_min_refs` 調低放開（§5）。
2. **決定有效百分位**（同時 resolve「多組命中」）：
   ```
   effective_pct = max( general_percentile,
                        { tier.percentile | tier ∈ premium_tiers
                          且 ∃ kw ∈ tier.keywords 使 kw 為 query 子字串 } )
   ```
   - 未命中任何溢價組 → `general_percentile`。
   - 命中一組或多組 → 取「general 與所有命中組」中**最高**的百分位。
   - 此規則順序無關、多重命中自動取最猛（符合「優先殺低估」），且保證命中溢價的品項 floor 永遠 ≥ 一般品 floor（即使某溢價組誤設低於 general 也不會反而更鬆）。
2.5. **小樣本安全夾（machine-enforced）**：若同類 ref 數 `< cfg.full_percentile_min_refs`，令 `effective_pct = min(effective_pct, 50)`。即 ref 數在 `[min_refs, full_percentile_min_refs)` 的小樣本一律退回 median（抗離群）；≥ `full_percentile_min_refs` 才用 step 2 的完整百分位。此夾為 runtime 不變式，預設組態（5）下小樣本拿不到 p60/p70。
3. **上錨 suggested**：`floor_value = _percentile(同類refs, effective_pct)`；`new_suggested = max(suggested, round(floor_value))`。**只會抬高、永不壓低。**
4. **floor 抬高時一併重算 range（避免上緣無 headroom、覆蓋率退步）**：若 `new_suggested > 原 suggested`，依該筆 `confidence` 對應的上偏帶（high −20%/+30%、medium −25%/+45%、low −30%/+60%，與 prompt `<range_and_confidence>` 一致）圍繞 `new_suggested` 重建區間，且與原區間取較寬者以不縮減覆蓋：
   - `new_min = min(原 min, round(new_suggested × (1 − down)))`
   - `new_max = max(原 max, round(new_suggested × (1 + up)))`
   floor 未生效（`new_suggested == 原 suggested`）→ range 不動。如此抬高後 suggested 不會貼在區間上緣，保留上方 headroom，覆蓋率不因上錨而退步。
5. **不就地修改**：以 `model_copy(update={...})` 產生新物件（更新 `suggested_price_jpy`，floor 生效時一併更新 `price_range_jpy`）。最終既有的 `_snap_estimate` 仍負責格點正規化與 `min ≤ suggested ≤ max` 收尾。
6. **confidence / rationale 不改寫**：floor 是 deterministic 上錨，`confidence` 僅用於選 range 上偏帶；rationale 文字維持模型原輸出（可能微幅落後於上錨後的 suggested）——屬接受的次要殘差（見 §4），不在本輪重寫。
7. **哨兵保留**：`suggested_price_jpy == 0`（`_reconcile` 的 no-estimate 哨兵）→ floor **no-op**，不破壞哨兵語意。
8. **cfg 為 None**（未設定 / 停用）→ floor **no-op**。

關鍵字判定：對 query 與設定關鍵字**兩側都先做 NFKC 正規化再 casefold**（`unicodedata.normalize("NFKC", s).casefold()`），再做子字串包含。理由：生字串 `keyword in query` 依賴輸入恰好用設定的全形/半形/拼寫形式，等效寬度（全形/半形）、大小寫、相容字元變體會靜默退回 general percentile——正是溢價層要修的失敗模式（對抗審查 finding）。NFKC 統一全/半形與相容字元、casefold 處理大小寫，使常見變體仍命中。此為**專供 tier 比對的輕量正規化**，與 `normalize_text`（服務商品名去重、行為不同）分開定義，避免互相牽動。空白/特殊拼寫差異仍可能漏判 → 由 config 關鍵字清單涵蓋（必要時加別名），並由 §4 變體測試把關。

`_percentile(values, pct)`：純函式，線性內插（與 [experiment_anchor_floor.py](../../../scripts/analysis/experiment_anchor_floor.py) 的 `percentile` 同定義）。`pct` 為 0–100，內部轉 0–1。空清單回傳 `None`；單元素回傳該值。

### §3 程式整合與資料流（[estimator.py](../../../estimator_king/bot/estimator.py)）

**(a) `_estimate_chunk` 多回傳同類 ref 價格**
回傳型別由 `EstimateBatch` 改為 `tuple[EstimateBatch, dict[str, list[int]]]`：第二個是 `{normalize_text(query) → 同類 ref 價格 list}`。`ranked`（top_k hits）與 `types`（`classify_query` 結果）在現有迴圈中已在 scope，僅多收集一份同類價，**不增加任何向量查詢或 classify 呼叫**。

**(b) 新增純函式 `_anchor_floor`**（模組層級，與 `snap_to_tax_grid` 同性質、可單測）
```
_anchor_floor(query: str, est: ProductEstimate, same_type_prices: list[int],
              cfg: AnchorFloorConfig | None) -> ProductEstimate
```
行為即 §2。**`query` 為使用者原始輸入行**（不是 `est.product_name`）——溢價關鍵字比對與同類價查找都必須以原始 query 為鍵，否則模型若改寫/縮寫回傳名稱，關鍵字會漏判、p70 靜默退回 general，正中本輪要修的溢價品（對抗審查 finding）。

**(c) `estimate_products` 串接**（順序為正確性關鍵）

`_reconcile` 逐 `product_names` 行輸出、回傳與 `product_names` **1:1 同序**對齊，故以 `zip` 取回原始 query，關鍵字比對與同類價查找皆以原始 query 為鍵（不用 `est.product_name`）：
```
reconciled = self._reconcile(product_names, all_estimates)              # 既有，與 product_names 同序
reconciled = [_anchor_floor(line, e,
                            prices_by_name.get(normalize_text(line), []),
                            self._anchor_floor)
              for line, e in zip(product_names, reconciled)]            # 新增：先上錨（以原始 query 為鍵）
reconciled = [_snap_estimate(e) for e in reconciled]                    # 既有：再格點收尾
```
`prices_by_name: dict[str, list[int]]` 由各 chunk 的 `_estimate_chunk` 第二回傳值累積合併，鍵為 `normalize_text(原始 query)`（`_estimate_chunk` 本就以 `product_names` 的原始 name 迭代，line 201），同名以先到為準，與 `_reconcile` 的 `setdefault` 去重語意一致。

**(d) `Estimator.__init__` 收 config**：新增 keyword-only 參數 `anchor_floor: AnchorFloorConfig | None = None`，存為 `self._anchor_floor`。預設 None = 停用，**既有不傳此參數的呼叫端與測試一律維持 floor 關閉、零影響**。[runner.py](../../../estimator_king/bot/runner.py) 建 `Estimator` 時傳 `config.estimator_anchor_floor`。

**(e) [config_schema.py](../../../estimator_king/config_schema.py)**：
- 新增 `@dataclass AnchorTier(percentile: int, keywords: list[str])`、`@dataclass AnchorFloorConfig(general_percentile: int, min_refs: int, full_percentile_min_refs: int, premium_tiers: list[AnchorTier])`。
- `AppConfig` 新增欄位 `estimator_anchor_floor: AnchorFloorConfig | None`。
- `from_yaml` 解析 `estimator.anchor_floor`：缺 → `None`；存在 → 建 `AnchorFloorConfig`（`min_refs` 缺省 3、`full_percentile_min_refs` 缺省 5、`premium_tiers` 缺省為空 list）。
- `config.validate()`（結構驗證）：若 `estimator_anchor_floor` 非 None，檢查 `general_percentile` 與每個 tier 的 `percentile` 為 0–100 整數、`min_refs` 為 ≥ 1 的整數、**`full_percentile_min_refs` 為 ≥ `min_refs` 的整數**（強制小樣本安全夾的有效性）、每個 tier 的 `keywords` 為非空的非空字串清單；違反則拋與既有結構驗證一致的錯誤。

**(f) 可稽核性（audit log）**：floor 實際生效（`new_suggested > 原 suggested`）時，發一條 `logger.info` 記錄 `query`、原 suggested → floored suggested、`effective_pct`、`floor_value`、同類 ref 數，沿用本專案既有「以 log 做 runtime 歸因」的模式（對照 prompt_hash 日誌）。如此 runtime log 可分辨「模型估價」與「floor 調整過的估價」，下游/事後除錯可稽核哪些估價被後處理上錨、幅度多少。不改 `ProductEstimate` schema（避免動到 chat schema 與 bot 輸出），可稽核性由 log 提供。

### §4 測試與邊界

**單元測試**（deterministic，補入 [tests/test_estimator.py](../../../tests/test_estimator.py)）：
- `_percentile`：已知序列線性內插（如 `[100,200,300,400]` 的 p75）、單元素、空清單回 `None`。
- `_anchor_floor`：
  - 無同類價 → 不變；
  - `floor < suggested` → 不變（含 range 不動）；
  - `floor > suggested` → suggested 抬到 floor；
  - 命中溢價組 → 用較高 tier percentile；
  - 多組命中 → 取 max；
  - 只命中 general（無溢價）→ 用 general；
  - **溢價關鍵字以 `query` 參數判定、非 `est.product_name`**：給一筆 `query` 含溢價關鍵字、但 `est.product_name` 為不含關鍵字的改寫名，驗證仍套用 premium tier（防 finding 1 回歸）；
  - **floor 抬高時 range 重算**：`new_suggested > 原 suggested` 時，依 confidence 帶確保 `max ≥ round(new_suggested × (1+up))`（上方仍有 headroom、不貼上緣）、`min ≤ new_suggested`，且與原區間取較寬者（覆蓋不縮）；
  - **稀疏守門**：同類 ref 數 `< min_refs`（含單一 ref、空清單）→ floor **no-op**（防 finding 回歸）；恰 `== min_refs` → 套用；
  - **`その他` no-op**：`classify_query` 回 `[]`（同類集合空）→ floor no-op，維持原估價（鎖死契約、防實作 drift）；
  - **小樣本安全夾**：ref 數在 `[min_refs, full_percentile_min_refs)` 時，即使命中 premium p70 或 general p60，`effective_pct` 仍被夾為 ≤ 50（用 median 算 floor）；ref 數 ≥ `full_percentile_min_refs` 才套完整 p60/p70（防 high finding 回歸，鎖死 runtime 不變式）；
  - **關鍵字變體比對（NFKC + casefold）**：給溢價關鍵字的全形/半形變體（如半形 `ﾓｺﾓｺ` vs 全形 `もこもこ` 對應、含拉丁字大小寫 `Big`/`BIG`）的 query，驗證仍命中 premium tier；無關字串不誤命中（防 finding 2 回歸）；
  - **audit log**：floor 生效時發 `logger.info` 含 query / 原→floored / effective_pct / floor_value / ref 數（可用 caplog 斷言生效時有記、no-op 時無記）；
  - 哨兵 `suggested == 0` → 不變；
  - `cfg is None` → 不變；
  - 回傳為新物件、原物件未就地修改；
  - 與 `_snap_estimate` 串接後仍保 `min ≤ suggested ≤ max` 且落 ¥110 格點。
- config 解析/驗證：合法區塊解析為 `AnchorFloorConfig`（含 `min_refs`、`full_percentile_min_refs`，缺省 3/5）；缺區塊 → `None`（停用）；`percentile` 越界（<0 或 >100）→ validate 報錯；`min_refs < 1` → validate 報錯；`full_percentile_min_refs < min_refs` → validate 報錯；空 `keywords` → validate 報錯。
- **既有測試維持綠燈**：不傳 `anchor_floor` 的 `Estimator` floor 停用；`_estimate_chunk` 回傳型別改為 tuple，需更新既有直接呼叫 `_estimate_chunk` 的測試解包（若有）——除解包外行為不變。

**邊界 / 已知殘差**：
- query 落 `その他`（`classify_query == []`）→ 同類集合恆空 → floor **一律 no-op**，維持模型原估價（見 §2 step 1）。`その他` 不在 floor 範圍，亦不另立 OTHER 桶；屬明確契約而非殘差。
- **結構性低估不在本輪解決範圍**：`ポーチ`/`ピンバッジ`/`SKNB` 等「排除本尊後真價高於所有保留同類 refs」的案例，任何 ref-based floor 都搆不到（floor 上限 = 同類 refs 的百分位，仍低於真價）。明確列為已知限制，不在本輪追求消除。
- 關鍵字子字串可能誤命中（罕見）→ 由 config 清單控制，接受。
- **rationale 文字微幅落後**：floor 抬高 suggested 後，`confidence` 與 `rationale` 文字未隨之改寫（仍描述上錨前的估價）。`confidence` 只被用來選 range 上偏帶不致誤導；rationale 為模型 prose，deterministic 改寫成本高且非本輪目標——列為接受殘差。可稽核性由 §3(f) audit log 提供（runtime 可分辨哪些估價被上錨、幅度多少），range 已由 §2 step 4 重算以維持覆蓋率（硬驗收條件），故此殘差僅影響 rationale 用語、不影響數值正確性與可稽核性。

**eval 對齊（有效性關鍵）**：[eval_estimate.py](../../../scripts/analysis/eval_estimate.py) 的 `build_context` 比照 [experiment_anchor_floor.py](../../../scripts/analysis/experiment_anchor_floor.py) 多收集同類 ref 價格，並在 `run_once` 對每筆套用 `_anchor_floor`（用與 production 相同的 `config.estimator_anchor_floor`），否則 before/after 量到的不是上線行為。`experiment_anchor_floor.py` 改為：(1) 讀 config 的 `premium_tiers`/`min_refs`（取代寫死的 `PREMIUM_KW` 與單一百分位 sweep）；(2) **按同類 ref 數分桶回報**（如 `n=3–4 / 5–6 / 7+`），每桶輸出 §5 閘門所需欄位（bucket N、因 `min_refs` skip 數、floor 生效數、signed/MAPE、pass/fail），讓小樣本桶的過錨風險與**樣本是否足夠**可見、供 §5 fail-closed 閘門判定。作為校準台。

## §5 部署、回滾、校準、非目標

- **部署/回滾**：改動為 config 段 + `estimator.py`/`config_schema.py` 程式。floor 由 `anchor_floor` config 段控制：刪掉該段 + 重啟 bot 即停用。**不動 retrieval / embedding / vector ID / SQLite schema → 切換前後 chroma/SQLite 完全相容，回滾不需 `rm -rf chroma/` 或 `--force-refetch`**（對照 CLAUDE.md「Re-index on indexing-model change」僅適用索引層改動）。
- **校準（上線前必做）**：`general=60 / premium=70 / min_refs=3 / full_percentile_min_refs=5` 為**待驗起始點**（後三者預設即 fail-closed 安全）。以對齊後的 `experiment_anchor_floor.py`（讀 config 分層 + 兩道守門 + **按同類 ref 數分桶回報**）確認：分層 signed err、各守門對命中率/穩定度的影響、以及**小樣本桶是否在放開 median 夾後過錨**。再以 `eval_estimate.py --runs ≥ 3` 取 before/after，數據記入 PR / commit。
- **稀疏守門閘門（machine-enforced 預設 + 校準才放開）**：安全性由 **runtime 不變式**保證，不靠流程紀律——預設 `full_percentile_min_refs=5` 下，ref 數 `[min_refs, 5)` 的小樣本一律被夾到 median（§2 step 2.5），**根本無法套 p60/p70**。要把 p60/p70 放開到更小的樣本，**唯一途徑是調低 `full_percentile_min_refs`，而這必須有 powered 證據**：
  - **校準腳本每桶必出欄位**：bucket 範圍、bucket N、因 `min_refs` skip 數、floor 生效數、signed/MAPE、**該桶 pass/fail**。
  - **powered 桶定義**：該桶 **floor 生效數 ≥ `MIN_BUCKET_N`（取 5）** 才算有證據。
  - **放開規則**：`full_percentile_min_refs` 只能調低到「該值以上的每個小樣本桶都 powered 且不退步」為止；任一欲放開的桶空/欠樣 → 不得調低（維持安全夾）。即「無證據 → 維持 median 夾」，**預設組態天生 fail-closed，無需人為記得**。
  - `min_refs` 太高會讓 floor 鮮少生效（top_k=10 下許多 query 同類 ref 不多）、太低則放行噪音；`full_percentile_min_refs` 控制「多大樣本才信任高百分位」。兩者最終值由分桶證據定。
- **相對驗收準則**（延續第二輪精神，25 筆手標樣本上設絕對門檻無意義）：candidate 相對 baseline 同時滿足——
  - **mean signed err 絕對值實質下降**（本輪首要目標；預期由 ≈ −10% 收斂到 |signed| ≤ 約 3%）；
  - **MAPE 不顯著變差**（允許 ≤ baseline + 2pp 的抽樣雜訊；超過則回頭調百分位）；
  - range 覆蓋率不變差（≥ baseline）；
  - no-estimate 集合為 baseline 之子集（floor 不產生新的 no-estimate——floor 只抬高正估價、對哨兵 no-op，理論上恆成立，仍須由 eval 確認）；
  - baseline 與 candidate 兩份 run 皆 VALID。
- **非目標**：不動 prompt（floor 是把 prompt 失效的軟規則硬化，prompt 留著）；不動 retrieval / rerank / embedding；不做「floor 抬很多時自動降 confidence」（YAGNI，後續迭代再議）；不做 per-store override（單一全域 `anchor_floor`）；不追求消除上述結構性低估殘差。

## 文件同步（強制）

更新 [docs/data-pipeline.md](../../../docs/data-pipeline.md)：於 reconcile → snap 之間新增 `_anchor_floor` 階段，記錄其機制（以**原始 query** 為鍵、關鍵字以 NFKC+casefold 正規化兩側比對、`min_refs` 稀疏守門、同類 top_k refs 取百分位、分層 effective_pct = max(general, 命中組)、只抬不壓、抬高時依 confidence 帶重算 range 以維持上偏與覆蓋率、floor 生效發 audit log、哨兵/空集/cfg-None no-op）、控制設定 key（`estimator.anchor_floor`）、對應函式位置（`file:line`）、與設計理由（prompt 對 mini 失效 → deterministic 硬化；分層因一般/溢價兩組最佳百分位相差約 10pp）。

## 驗證指令

- Type check：`.venv/bin/basedpyright estimator_king/bot/estimator.py estimator_king/config_schema.py scripts/analysis/eval_estimate.py scripts/analysis/experiment_anchor_floor.py`
- Lint：`uvx ruff check estimator_king/bot/estimator.py estimator_king/config_schema.py scripts/analysis/eval_estimate.py scripts/analysis/experiment_anchor_floor.py tests/test_estimator.py`
- 測試：`.venv/bin/python -m pytest tests/test_estimator.py -v -o addopts=""`
- 效果驗證（before/after）：`set -a; source .env; set +a; PYTHONPATH=. .venv/bin/python scripts/analysis/eval_estimate.py --runs 3`

## 附錄：本輪採用的起始設定

| 旋鈕 | 值 | 依據 |
| --- | --- | --- |
| `general_percentile` | 60 | sweep：一般品 signed −0.5%（最接近 0） |
| 溢價 tier `percentile` | 70 | sweep：溢價品 signed +0.1%（最接近 0；p75 過矯到 +1.9%） |
| `min_refs` | 3 | 稀疏守門下限起點；< 此 → no-op |
| `full_percentile_min_refs` | 5 | 小樣本安全夾預設；`[3,5)` 退 median，machine-enforced fail-closed |
| 溢價 `keywords` | `温感, もこもこ, あったか, なりきり` | quick verify 的溢價品定義；對應 prompt `<anchoring>` 溢價關鍵字 |
