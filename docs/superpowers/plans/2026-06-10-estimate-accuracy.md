# 估價準確率優化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提升 `/estimate` 估價準確率：加入 deterministic ¥110 含稅格點正規化，並重寫估價 system prompt（修正向下/中央錨定偏誤、消除指令矛盾、校準信心與區間，並針對 gpt-5.4-mini 優化）。

**Architecture:** 全部改動集中在 [estimator_king/bot/estimator.py](../../../estimator_king/bot/estimator.py)。新增兩個模組層級純函式 `snap_to_tax_grid`（價格 round 到最近 ¥110 倍數）與 `_snap_estimate`（對單筆估價套用 snap 並保證 `min ≤ suggested ≤ max`），在 `Estimator.estimate_products` 的 reconcile 之後套用；同時重寫 `SYSTEM_PROMPT`。retrieval / rerank / chat provider / 資料結構不動。

**Tech Stack:** Python 3、pydantic v2（`ProductEstimate.model_copy`）、pytest、basedpyright、ruff。

---

## 驗證工具（每個 Task 結束都要跑）

- Type check：`.venv/bin/basedpyright estimator_king/bot/estimator.py`
- Lint：`uvx ruff check estimator_king/bot/estimator.py tests/test_estimator.py`
- 單檔測試：`.venv/bin/python -m pytest tests/test_estimator.py -v -o addopts=""`

`pytest.ini` 設了 `addopts = --cov=...`，跑單檔一律加 `-o addopts=""`（不要用 `-p no:cov`）。

---

## File Structure

| 檔案 | 責任 | 動作 |
| --- | --- | --- |
| `estimator_king/bot/estimator.py` | 估價核心：snap 函式 + prompt + 套用 | Modify |
| `tests/test_estimator.py` | 估價單元測試 | Modify（新增測試） |
| `docs/data-pipeline.md` | 端到端資料流參考 | Modify（同步 chat-estimate / reconcile 階段） |

---

## Task 1: `snap_to_tax_grid` 純函式

**Files:**
- Modify: `estimator_king/bot/estimator.py`（在 `SYSTEM_PROMPT` 定義之後、`_Embedder` Protocol 之前，新增常數與函式）
- Test: `tests/test_estimator.py`

- [ ] **Step 1: 更新測試檔 import**

把 `tests/test_estimator.py` 第 1 行：

```python
from estimator_king.bot.estimator import Estimator
```

改為（Task 1 只 import 本 Task 定義的 `snap_to_tax_grid`；`_snap_estimate` 留到 Task 2 才加入）：

```python
from estimator_king.bot.estimator import Estimator, snap_to_tax_grid
```

- [ ] **Step 2: 寫失敗測試**

在 `tests/test_estimator.py` 末尾新增：

```python
def test_snap_to_tax_grid_on_grid_unchanged():
    assert snap_to_tax_grid(6600) == 6600
    assert snap_to_tax_grid(1100) == 1100
    assert snap_to_tax_grid(3850) == 3850


def test_snap_to_tax_grid_rounds_up_when_remainder_at_least_55():
    assert snap_to_tax_grid(3800) == 3850  # remainder 60


def test_snap_to_tax_grid_rounds_down_when_remainder_below_55():
    assert snap_to_tax_grid(3000) == 2970  # remainder 30


def test_snap_to_tax_grid_tie_rounds_up():
    assert snap_to_tax_grid(55) == 110  # remainder 55


def test_snap_to_tax_grid_non_positive_returns_zero():
    assert snap_to_tax_grid(0) == 0
    assert snap_to_tax_grid(-50) == 0
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -k snap_to_tax_grid -v -o addopts=""`
Expected: 收集階段或執行 FAIL，因為 `snap_to_tax_grid` 尚未定義（ImportError）。

- [ ] **Step 4: 實作**

在 `estimator_king/bot/estimator.py` 的 `SYSTEM_PROMPT = (...)` 區塊之後、`class _Embedder(Protocol):` 之前，新增：

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

