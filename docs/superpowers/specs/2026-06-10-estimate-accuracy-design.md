# 估價準確率優化 — 設計規格

日期：2026-06-10
範圍：`/estimate` 估價結果的準確率優化（#1–#4），並針對 chat model `gpt-5.4-mini` 做 prompt 優化。

## 背景與問題

對一批 12 筆真實 `/estimate` 輸出與實際定價做量化比對，得到：

- MAPE 18.8%、中位數絕對誤差 15.9%
- 系統性低估：8/12 偏低，平均帶符號誤差 -9.9%
- **12/12 實際價格都是 ¥110 的整數倍**（含稅 = 稅前 ×1.1，稅前皆為 ¥100 整數倍），但目前只有 5/12 估價自然落在此格點
- 5 筆實際價格落在模型自報 range 之外（其中 4 筆標 `high`）→ 信心與區間樂觀

四個根因與對應修正：

- **#1 含稅格點未利用**：模型輸出 ¥3,000／¥2,400／¥1,200 等非真實格點的乾淨整數。
- **#2 中央錨定 + 引用 DB 外行情**：實際價＝最高 reference 時模型卻挑中位（ブランケット refs 含 6,600→估 5,500），rationale 出現「一般相場 5,000–6,600」這種 DB 外知識；帶溢價特徵的品項（温感、もこもこ）一律被低估。
- **#3 recency 蓋過同類比對**：ぶんぶんアクスタ 的 exact 同類 ¥1,760 被較新但較便宜的 ¥1,200 蓋掉。
- **#4 信心/區間校準樂觀**：`high` 信心卻 range 包不住實際值。

### gpt-5.4-mini 相關

依 OpenAI 公開的 GPT-5 prompting 指引，GPT-5 系列對「互相矛盾的指令」特別敏感（會耗 reasoning token 去調和衝突）。目前 `SYSTEM_PROMPT` 同時含「prefer references of the SAME item_type」與「weight more RECENT prices higher」兩條未分優先序的並列指令，正是這類潛在矛盾。因此 #3 同時是準確率修正與針對 gpt-5.4-mini 的 prompt 衛生修正。

## 設計總覽

改動全部集中在 [estimator.py](../../../estimator_king/bot/estimator.py)，不動 retrieval、rerank、chat provider、資料結構。

1. 重寫 `SYSTEM_PROMPT`（涵蓋 #2/#3/#4 + gpt-5.4-mini 優化）。
2. 新增純函式 `snap_to_tax_grid` 與套用邏輯 `_snap_estimate`（#1，deterministic 後處理）。
3. 在 `estimate_products` 的 `_reconcile` 之後套用 snap。
4. 補單元測試。
5. 同步 `docs/data-pipeline.md`。

## 元件設計

### 元件 A：`snap_to_tax_grid`（純函式，#1）

模組層級純函式，放在 [estimator.py](../../../estimator_king/bot/estimator.py)。

行為契約：

- 輸入整數 JPY 價格，回傳最近的 ¥110 整數倍。
- 平手（餘數恰為 55，即距上下格點相等）時**往上** round，與系統性低估的修正方向一致。
- 輸入 `<= 0` 時回傳 `0`（保留 `_reconcile` 用 `0` 表示「無估價」的哨兵值，不被 snap 破壞）。

實作：

```python
_TAX_GRID_JPY = 110


def snap_to_tax_grid(price: int) -> int:
    """Round a JPY price to the nearest ¥110 tax-inclusive grid point.

    Japanese retail prices are tax-included and are exact multiples of ¥110
    (pre-tax base x 1.1). Ties (remainder exactly 55) round up, matching the
    observed upward price drift. Non-positive input returns 0, preserving the
    "no estimate" sentinel produced by reconciliation.
    """
    if price <= 0:
        return 0
    quotient, remainder = divmod(price, _TAX_GRID_JPY)
    if remainder * 2 >= _TAX_GRID_JPY:
        quotient += 1
    return quotient * _TAX_GRID_JPY
```

### 元件 B：`_snap_estimate`（套用邏輯，#1）

模組層級純函式，對單筆 `ProductEstimate` 套用格點 snap，並保證排序不變。

行為契約：

