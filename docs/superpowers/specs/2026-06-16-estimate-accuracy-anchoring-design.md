# 估價準確率優化（第二輪）— 錨定／計數語意／信心校準 + eval 工具

日期：2026-06-16
範圍：`/estimate` 估價結果的第二輪準確率優化，全部集中在 prompt 層（`SYSTEM_PROMPT`），外加一支可量測 net 效果的 eval 腳本。延續並修正 [2026-06-10 第一輪規格](2026-06-10-estimate-accuracy-design.md)。

## 背景與證據

第一輪（2026-06-10）以 `snap_to_tax_grid` 徹底解決含稅格點問題（#1），並重寫 `SYSTEM_PROMPT`（matching 優先序、禁 DB 外行情、溢價錨上端、range/confidence）。但對一批 13 筆 **改版後** 的真實 `/estimate` 輸出與正式價格做比對，發現：

- MAPE ≈ 18.4%、平均帶符號誤差 ≈ −13.8% → **系統性低估未改善，甚至略增**。
- 13 筆估價 100% 落在 ¥110 格點（#1 已解）。
- 4/13 正式價格仍落在模型自報 range 之外，且其中 2 筆標 `high`。

### Retrieval 實測（關鍵）

對 4 個失誤品項用 **與 `Estimator._estimate_chunk` 完全相同的 retrieval 路徑**（embed → `classify_query` → N 個 type-filtered query + 1 個 plain query → 依 vector id 去重取最小 distance → `_rerank` → top_k）重跑，檢視 chat model 實際看到的 references：

| 品項 | 估/實 | 正確比價是否已在 refs | 判定 |
| --- | --- | --- | --- |
| ボイス1種 | 550 / 1100 | ✅ 同時撈到多筆 ¥1,000 單支ボイス **與** ¥500「全N種」廉價包 | 推理錯：被計數語意帶偏 |
| ポーチ | 2750 / 4400 | ✅ 連正解本尊「鷹嶺ルイ ポーチ ¥4,400」都撈到，refs 中位數 ≈¥3,400 | 錨定錯：估在自己 refs 中位數以下 |
| YB-2 RAP DOGサコッシュ | 2970 / 4950 | ⚠️ 同名只有基礎款 ¥2,970（聯名本尊未入庫），但有 ¥4,950 包款作上端訊號 | 結構性（本尊未入庫）+ 信心過高 |
| ピンバッジ2個セット | 1980 / 3300 | ✅ 撈到「ピンバッジ3種セット ¥3,300」（=正解價） | 推理錯：依計數內插 |

**結論：4 筆失誤中 3 筆的正確/上端比價本來就在 refs 裡，屬推理／錨定問題，而非 retrieval 缺漏。** 因此本輪修正集中在 prompt 推理層，不動 retrieval / rerank。

### 三個根因與對應修正

- **#A 基準錨定偏低**：模型在正常情況把 suggested 估在同類 refs 中位數以下（ポーチ）。第一輪只加了「帶溢價特徵 → 錨上端」，沒處理一般情況的基準錨定。
- **#B 計數語意誤導**：名稱中的 種／個セット 數量被當成價格乘數。ボイス1種 被「全4種¥500」帶偏（誤以為種少＝便宜），ピンバッジ2個セット 被在「單品」與「3種セット」間內插（誤以為 2 < 3 ＝較便宜）。
- **#C 信心／區間樂觀**：同名但帶額外修飾詞（聯名／尺寸）或泛用單字名（refs 價差大）時仍標 `high`，且區間包不住實際值。

## 設計總覽

改動範圍主要集中在 [estimator.py](../../../estimator_king/bot/estimator.py) 的 `SYSTEM_PROMPT`，不動 retrieval、rerank、chat provider、資料結構、`snap_to_tax_grid` 的行為——外加一個 prompt-hash 啟動日誌欄位（見「部署與回滾」）與一支 eval 腳本。採「外科式編修現有 prompt」（保留 commit `2b72223` 已驗證的 XML 分節結構，只動三處，改動可歸因），不整段重寫，不做 deterministic 錨定後處理。