- [ ] **Step 5: 跑測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -k snap_to_tax_grid -v -o addopts=""`
Expected: 5 個 `snap_to_tax_grid` 測試全 PASS。

- [ ] **Step 6: Type check + Lint**

Run: `.venv/bin/basedpyright estimator_king/bot/estimator.py`
Expected: 0 errors。
Run: `uvx ruff check estimator_king/bot/estimator.py tests/test_estimator.py`
Expected: 無 lint 問題。

- [ ] **Step 7: Commit**

```bash
git add estimator_king/bot/estimator.py tests/test_estimator.py
git commit -m "feat(estimator): add tax-grid price snapping helper"
```

---

## Task 2: `_snap_estimate` 套用單筆估價

**Files:**
- Modify: `estimator_king/bot/estimator.py`（緊接 `snap_to_tax_grid` 之後新增）
- Test: `tests/test_estimator.py`

- [ ] **Step 1: 擴充測試檔 import**

把 `tests/test_estimator.py` 第 1 行（Task 1 已改為 `from estimator_king.bot.estimator import Estimator, snap_to_tax_grid`）擴充為：

```python
from estimator_king.bot.estimator import Estimator, snap_to_tax_grid, _snap_estimate
```

- [ ] **Step 2: 寫失敗測試**

在 `tests/test_estimator.py` 末尾新增：

```python
def test_snap_estimate_snaps_all_three_values():
    est = ProductEstimate(
        product_name="x", suggested_price_jpy=3800,
        price_range_jpy=PriceRange(min=3000, max=5000), confidence="high",
        rationale="r", reference_products=[])
    out = _snap_estimate(est)
    assert out.suggested_price_jpy == 3850
    assert out.price_range_jpy.min == 2970
    assert out.price_range_jpy.max == 4950
    assert out.suggested_price_jpy % 110 == 0
    assert out.price_range_jpy.min % 110 == 0
    assert out.price_range_jpy.max % 110 == 0


def test_snap_estimate_clamps_when_snapped_bounds_cross_suggested():
    # suggested 3800->3850; min 3960->3960 (> suggested); max 3700->3740 (< suggested)
    est = ProductEstimate(
        product_name="x", suggested_price_jpy=3800,
        price_range_jpy=PriceRange(min=3960, max=3700), confidence="medium",
        rationale="r", reference_products=[])
    out = _snap_estimate(est)
    assert out.price_range_jpy.min <= out.suggested_price_jpy <= out.price_range_jpy.max
    assert out.suggested_price_jpy == 3850
    assert out.price_range_jpy.min == 3850
    assert out.price_range_jpy.max == 3850


def test_snap_estimate_sentinel_stays_zero():
    est = ProductEstimate(
        product_name="x", suggested_price_jpy=0,
        price_range_jpy=PriceRange(min=0, max=0), confidence="low",
        rationale="r", reference_products=[])
    out = _snap_estimate(est)
    assert out.suggested_price_jpy == 0
    assert out.price_range_jpy.min == 0
    assert out.price_range_jpy.max == 0


def test_snap_estimate_does_not_mutate_input():
    est = ProductEstimate(
        product_name="x", suggested_price_jpy=3800,
        price_range_jpy=PriceRange(min=3000, max=5000), confidence="high",
        rationale="r", reference_products=[])
    _snap_estimate(est)
    assert est.suggested_price_jpy == 3800
    assert est.price_range_jpy.min == 3000
    assert est.price_range_jpy.max == 5000
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -k snap_estimate -v -o addopts=""`
Expected: FAIL（import 的 `_snap_estimate` 尚未定義，整個測試模組於 collection 階段 ImportError）。

- [ ] **Step 4: 實作**

在 `estimator_king/bot/estimator.py` 的 `snap_to_tax_grid` 函式之後新增：

```python
def _snap_estimate(est: ProductEstimate) -> ProductEstimate:
    """Snap an estimate's prices onto the ¥110 grid, keeping min <= suggested <= max."""
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