- 對 `suggested_price_jpy`、`price_range_jpy.min`、`price_range_jpy.max` 各自呼叫 `snap_to_tax_grid`。
- snap 後強制 `min <= suggested <= max`：`min = min(snapped_min, snapped_suggested)`、`max = max(snapped_max, snapped_suggested)`，避免 snap 造成的邊界反序。
- 不就地修改；以 `model_copy(update=...)` 產生新物件。
- 哨兵值（全 0）經 snap 後仍為全 0。

實作：

```python
def _snap_estimate(est: ProductEstimate) -> ProductEstimate:
    suggested = snap_to_tax_grid(est.suggested_price_jpy)
    low = snap_to_tax_grid(est.price_range_jpy.min)
    high = snap_to_tax_grid(est.price_range_jpy.max)
    low = min(low, suggested)
    high = max(high, suggested)
    return est.model_copy(update={
        "suggested_price_jpy": suggested,
        "price_range_jpy": PriceRange(min=low, max=high),
    })
```

`PriceRange` 與 `ProductEstimate` 已在 [estimator.py](../../../estimator_king/bot/estimator.py) 頂部從 `estimator_king.llm.chat` import，無需新增 import。

### 元件 C：在 `estimate_products` 套用 snap（#1）

於 `Estimator.estimate_products` 內，`reconciled = self._reconcile(...)` 之後、回傳之前，對每筆套用 `_snap_estimate`：

```python
reconciled = self._reconcile(product_names, all_estimates)
reconciled = [_snap_estimate(est) for est in reconciled]
```

snap 在 reconcile 之後，確保補進來的哨兵與正常估價一致地通過格點正規化。

### 元件 D：重寫 `SYSTEM_PROMPT`（#2/#3/#4 + gpt-5.4-mini）

以 XML tag 為主分節（提升 mini 模型指令遵循度），核心規則前置、例外後置、術語一致，並**移除對輸出 JSON 欄位的冗餘重述**（`ChatProvider` 已用 `response_format=EstimateBatch` 強制 schema），只保留 confidence 的語意定義。所有 tie-breaker 改為顯式優先序，消除指令矛盾。

新 `SYSTEM_PROMPT` 完整文字：

```python
SYSTEM_PROMPT = (
    "<role>\n"
    "You are the Estimator King, a price estimator for Japanese hololive/vspo "
    "merchandise. You price exactly one item per input line, using only the "
    "reference items provided in the user message.\n"
    "</role>\n\n"
    "<goal>\n"
    "For each product line, output a JPY price estimate grounded in the reference "
    "items: a single suggested price, a plausible price range, a confidence level, "
    "a short rationale, and up to 3 of the references you actually used.\n"
    "</goal>\n\n"
    "<grounding_rules>\n"
    "- Use ONLY the provided reference context. Never invent prices or products not "
    "present in it.\n"
    "- Do NOT use outside market knowledge or general '相場' price ranges. If a "
    "rationale would cite a typical/general market price that is not taken from the "
    "references, that is a violation — do not use it.\n"
    "- Cite up to 3 reference_products you actually drew from the context.\n"
    "</grounding_rules>\n\n"
    "<matching_priority>\n"
    "Rank references in this strict order:\n"
    "1. item_type: references of the SAME item_type as the queried line dominate; "
    "cross-type references are only weak signal.\n"
    "2. size/material: among same-type references, prefer those whose item_name and "
    "detail line match the queried size and material.\n"
    "3. recency: use the published date ONLY to break ties among references that are "
    "otherwise equally comparable. A more recent but less-comparable reference must "
    "NOT override a closer same-type/size match.\n"
    "</matching_priority>\n\n"
    "<premium_adjustment>\n"
    "If the queried line names a premium feature or material that the comparable "
    "references do not have (for example heated/温感, fluffy/もこもこ・あったか, "
    "oversized, character cosplay/なりきり, special material), anchor to the UPPER "
    "end of the comparable references rather than their median — premium variants "
    "sell above standard ones.\n"
    "</premium_adjustment>\n\n"
    "<price_format>\n"
    "All Japanese retail prices are tax-included and are exact multiples of ¥110 "
    "(pre-tax base × 1.1). suggested_price and BOTH price_range bounds must be "
    "integer JPY and exact multiples of 110.\n"
    "</price_format>\n\n"
    "<range_and_confidence>\n"
    "- price_range should bracket realistic outcomes: span roughly ±25–30% around "
    "the suggested price, skewed upward (leave more headroom above than below), "
    "because real prices tend to exceed conservative estimates. Keep "
    "min ≤ suggested ≤ max.\n"
    "- confidence: high = a near-exact same-name/same-type reference exists AND the "
    "suggested price sits within the price span of same-type references (not "
    "extrapolated); medium = same-type references exist but size/variant/feature "
    "differs; low = only cross-type or weak matches.\n"
    "</range_and_confidence>\n\n"
    "<output_rules>\n"
    "- Produce exactly one estimate per input line, in the same order; none skipped, "
    "none merged.\n"
    "- If no strong match exists, still return an estimate with confidence \"low\" "
    "and a rationale stating the limitation — do NOT fabricate a closer match.\n"
    "</output_rules>"
)
```