1. `SYSTEM_PROMPT`：`<premium_adjustment>` 升級為 `<anchoring>`（#A）、新增 `<set_and_count>`（#B）、重新校準 `<range_and_confidence>`（#C）。
2. `Estimator` 加一個 `SYSTEM_PROMPT` 短 hash 屬性並附加到既有起訖日誌（runtime 歸因，詳見「部署與回滾」）。
3. 新增 `scripts/analysis/eval_estimate.py` + 內嵌 25 筆標註 fixtures，**重現 `_estimate_chunk` retrieval 並套用本尊排除**，量測 MAPE／range 覆蓋率等指標。
4. 同步 [docs/data-pipeline.md](../../../docs/data-pipeline.md)。

## 部署與回滾（rollout & rollback）

本專案為單實例 Discord bot，無多租戶 / canary / feature-flag 基建，亦不為單一 prompt 改動引入此類基建（YAGNI、且超出「prompt 層」範圍）。改動的安全性與可逆性由以下事實保證，需在 spec 與 commit 訊息明確記錄：

- **單點、git-tracked**：改動只是 `estimator.py` 內 `SYSTEM_PROMPT` 字串常數（外加 eval 腳本與文件）。回滾 = `git revert` 該 commit，無資料庫 migration、無設定變更。
- **不需 re-index**：本輪不動 retrieval、embedding 模型、vector ID 方案、embedding document 格式或 SQLite schema，因此切換前後 chroma / SQLite **完全相容**，回滾不需 `rm -rf chroma/` 或 `--force-refetch`（對照 CLAUDE.md「Re-index on indexing-model change」僅適用於索引層改動）。
- **生效範圍**：bot 重啟後新 prompt 立即套用到所有 `/estimate` 流量（`SYSTEM_PROMPT` 於 process 啟動時載入）；無灰度。因此**上線前必須先用 eval 腳本取得 before/after 數據並通過下述驗收準則**，數據記錄於 PR / commit 訊息；若上線後發現迴歸，以 `git revert` + 重啟即時回復。
- **runtime 歸因（prompt hash 日誌）**：`Estimator` 初始化時計算 `SYSTEM_PROMPT` 的短 hash（`hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:8]`）存為實例屬性，並**附加**到 `estimate_products` 既有的起訖 `logger.info` 訊息尾端（`prompt=<hash>`，附加而非改寫現有文字）。如此 runtime log 可把任一批估價結果**精確繫到產生它的 prompt 文字版本**（hash 對應 git 中該版 `SYSTEM_PROMPT`），讓事後診斷「這筆爛估價是哪版 prompt 產生的」有據可查。這是本輪唯一觸及 prompt 字串以外的 production 程式碼改動，限縮為計算 hash + 一個 log 欄位、不改估價行為與既有日誌的既有子字串。
- **明確排除（YAGNI，已記錄之設計決定）**：不引入「可在啟動時切換的多 prompt 選擇機制」或 feature-flag。單實例 bot 的回滾 = `git revert` + 重啟即足夠快且可逆；維護兩份並存 prompt 與切換設定的成本與複雜度，對單實例、無 SLA 的個人專案不成比例。commit 訊息 + 上述 hash 日誌共同構成版本邊界，不另設獨立 prompt 版本號欄位。

## 元件設計

### 元件 A：`SYSTEM_PROMPT` 三處改動（#A/#B/#C）

全部在 [estimator.py](../../../estimator_king/bot/estimator.py) 的 `SYSTEM_PROMPT` 字串常數內，保留現有以 XML tag 分節的結構與其他既有分節（`<role>`／`<goal>`／`<grounding_rules>`／`<matching_priority>`／`<price_format>`／`<output_rules>`）不變。

**(a) `<premium_adjustment>` → 升級為 `<anchoring>`（#A）**

移除原 `<premium_adjustment>` 區塊，於同位置（`<matching_priority>` 之後）改為 `<anchoring>`，把基準錨定列為第一條、原溢價規則保留為第二條：