`ProductEstimate` 與 `PriceRange` 已於 `estimator.py` 第 11 行 `from estimator_king.llm.chat import EstimateBatch, PriceRange, ProductEstimate` import，無需新增 import。

- [ ] **Step 5: 跑測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -k snap_estimate -v -o addopts=""`
Expected: 4 個 `snap_estimate` 測試全 PASS。

- [ ] **Step 6: Type check + Lint**

Run: `.venv/bin/basedpyright estimator_king/bot/estimator.py`
Expected: 0 errors。
Run: `uvx ruff check estimator_king/bot/estimator.py tests/test_estimator.py`
Expected: 無 lint 問題。

- [ ] **Step 7: Commit**

```bash
git add estimator_king/bot/estimator.py tests/test_estimator.py
git commit -m "feat(estimator): snap estimate prices onto tax grid with ordering guard"
```

---

## Task 3: 在 `estimate_products` 套用 snap

**Files:**
- Modify: `estimator_king/bot/estimator.py`（`Estimator.estimate_products`，`reconciled = self._reconcile(...)` 之後）
- Test: `tests/test_estimator.py`

- [ ] **Step 1: 寫失敗測試**

在 `tests/test_estimator.py` 末尾新增：

```python
def test_estimate_products_snaps_output_to_grid():
    vs = RecordingVectorStore([_hit("a", "ぬいぐるみ", 500, 100, 0.1)])
    chat = FakeChat([ProductEstimate(
        product_name="もちもちぬいぐるみ", suggested_price_jpy=3800,
        price_range_jpy=PriceRange(min=3000, max=5000), confidence="high",
        rationale="r", reference_products=[])])
    est = _estimator(vs, chat, typing=FakeTypingProvider("ぬいぐるみ"))
    batch = est.estimate_products(["もちもちぬいぐるみ"], "u")
    out = batch.estimates[0]
    assert out.suggested_price_jpy == 3850
    assert out.suggested_price_jpy % 110 == 0
    assert out.price_range_jpy.min % 110 == 0
    assert out.price_range_jpy.max % 110 == 0
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_estimator.py::test_estimate_products_snaps_output_to_grid -v -o addopts=""`
Expected: FAIL，`suggested_price_jpy` 仍為未 snap 的 `3800`（`assert 3800 == 3850` 失敗）。

- [ ] **Step 3: 實作**

在 `estimator_king/bot/estimator.py` 的 `Estimator.estimate_products` 中，找到：

```python
        reconciled = self._reconcile(product_names, all_estimates)
```

在其後緊接一行：

```python
        reconciled = [_snap_estimate(est) for est in reconciled]
```

（即 snap 在 reconcile 之後、`logger.info(...)` 與 `return EstimateBatch(estimates=reconciled)` 之前。）

- [ ] **Step 4: 跑測試確認通過 + 全檔回歸**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -v -o addopts=""`
Expected: 新測試 PASS，且 `test_estimator.py` 全部既有測試維持 PASS。
（snap 只作用於估價輸出；既有測試對 reference context `price_jpy` 的斷言不受影響，reconciliation 哨兵 `suggested_price_jpy == 0` 經 snap 後仍為 0。）

- [ ] **Step 5: Type check + Lint**

Run: `.venv/bin/basedpyright estimator_king/bot/estimator.py`
Expected: 0 errors。
Run: `uvx ruff check estimator_king/bot/estimator.py tests/test_estimator.py`
Expected: 無 lint 問題。

- [ ] **Step 6: Commit**

```bash
git add estimator_king/bot/estimator.py tests/test_estimator.py
git commit -m "feat(estimator): apply tax-grid snapping to estimate output"
```

---

## Task 4: 重寫 `SYSTEM_PROMPT`