新舊 prompt 行為對照：

| 主題 | 舊行為 | 新行為 |
| --- | --- | --- |
| recency vs item_type | 兩條並列、未分優先序（矛盾） | 顯式優先序，recency 僅 tie-breaker（#3） |
| DB 外行情 | 僅「never invent」 | 明確禁止引用一般「相場」（#2） |
| 溢價品項 | 無 | 帶溢價特徵 → 錨上端（#2） |
| 含稅格點 | 無 | 必為 ¥110 整數倍（#1，prompt 端雙保險） |
| range | 由模型自由給 | ±25–30%、偏上（#4） |
| confidence high | 「direct/near-exact same-type」 | 加上「suggested 須落在同類 references 價格跨度內、非外推」（#4） |
| 輸出欄位列舉 | 在「# Output」重述欄位名 | 移除（schema 已強制），只留 confidence 語意 |

非目標（明確不納入，避免 scope creep）：寫死的 confidence 數值計分公式、時間窗折舊規則、`reasoning_effort` 門檻建議、多角度 Step A/B/C 估價流程。

## 測試（單元測試，補入 [tests/test_estimator.py](../../../tests/test_estimator.py)）

針對 deterministic 部分撰寫，prompt 變更靠 review，不建 eval harness。

`snap_to_tax_grid` 測試：

- 已在格點上：`6600 -> 6600`、`1100 -> 1100`（不變）。
- 向下 round：`3850` 已是格點不變；以 `3800 -> 3850`（餘 60 ≥ 55 進位）、`3000 -> 2970`（餘 30 < 55 退位）驗證最近格點。
- 平手往上：`55 -> 110`（餘 55，進位）。
- 非正數哨兵：`0 -> 0`、`-50 -> 0`。

`_snap_estimate` 測試：

- 三值都 snap 到 ¥110 整數倍。
- snap 後保證 `min ≤ suggested ≤ max`：給一組 snap 後會反序的輸入（例如 suggested 進位、min 維持較高），驗證被夾回。
- 哨兵估價（全 0、confidence `low`）經 snap 後仍全 0。
- 回傳為新物件，原物件未被就地修改。

`estimate_products` 整合：

- 既有 type-filtered query / recency rerank / diversity / reconciliation / fetch_multiplier 等測試須維持綠燈（snap 套用後最終輸出價格皆為 ¥110 整數倍，必要時調整既有測試中對 `FakeChat` 回傳價的斷言為格點值）。

## 文件同步（強制）

更新 [docs/data-pipeline.md](../../../docs/data-pipeline.md)：

- chat-estimate 階段：更新 system prompt 行為描述（matching 優先序、禁用 DB 外行情、溢價錨上端、含稅格點、range/confidence 校準）。
- reconcile 階段之後：新增 deterministic `snap_to_tax_grid` 後處理步驟，記錄其機制（round 到最近 ¥110 倍數、平手往上、0 哨兵保留、保證 `min ≤ suggested ≤ max`）、對應函式位置與設計理由。

## 驗證指令

- Type check：`.venv/bin/basedpyright estimator_king/bot/estimator.py`
- Lint：`uvx ruff check estimator_king/bot/estimator.py tests/test_estimator.py`
- 測試：`.venv/bin/python -m pytest tests/test_estimator.py -v -o addopts=""`