```
<anchoring>
Among the comparable same-type references, decide where to anchor the suggested price:
- Default: anchor at the MEDIAN-to-UPPER of the comparable references — do NOT anchor below their median unless the queried line names a clearly simpler or cheaper variant (smaller size, plain/no special material, fewer components). Real prices tend to exceed conservative midpoints, so a below-median guess is rarely correct.
- Premium signal: if the queried line names a premium feature or material the references lack (heated/温感, fluffy/もこもこ・あったか, oversized, character cosplay/なりきり, special material), anchor at the UPPER end instead of the median.
</anchoring>
```

設計理由：直接修 ポーチ 類「估在自己 refs 中位數以下」。維持「median-to-upper」的**溫和**上偏（非「一律 upper」），因為低估佔多數（baseline 8/12、本輪 6/13 偏低），但溫和錨定不會把多數本就準的品項推過頭。

**(b) 新增 `<set_and_count>`（#B）**

緊接在 `<anchoring>` 之後新增：

```
<set_and_count>
A type or piece count in the name (1種, 2個セット, 全4種, etc.) is NOT a reliable price multiplier:
- Do NOT interpolate price by count — a 2-piece set is not necessarily cheaper than a 3-piece set; price on the same-type set references at the same single-vs-set tier, not on the exact number.
- A standalone single item (e.g. 1種) can cost as much as or MORE than a bundled multi-type set, because multi-type bundles are often discounted per unit. Do not assume "fewer types = cheaper".
- Treat the single-vs-set distinction and item_type as the signal; treat the specific count as a weak detail, not a price driver.
</set_and_count>
```

設計理由：修 ボイス1種（不再假設種少＝便宜，錨在 ¥1,000 單支帶）與 ピンバッジ2個セット（不依計數內插，錨在 set refs ¥2,500/¥3,300）。措辭刻意保持**中立**（「不是可靠乘數」「不要內插」），而非斷言某個方向，以免在僅兩筆樣本上過擬合。

**(c) 重新校準 `<range_and_confidence>`（#C）**

整段替換為：

```
<range_and_confidence>
- price_range must bracket realistic outcomes with an upward skew (more headroom above than below), because real prices tend to exceed conservative estimates:
  - high confidence: span roughly −20% to +30% around the suggested price.
  - medium confidence: span roughly −25% to +45%.
  - low confidence: span roughly −30% to +60%.
  Keep min ≤ suggested ≤ max.
- confidence:
  - high = a near-exact same-NAME, same-type reference exists AND the queried line carries no extra qualifier (collaboration/brand/series name, size, material, set count) the reference lacks AND the suggested price sits within the price span of same-type references (not extrapolated).
  - medium = same-type references exist but size/variant/feature/set-count differs, OR the name is a generic single word whose same-type references span a wide price range.
  - low = only cross-type or weak matches.
</range_and_confidence>
```

設計理由：(1) 區間依信心分級放寬，medium/low 上緣放更寬，覆蓋持續低估造成的 range miss；(2) 收緊 `high`——同名但查詢帶額外修飾詞（聯名／尺寸／set count）或泛用單字名 refs 價差大時降為 medium，直接修 サコッシュ／ポーチ 誤標 high。

`snap_to_tax_grid` / `_snap_estimate` 仍在 `estimate_products` 末端套用，與本輪改動正交，不需更動。

#### 新舊 prompt 行為對照

| 主題 | 第一輪（現行） | 本輪 |
| --- | --- | --- |
| 基準錨定 | 無（只有溢價特徵→上端） | median-to-upper 預設，不得低於中位（#A） |
| 計數語意 | 無 | 種／個セット 非價格乘數、不內插（#B） |
| 溢價特徵 | `<premium_adjustment>` 獨立區塊 | 併入 `<anchoring>` 第二條，行為不變 |
| range 寬度 | 固定 ±25–30% 偏上 | 依信心分級（high −20/+30、medium −25/+45、low −30/+60）（#C） |
| confidence high | 近似同名同類 + 落在同類跨度內 | 再加「無額外修飾詞」「泛用單字名價差大→降 medium」（#C） |

### 元件 B：`scripts/analysis/eval_estimate.py`（量測工具）

落點 `scripts/analysis/`，與既有 `calibrate_*.py`／`experiment_*.py` 同目錄、同風格、同跑法（皆「忠實重現 `Estimator._estimate_chunk`」、以 `set -a; source .env; set +a; PYTHONPATH=. .venv/bin/python ...` 執行）。