**Files:**
- Modify: `estimator_king/bot/estimator.py`（`SYSTEM_PROMPT` 整段替換）

此 Task 無新增單元測試（prompt 行為靠 review 與既有回歸測試把關，依 spec 不建 eval harness）。

- [ ] **Step 1: 替換 `SYSTEM_PROMPT`**

把 `estimator_king/bot/estimator.py` 現行的 `SYSTEM_PROMPT = (...)` 整個區塊（從 `SYSTEM_PROMPT = (` 到對應結尾的 `)`）替換為：

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

- [ ] **Step 2: 全檔回歸測試**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -v -o addopts=""`
Expected: 全部 PASS。`SYSTEM_PROMPT` 僅由 `FakeChat` 接收但不被斷言內容，故 prompt 內容變更不影響既有測試。

- [ ] **Step 3: Type check + Lint**

Run: `.venv/bin/basedpyright estimator_king/bot/estimator.py`
Expected: 0 errors。
Run: `uvx ruff check estimator_king/bot/estimator.py`
Expected: 無 lint 問題。

- [ ] **Step 4: Commit**

```bash
git add estimator_king/bot/estimator.py
git commit -m "feat(estimator): rewrite system prompt to fix anchoring bias and tune for gpt-5.4-mini"
```

---

## Task 5: 同步 `docs/data-pipeline.md`

**Files:**
- Modify: `docs/data-pipeline.md`（`## 階段 14:Chat 估價、對齊與輸出`，以及查詢端 ASCII 資料流區塊）

依 CLAUDE.md，data-pipeline 流程變更必須同 PR 更新此文件。chat-estimate 與 reconcile 並非兩個獨立的頂層階段，而是同一個 `## 階段 14:Chat 估價、對齊與輸出` 底下的編號子項（item 1 = 組 prompt / `SYSTEM_PROMPT`、item 3 = `_reconcile`、item 4 = `format_estimates`）。本 Task 需先讀取 `docs/data-pipeline.md` 取得實際行號，再按該檔既有格式（編號子項 + `[檔名:行號](相對路徑#L行號)` 引用 + 結尾「> **設計理由**」區塊）整合下列變更。`file:line` 引用一律用該檔慣例：短檔名標籤 + 行號 + `#L` 錨點（例如 `[estimator.py:101](../estimator_king/bot/estimator.py#L101)`），行號以實際檔案為準。

- [ ] **Step 1: 讀取並定位**

讀取 `docs/data-pipeline.md`，定位：(a) `## 階段 14` 的「**對應 function**」標頭；(b) item 1 描述 `SYSTEM_PROMPT` 要求的那段（「每行一筆估價、同序不漏…」）；(c) item 3 `_reconcile`、item 4 `format_estimates`；(d) 階段 14 結尾的「> **設計理由**」區塊（含「『近期價加權』是抗通膨主力」一句）；(e) 檔案開頭查詢端 ASCII 資料流區塊中 `_reconcile → format_estimates` 那段。

- [ ] **Step 2: 更新 item 1 的 prompt 行為描述**

將 item 1 描述 `SYSTEM_PROMPT` 要求的文字（現為「每行一筆估價、同序不漏、**只能**用提供的參考、優先同 `item_type`、近期價加權、依 `item_name` 與 detail 比對 size/材質、整數 JPY、最多 3 筆 `reference_products`、無強匹配仍給 `low` 估價而非捏造。」）改寫為：

> `SYSTEM_PROMPT`（[estimator.py:16](../estimator_king/bot/estimator.py#L16)）以 XML 區塊要求：每行一筆估價、同序不漏、**只能**用提供的參考（禁止引用參考以外的一般「相場」行情）、references 採嚴格優先序 **item_type > size/材質 > recency**（recency 僅作 tie-breaker、不得蓋過更接近的同類比對）、帶參考所無的溢價特徵/素材（温感、もこもこ／あったか、加大、なりきり等）時錨定同類參考**上端**、價格為含稅且必為 **¥110 整數倍**、price_range 約 **±25–30% 且偏上**、confidence `high` 需同名/同型近似 exact 且 suggested 落在同類參考價格跨度內、最多 3 筆 `reference_products`、無強匹配仍給 `low` 估價而非捏造。輸出欄位不在 prompt 重述，由 `response_format=EstimateBatch` schema 強制。

- [ ] **Step 3: 更新階段 14「設計理由」中已過時的 recency 敘述**

階段 14 結尾「> **設計理由**」區塊裡的「**prompt 與檢索設計呼應**」項現述「『近期價加權』是抗通膨主力(故 rerank recency_weight 可以很小)」，與新 prompt（recency 已降為僅 tie-breaker）矛盾。將該句改寫為：

> - **prompt 與檢索設計呼應**：「優先同 `item_type`」對應 per-type `where` 檢索；**recency 在 prompt 端已降為僅 tie-breaker**——同類比對接近度優先，rerank 的 `recency_weight` 因此維持很小，只做同等可比時的微調而非主導；「依 item_name/detail 比對 size/材質」正是 `_format_reference` 要輸出 `item_name`、`item_type`、`product_title`、價格、日期、store 與 snippet 這幾個欄位的原因。

- [ ] **Step 4: 新增 snap 子項（介於 `_reconcile` 與 `format_estimates` 之間）**

snap 在 `_reconcile` 之後、`format_estimates` 之前執行。在階段 14 的 item 3（`_reconcile`）與 item 4（`format_estimates`）之間插入新子項，並將原 `format_estimates` 子項順移編號：

> N. **含稅格點正規化（snap）**（[estimator.py:LINE](../estimator_king/bot/estimator.py#Lline)）：`_reconcile` 之後，對每筆估價的 `suggested_price_jpy` 與 `price_range_jpy{min,max}` 各自 round 到最近的 **¥110** 倍數（`snap_to_tax_grid` / `_snap_estimate`）；平手（餘 55）往上；非正數的「無估價」哨兵維持 `0`；snap 後強制 `min ≤ suggested ≤ max`。

（`LINE`/`#Lline` 以 `_snap_estimate` 在 `estimator.py` 的實際行號填入。）

同時更新階段 14 的「**對應 function**」標頭，於 `_reconcile` 之後、`format_estimates` 之前加入 `_snap_estimate`（[estimator.py:Lxxx]，行號以實際檔案為準）。

- [ ] **Step 5: 在「設計理由」區塊補上 snap 的理由**

於階段 14「> **設計理由**」區塊新增一項：

> - **為何 snap 到 ¥110 格點**：日本零售價皆為含稅價＝稅前(¥100 整數倍)×1.1，必為 ¥110 整數倍。觀測 12 筆實際定價 12/12 落在此格點、模型自然只 5/12；deterministic 後處理保證輸出落點正確，與 prompt `<price_format>` 形成雙保險。

- [ ] **Step 6: 更新查詢端 ASCII 資料流區塊**

在檔案開頭查詢端 ASCII 區塊中，`_reconcile(...)` 與 `format_estimates → Discord embeds` 之間插入一行 snap，使流程為 `… → _reconcile → snap_to_tax_grid（每筆價格 round 到最近 ¥110 倍數）→ format_estimates`（沿用既有框線與縮排風格）。

- [ ] **Step 7: Commit**

```bash
git add docs/data-pipeline.md
git commit -m "docs(data-pipeline): sync prompt rules and tax-grid snap step"
```

---

## 完成後整體驗證

- [ ] **全測試套件（含 coverage）**

Run: `.venv/bin/python -m pytest`
Expected: 全綠（含既有其他測試檔）。

- [ ] **生產碼 type 0-errors gate**

Run: `.venv/bin/basedpyright estimator_king/`
Expected: production code 0 errors（測試檔的 duck-typed fake 既有 noise 不計）。