**標註資料**：module 層級 list 內嵌於腳本頂部（自足、零相依、易追加），seed 本規格附錄的 25 筆 `(query, official_jpy)`。

**行為契約**：

1. `AppConfig.from_yaml("stores_config.yaml")` → `build_providers(config, with_chat=True)` → 用與 [runner.py](../../../estimator_king/bot/runner.py) 建 `Estimator` 完全相同的參數（`item_types`、`item_types_version`、`estimator_top_k`、`estimator_recency_weight`、`estimator_diversity_weight`、`estimator_fetch_multiplier`）建立 `Estimator`。
2. **重現 `Estimator._estimate_chunk` 的 retrieval、但套用本尊排除**（理由見下方「本尊排除」——這是本 eval 的核心；故 **不直接呼叫 `estimate_products`**，因其 retrieval 為內部封裝、無法注入排除）。逐 query：`embed_query` → `classify_query` → N 個 type-filtered query + 1 個 plain query → 依 vector id 去重取最小 distance → **剔除本尊 hit** → `Estimator._rerank` → 取 top_k → `Estimator._format_reference` 組 context。與既有 `calibrate_*.py`「忠實重現 `_estimate_chunk` retrieval」同手法（可直接沿用其程式碼結構）。
3. 以與 `_estimate_chunk` 相同的 chunk 格式（每 10 query 一批、相同 user_prompt 組裝）呼叫 `self._chat.estimate(SYSTEM_PROMPT, user_prompt)`，對每筆結果套用 `_snap_estimate`（自 estimator 匯入），再逐筆對齊輸入計算：絕對誤差%、帶符號誤差%、是否落在 range（`min ≤ official ≤ max`）、confidence。

**本尊排除（self-exclusion，eval 有效性關鍵）**：這批 fixture 商品多已在後續 crawl 入庫，若不排除，retrieval 會把「本尊」當 sim≈1.0 的 top reference 撈回，模型照抄即近 100% 命中——量到的是「能否撈到自己」而非估價能力，違背 eval 目的（衡量「DB 無完全對應品」時的外推）。故組 context 前，對每個 query 剔除其本尊 hit：

- **判準**：`price_jpy == official_jpy`（必要條件，避免誤刪泛用名的合法比價，如 query「ポーチ」下多個不同商品同名「ポーチ」但價格不同）**且**（`normalize_text(item_name) == normalize_text(query)` 或 該 hit 相似度 ≥ 0.95）作為身分確認。`normalize_text` 重用 [crawler/snapshot.py](../../../estimator_king/crawler/snapshot.py)。
- 採 name+price 而非「同發售日+同名」：`published_at` 難對齊每筆 fixture 發售日；純同名會誤刪泛用比價。
- **可稽核**：eval 須對每個 fixture 印出「被剔除的本尊」（item_name｜price｜sim），供操作者肉眼確認本尊確實被找到並排除（名稱 formatting 差異可能漏抓，印出來才查得到）；某 fixture 若預期有本尊卻顯示未剔除，需調整判準後重跑。
- **已知殘差**：只排除「同名同價的本尊」，不排除「同檔期一起入庫的其他周邊（sibling）」；若同系列商品同價同名極近似亦可能被相似度條件帶到，屬保守誤刪（寧可少一筆比價、不要 self-leak），可接受。

**指標與輸出**：

1. 彙總指標（對齊第一輪規格用語）：**MAPE、中位數絕對誤差、平均帶符號誤差、range 覆蓋率%、完全命中率（|誤差| < 5%）**，外加 **no-estimate 計數與比率**（suggested == 0 哨兵的筆數 / 總筆數）作為一級指標。no-estimate 筆數排除於 MAPE 計算外，但其計數**必須**出現在 summary block（不得只藏在逐筆表），以免「靠製造更多 no-estimate 來壓低 MAPE」的迴歸被埋沒。
2. 輸出逐筆表（query／est／official／誤差%／in-range／confidence）+ summary block 至 stdout。summary 至少含：MAPE、中位數絕對誤差、平均帶符號誤差、range 覆蓋率%、完全命中率、**no-estimate 計數/比率**、有效樣本數、`--runs` 值。
3. `--runs N`（預設 3）：chat 為非決定性，對每筆跑 N 次、回報指標的平均；做上線決策的對比 run 必須 `--runs >= 3`。

**邊界與失敗處理（fail-closed）**：

- 某 query 在當前 DB 無 refs → 模型回 `low`／可能回 0 哨兵；eval 照常計入（顯示為大誤差或標記 no-estimate），不中斷整批。
- official_jpy 必為正整數；suggested 為 0 哨兵時，該筆以「no estimate」標記、排除於 MAPE 計算外但計入逐筆表，並累加進 no-estimate 計數。
- **批次完整性檢查**：所有 chunk 的 chat 估價結果，經對齊輸入後，有估價結果的 fixture 筆數 + no-estimate 筆數必須等於 fixture 總筆數（每筆 query 都要有對應輸出）；若不相等（chat 漏回或無法對齊某 query），該 run 標記為 **INVALID** 並以非零 exit code 結束，不輸出可用於決策的 summary（避免拿殘缺批次當依據）。
- **依賴降級即作廢**：任何 embedding/chat API timeout、rate-limit、或例外導致該次 run 無法取得全部估價時，run 標記 INVALID（fail-closed），不得以部分結果宣稱改善。
- 需 live chroma + embedding/chat API key（讀 `.env`）；dev-only、不進 CI、不寫 unit test（與其他 analysis 腳本一致）。

**用法與驗收準則（before/after 對比，寫入 docstring）**：改 prompt 前先以 `--runs >= 3` 跑一次 baseline（記下 MAPE / range 覆蓋率 / no-estimate 計數）→ 套用元件 A → 以相同 `--runs` 再跑。採**相對驗收**（25 筆手標樣本上設絕對門檻無意義）：新 prompt 視為通過，當且僅當相對 baseline 同時滿足

- MAPE 不變差（≤ baseline）、
- range 覆蓋率不變差（≥ baseline）、
- **no-estimate 為 per-fixture 子集**：after-run 的 no-estimate 集合（依 fixture query 識別）必須是 baseline no-estimate 集合的子集——**任何在 baseline 有實際估價、卻在 after-run 變成 ¥0 哨兵的 fixture，即判失敗**（不得以「另一筆 no-estimate 變回有估」互相抵銷；只看總計數會漏掉這種 per-class 估價流失）、
- 兩次 run 皆為 VALID（非 INVALID）。

為支援上述 per-fixture 子集判定，summary 之外，eval 須輸出每次 run 的 no-estimate fixture 清單（依 query），供 baseline 與 after-run 做集合比對。

任一條不滿足即不上線（回到元件 A 收斂或放棄該規則）。決策依據與 before/after 數據記錄於 PR / commit 訊息。

### 已知殘差（接受、由 eval 量化，不追求消除）

baseline 12 筆中有 3 筆為「過估」反例，上偏錨定／溢價規則會使其略增誤差：

- わためなりきりアイマスク：est 2400 / 實 2200（"なりきり" 溢價關鍵字，實際卻在地板價）。
- BANCHOジャージ：est 12100 / 實 9350（錨在同名最高 ref）。
- 王国アクリルジオラマスタンド：est 3800 / 實 3300。

設計取捨：對抗「系統性低估（多數）」必然以少數「本就在中位或偏低」品項的過度修正為代價。net 效果（MAPE／帶符號誤差是否下降）**以 eval 腳本量測判定，不以個案眼測**。若 eval 顯示 premium 規則拖累 net，於後續迭代再收斂——本輪不預先處理。

### サコッシュ 結構性限制（接受）

YB-2 RAP DOGサコッシュ 的聯名本尊未入庫，同類僅有基礎款 ¥2,970，本輪修正可讓它**不再誤標 high**（改為 medium、區間放寬），但 suggested 仍會偏低。徹底修正需「聯名小物上偏」關鍵字規則，已於 brainstorming 明確排除，故列為已知限制。

## 非目標（明確排除，避免 scope creep）

- 不動 retrieval / rerank（實測證明正確比價已在 refs，問題在推理層）。
- 不做聯名／限定關鍵字上偏規則。
- 不做 deterministic 錨定後處理（不改 `_estimate_chunk → _reconcile` 的資料流）。
- 不整段重寫 `SYSTEM_PROMPT`。
- eval 不進 CI、不改 chat 溫度／模型參數、不為 eval 腳本寫 unit test。
- 不追求消除上述已知殘差／結構性限制。

## 測試

- **既有 `tests/test_estimator.py` 維持綠燈**：snap／retrieval／rerank／reconcile／fetch_multiplier 等斷言皆不觸及 `SYSTEM_PROMPT` 文字（已查證 `tests/` 無任何斷言 `SYSTEM_PROMPT`／分節名），prompt 改動不影響既有測試。
- **不為 prompt 新增 unit test**：prompt 變更靠 eval 腳本 + review 驗證（沿用第一輪「prompt 變更靠 review，不建 eval harness 進 CI」的決定；本輪 eval 為手動 dev 工具）。
- **prompt hash 日誌不破壞既有 logging 測試**：`tests/test_estimator_logging.py` 以子字串（`in`）斷言 `estimate done for ...`／`N estimates`，hash 為**附加**欄位故既有斷言仍成立；可選擇性新增一條斷言 log 含 `prompt=` 前綴，但非必要。
- **不為 eval 腳本寫 unit test**：與 `scripts/analysis/calibrate_*.py` 慣例一致。

## 文件同步（強制）

更新 [docs/data-pipeline.md](../../../docs/data-pipeline.md) 的 chat-estimate 階段：補上 system prompt 新行為——`<anchoring>`（median-to-upper 基準錨定、溢價併入）、`<set_and_count>`（計數中立、不內插）、`<range_and_confidence>`（信心分級區間 + 收緊 high 判準），並更新對應「設計理由」。eval 腳本以自身 docstring 說明用法，不另動 runbook（與其他 analysis 腳本一致）。

## 驗證指令

- Type check：`.venv/bin/basedpyright estimator_king/bot/estimator.py scripts/analysis/eval_estimate.py`
- Lint：`uvx ruff check estimator_king/bot/estimator.py scripts/analysis/eval_estimate.py`
- 測試：`.venv/bin/python -m pytest tests/test_estimator.py -v -o addopts=""`
- 效果驗證：`set -a; source .env; set +a; PYTHONPATH=. .venv/bin/python scripts/analysis/eval_estimate.py`

## 附錄：eval fixtures（25 筆 query → 正式價格）

本輪 13 筆（改版後實測）：

| query | official_jpy |
| --- | --- |
| オーロラアクリルパネル | 3520 |
| ハート型缶バッジ | 660 |
| れきお〜推し活ショレダーバッグ | 5500 |
| おくるみすうぬいぐるみ | 4400 |
| ボイス1種 | 1100 |
| YB-2 RAP DOGパーカー | 11000 |
| YB-2 RAP DOGサコッシュ | 4950 |
| YB-2 RAP DOGキャップ | 3850 |
| これはYB-2しゃない　ころねのランダムラバーストラップ | 1100 |
| アクリルジオラマスタンド | 3850 |
| ピンバッジ2個セット | 3300 |
| ポーチ | 4400 |
| ぬいぐるみ　ダークローズ衣装ver. (H 250mm x W 180mm x D 120mm) | 5500 |

2026-06-10 baseline 12 筆：

| query | official_jpy |
| --- | --- |
| わためのあったかブランケット | 6600 |
| わため＆わためいと温感マグカップ | 3850 |
| わためいとクッション | 4950 |
| わためなりきりアイマスク | 2200 |
| ぶんぶんばんちょーアクリルスタンド | 1760 |
| BANCHOジャージ | 9350 |
| はじめとおそろいチョーカー | 4400 |
| ぬいぐるみキーホルダー　ブラックオーロラ衣装ver. | 3850 |
| 王国アクリルジオラマスタンド | 3300 |
| ランダムフブちゃんずラバーキーホルダー (H89xW63cm) | 1100 |
| もこもこフブちゃんカードホルダー (全4種) | 3520 |
| SKNB FACTORY配達鞄 | 6600 |
