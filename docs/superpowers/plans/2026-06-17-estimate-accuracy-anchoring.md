# 估價準確率優化（第二輪）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正 `/estimate` 的系統性低估與計數語意誤判（prompt 層），並交付一支能量測 net 效果的 eval 腳本。

**Architecture:** 改動集中在 [estimator.py](../../../estimator_king/bot/estimator.py) 的 `SYSTEM_PROMPT`（三處外科式編修）＋一個 prompt-hash 啟動屬性附加到既有日誌；新增 `scripts/analysis/eval_estimate.py`（重現 `_estimate_chunk` retrieval、套用「本尊排除」、配對多-run 量測）；同步 [docs/data-pipeline.md](../../../docs/data-pipeline.md) 階段 14。不動 retrieval/rerank/chat provider/資料結構/`snap_to_tax_grid` 的行為。

**Tech Stack:** Python 3、basedpyright、ruff（uvx）、pytest、ChromaDB、OpenAI-相容 chat/embedding provider。

**Spec:** [docs/superpowers/specs/2026-06-16-estimate-accuracy-anchoring-design.md](../specs/2026-06-16-estimate-accuracy-anchoring-design.md)

**驗證指令（每個 code 任務收尾都要跑）：**
- Type check：`.venv/bin/basedpyright estimator_king/bot/estimator.py scripts/analysis/eval_estimate.py`
- Lint：`uvx ruff check estimator_king/bot/estimator.py scripts/analysis/eval_estimate.py tests/test_estimator_logging.py`
- 測試（單檔，覆寫 addopts）：`.venv/bin/python -m pytest tests/test_estimator.py tests/test_estimator_logging.py -v -o addopts=""`

> 註：type gate 是 **production code（`estimator_king/`）0 errors**；測試檔的 duck-typed fakes 既有 `reportArgumentType` 雜訊屬慣例、非回歸。

---

## File Structure

- **Modify** [estimator_king/bot/estimator.py](../../../estimator_king/bot/estimator.py)
  - `SYSTEM_PROMPT`：`<premium_adjustment>` → `<anchoring>`；新增 `<set_and_count>`；改寫 `<range_and_confidence>`（Task 1）。
  - `import hashlib`；`Estimator.__init__` 加 `self._prompt_hash`；`estimate_products` 兩條 `logger.info` 附加 `prompt=%s`（Task 2）。
- **Modify** [tests/test_estimator_logging.py](../../../tests/test_estimator_logging.py)：新增 `prompt=` 斷言（Task 2）。
- **Create** [scripts/analysis/eval_estimate.py](../../../scripts/analysis/eval_estimate.py)：eval 工具（Task 3）。
- **Modify** [docs/data-pipeline.md](../../../docs/data-pipeline.md) 階段 14：同步 prompt 行為（Task 4）。

---

## Task 1：改寫 `SYSTEM_PROMPT` 三處（#A/#B/#C）

**Files:**
- Modify: `estimator_king/bot/estimator.py:45-66`

無 unit test（prompt 文字無測試斷言；由 lint/type + Task 3 eval 驗證）。改動只替換字串常數內容。

- [ ] **Step 1：把 `<premium_adjustment>` 區塊替換為 `<anchoring>` + `<set_and_count>`**

在 [estimator.py:45-51](../../../estimator_king/bot/estimator.py#L45) 找到現有區塊：

```python
    "<premium_adjustment>\n"
    "If the queried line names a premium feature or material that the comparable "
    "references do not have (for example heated/温感, fluffy/もこもこ・あったか, "
    "oversized, character cosplay/なりきり, special material), anchor to the UPPER "
    "end of the comparable references rather than their median — premium variants "
    "sell above standard ones.\n"
    "</premium_adjustment>\n\n"
```

整段替換為：

```python
    "<anchoring>\n"
    "Among the comparable same-type references, decide where to anchor the "
    "suggested price:\n"
    "- Default: anchor at the MEDIAN-to-UPPER of the comparable references — do "
    "NOT anchor below their median unless the queried line names a clearly simpler "
    "or cheaper variant (smaller size, plain/no special material, fewer "
    "components). Real prices tend to exceed conservative midpoints, so a "
    "below-median guess is rarely correct.\n"
    "- Premium signal: if the queried line names a premium feature or material the "
    "references lack (heated/温感, fluffy/もこもこ・あったか, oversized, character "
    "cosplay/なりきり, special material), anchor at the UPPER end instead of the "
    "median.\n"
    "</anchoring>\n\n"
    "<set_and_count>\n"
    "A type or piece count in the name (1種, 2個セット, 全4種, etc.) is NOT a "
    "reliable price multiplier:\n"
    "- Do NOT interpolate price by count — a 2-piece set is not necessarily "
    "cheaper than a 3-piece set; price on the same-type set references at the same "
    "single-vs-set tier, not on the exact number.\n"
    "- A standalone single item (e.g. 1種) can cost as much as or MORE than a "
    "bundled multi-type set, because multi-type bundles are often discounted per "
    "unit. Do not assume \"fewer types = cheaper\".\n"
    "- Treat the single-vs-set distinction and item_type as the signal; treat the "
    "specific count as a weak detail, not a price driver.\n"
    "</set_and_count>\n\n"
```

- [ ] **Step 2：改寫 `<range_and_confidence>` 區塊**

在 [estimator.py:57-66](../../../estimator_king/bot/estimator.py#L57) 找到現有區塊：

```python
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
```

整段替換為：

```python
    "<range_and_confidence>\n"
    "- price_range must bracket realistic outcomes with an upward skew (more "
    "headroom above than below), because real prices tend to exceed conservative "
    "estimates:\n"
    "  - high confidence: span roughly -20% to +30% around the suggested price.\n"
    "  - medium confidence: span roughly -25% to +45%.\n"
    "  - low confidence: span roughly -30% to +60%.\n"
    "  Keep min ≤ suggested ≤ max.\n"
    "- confidence:\n"
    "  - high = a near-exact same-NAME, same-type reference exists AND the queried "
    "line carries no extra qualifier (collaboration/brand/series name, size, "
    "material, set count) the reference lacks AND the suggested price sits within "
    "the price span of same-type references (not extrapolated).\n"
    "  - medium = same-type references exist but size/variant/feature/set-count "
    "differs, OR the name is a generic single word whose same-type references span "
    "a wide price range.\n"
    "  - low = only cross-type or weak matches.\n"
    "</range_and_confidence>\n\n"
```

- [ ] **Step 3：Type check + Lint**

Run: `.venv/bin/basedpyright estimator_king/bot/estimator.py && uvx ruff check estimator_king/bot/estimator.py`
Expected: production code 0 errors；ruff `All checks passed!`。

- [ ] **Step 4：跑既有 estimator 測試確認未回歸**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -v -o addopts=""`
Expected: PASS（snap/retrieval/rerank/reconcile 等斷言不觸及 prompt 文字）。

- [ ] **Step 5：Commit**

```bash
git add estimator_king/bot/estimator.py
git commit -m "feat(estimator): anchor median-to-upper, add set/count rule, tier ranges"
```

---

## Task 2：`Estimator` prompt-hash 屬性 + 附加到既有日誌

**Files:**
- Modify: `estimator_king/bot/estimator.py`（import 區、`__init__`、`estimate_products`）
- Test: `tests/test_estimator_logging.py`

採 TDD：先加日誌斷言（失敗）→ 實作 → 通過。

- [ ] **Step 1：在 logging 測試新增 `prompt=` 斷言（失敗測試）**

修改 [tests/test_estimator_logging.py:51-53](../../../tests/test_estimator_logging.py#L51) 的最後一個 `assert`，由：

```python
    assert any(
        "estimate done for discord-1" in m and "2 estimates" in m for m in info_msgs
    )
```

改為：

```python
    assert any(
        "estimate done for discord-1" in m and "2 estimates" in m for m in info_msgs
    )
    assert any("prompt=" in m for m in info_msgs)
```

- [ ] **Step 2：跑測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_estimator_logging.py::test_chunk_debug_and_done_info -v -o addopts=""`
Expected: FAIL on `assert any("prompt=" in m ...)`（目前日誌沒有 `prompt=`）。

- [ ] **Step 3：加 `import hashlib`**

[estimator.py:4-6](../../../estimator_king/bot/estimator.py#L4) 現為：

```python
import logging
import time
from collections.abc import Sequence
```

改為（`hashlib` 置於最前，維持字母序）：

```python
import hashlib
import logging
import time
from collections.abc import Sequence
```

- [ ] **Step 4：在 `__init__` 末尾計算 prompt hash**

在 [estimator.py:148](../../../estimator_king/bot/estimator.py#L148) 的 `self._fetch_multiplier = fetch_multiplier` 之後加一行：

```python
        self._fetch_multiplier = fetch_multiplier
        self._prompt_hash = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:8]
```

- [ ] **Step 5：把 prompt hash 附加到 `estimate_products` 的兩條 `logger.info`**

在 [estimator.py:153](../../../estimator_king/bot/estimator.py#L153)，把起始日誌：

```python
        logger.info("estimate request from %s for %d products", user_id, len(product_names))
```

改為：

```python
        logger.info("estimate request from %s for %d products prompt=%s",
                    user_id, len(product_names), self._prompt_hash)
```

在 [estimator.py:165-166](../../../estimator_king/bot/estimator.py#L165)，把結束日誌：

```python
        logger.info("estimate done for %s: %d estimates in %.1fs",
                    user_id, len(reconciled), time.monotonic() - start)
```

改為：

```python
        logger.info("estimate done for %s: %d estimates in %.1fs prompt=%s",
                    user_id, len(reconciled), time.monotonic() - start, self._prompt_hash)
```

- [ ] **Step 6：跑測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_estimator_logging.py -v -o addopts=""`
Expected: PASS（含新 `prompt=` 斷言；既有 `estimate done`/`2 estimates` 子字串仍在）。

- [ ] **Step 7：Type check + Lint + 全 estimator 測試**

Run: `.venv/bin/basedpyright estimator_king/bot/estimator.py && uvx ruff check estimator_king/bot/estimator.py tests/test_estimator_logging.py && .venv/bin/python -m pytest tests/test_estimator.py tests/test_estimator_logging.py -o addopts=""`
Expected: 0 errors / All checks passed / PASS。

- [ ] **Step 8：Commit**

```bash
git add estimator_king/bot/estimator.py tests/test_estimator_logging.py
git commit -m "feat(estimator): log SYSTEM_PROMPT hash for runtime attribution"
```

---

## Task 3：新增 `scripts/analysis/eval_estimate.py`

**Files:**
- Create: `scripts/analysis/eval_estimate.py`

無 unit test（與 `scripts/analysis/calibrate_*.py` 慣例一致；需 live chroma + API）。驗證 = type check + lint + import smoke。腳本重現 `_estimate_chunk` retrieval、對每筆 query 剔除「本尊」、配對多-run 量測。檔頭以 `# pyright: reportPrivateUsage=false` 允許刻意重用 `Estimator` 內部（與 `calibrate_*.py` 重用內部檢索同精神）。

- [ ] **Step 1：寫入完整腳本**

建立 `scripts/analysis/eval_estimate.py`，內容如下（完整、可執行、無待填）：

```python
# pyright: reportPrivateUsage=false
"""eval: measure /estimate accuracy on 25 labeled fixtures, with self-exclusion.

The fixture products have since been ingested into the store DB, so a bare name
query would retrieve the exact "self" at sim~1.0 and the model would just copy
its price -> inflated, meaningless accuracy. This script reproduces
Estimator._estimate_chunk retrieval faithfully but DROPS each query's self
(price == official AND name/high-similarity match, similarity = 1.0 - distance)
before building the context, so it measures estimation skill when the DB has no
exact match -- the real-world scenario these fixtures came from.

Workflow (before/after, see the design spec): on the baseline commit run once;
apply the prompt change; on the candidate commit run again with the same
--runs N and same fixtures; compare per the spec acceptance criteria.

Run: set -a; source .env; set +a; PYTHONPATH=. .venv/bin/python \\
    scripts/analysis/eval_estimate.py --runs 3
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
from typing import Any

from estimator_king.bot.estimator import SYSTEM_PROMPT, Estimator, _snap_estimate
from estimator_king.config_schema import load_config
from estimator_king.crawler.snapshot import normalize_text
from estimator_king.llm.chat import EstimationError
from estimator_king.runtime import build_providers
from estimator_king.sync.typing import classify_query

# (query, official_jpy) -- seeded from the design spec appendix (25 fixtures).
FIXTURES: list[tuple[str, int]] = [
    # round-2 batch (post-fix queries)
    ("オーロラアクリルパネル", 3520),
    ("ハート型缶バッジ", 660),
    ("れきお〜推し活ショレダーバッグ", 5500),
    ("おくるみすうぬいぐるみ", 4400),
    ("ボイス1種", 1100),
    ("YB-2 RAP DOGパーカー", 11000),
    ("YB-2 RAP DOGサコッシュ", 4950),
    ("YB-2 RAP DOGキャップ", 3850),
    ("これはYB-2しゃない　ころねのランダムラバーストラップ", 1100),
    ("アクリルジオラマスタンド", 3850),
    ("ピンバッジ2個セット", 3300),
    ("ポーチ", 4400),
    ("ぬいぐるみ　ダークローズ衣装ver. (H 250mm x W 180mm x D 120mm)", 5500),
    # 2026-06-10 baseline batch
    ("わためのあったかブランケット", 6600),
    ("わため＆わためいと温感マグカップ", 3850),
    ("わためいとクッション", 4950),
    ("わためなりきりアイマスク", 2200),
    ("ぶんぶんばんちょーアクリルスタンド", 1760),
    ("BANCHOジャージ", 9350),
    ("はじめとおそろいチョーカー", 4400),
    ("ぬいぐるみキーホルダー　ブラックオーロラ衣装ver.", 3850),
    ("王国アクリルジオラマスタンド", 3300),
    ("ランダムフブちゃんずラバーキーホルダー (H89xW63cm)", 1100),
    ("もこもこフブちゃんカードホルダー (全4種)", 3520),
    ("SKNB FACTORY配達鞄", 6600),
]

SELF_SIM_THRESHOLD = 0.95
EXACT_HIT_PCT = 5.0
OFFICIAL_BY_Q: dict[str, int] = dict(FIXTURES)


def _git(args: list[str]) -> str:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception:
        return "unknown"


def build_context(est: Estimator, query: str, official: int) -> tuple[str, list[str]]:
    """Reproduce _estimate_chunk retrieval for one query, drop the self hits,
    return (context_block, excluded_self_descriptions)."""
    embedding = est._embedder.embed_query(query)
    types = classify_query(
        query, item_types=est._item_types,
        item_types_version=est._item_types_version,
        typing_provider=est._typing_provider, repository=None,
    )
    merged: dict[str, Any] = {}
    queries: list[dict[str, Any] | None] = [{"item_type": t} for t in types]
    queries.append(None)
    fetch_n = est._top_k * est._fetch_multiplier
    for where in queries:
        for hit in est._vector_store.query(embedding, fetch_n, where=where):
            prev = merged.get(hit.id)
            if prev is None or hit.distance < prev.distance:
                merged[hit.id] = hit

    nq = normalize_text(query)
    kept: list[Any] = []
    selves: list[str] = []
    for hit in merged.values():
        price = int(hit.metadata.get("price_jpy", 0) or 0)
        name = str(hit.metadata.get("item_name") or "")
        sim = 1.0 - hit.distance
        is_self = price == official and (normalize_text(name) == nq or sim >= SELF_SIM_THRESHOLD)
        if is_self:
            selves.append(f"{name}|¥{price}|sim={sim:.3f}")
        else:
            kept.append(hit)

    ranked = est._rerank(kept)[: est._top_k]
    refs = "\n".join(est._format_reference(h) for h in ranked)
    return f"### Query: {query}\n{refs or '(no matches)'}", selves


def run_once(est: Estimator) -> dict[str, tuple[int, bool]]:
    """One full pass over FIXTURES. Returns {query: (suggested_jpy, in_range)};
    suggested 0 means no estimate. Raises EstimationError on chat failure."""
    out: dict[str, tuple[int, bool]] = {}
    for start in range(0, len(FIXTURES), est.CHUNK_SIZE):
        chunk = FIXTURES[start:start + est.CHUNK_SIZE]
        blocks: list[str] = []
        for query, official in chunk:
            block, selves = build_context(est, query, official)
            blocks.append(block)
            tag = ", ".join(selves) if selves else "(none found)"
            print(f"  self-excluded [{query}]: {tag}")
        user_prompt = (
            "Products to estimate (one per line):\n"
            + "\n".join(q for q, _ in chunk)
            + "\n\nReference context:\n"
            + "\n\n".join(blocks)
        )
        batch = est._chat.estimate(SYSTEM_PROMPT, user_prompt)
        by_name: dict[str, Any] = {}
        for e in batch.estimates:
            by_name.setdefault(normalize_text(e.product_name), e)
        for query, official in chunk:
            est_obj = by_name.get(normalize_text(query))
            if est_obj is None:
                out[query] = (0, False)  # reconcile-style: missing -> no-estimate
            else:
                snapped = _snap_estimate(est_obj)
                in_range = (snapped.price_range_jpy.min <= official
                            <= snapped.price_range_jpy.max)
                out[query] = (snapped.suggested_price_jpy, in_range)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate /estimate accuracy.")
    parser.add_argument("--runs", type=int, default=3,
                        help="runs per fixture (>=3 for ship decisions)")
    args = parser.parse_args()

    config = load_config()
    providers = build_providers(config, with_chat=True)
    assert providers.chat is not None, "eval needs chat; check chat_api_key in .env"
    est = Estimator(
        providers.embedder, providers.chat, providers.vector_store,
        providers.typing_provider,
        item_types=config.item_types,
        item_types_version=config.item_types_version,
        top_k=config.estimator_top_k,
        recency_weight=config.estimator_recency_weight,
        diversity_weight=config.estimator_diversity_weight,
        fetch_multiplier=config.estimator_fetch_multiplier,
    )

    per_fixture: dict[str, list[tuple[int, bool]]] = {q: [] for q, _ in FIXTURES}
    try:
        for r in range(args.runs):
            print(f"\n===== run {r + 1}/{args.runs} =====")
            result = run_once(est)
            for q, _ in FIXTURES:
                per_fixture[q].append(result[q])
    except EstimationError as exc:
        print(f"\nINVALID run: chat failed ({exc}); not reporting summary.",
              file=sys.stderr)
        sys.exit(2)

    # no-estimate set: a fixture with ¥0 in ANY run (conservative aggregation).
    no_estimate = {q for q, vals in per_fixture.items() if any(v[0] == 0 for v in vals)}

    per_fixture_err: dict[str, float] = {}
    per_fixture_signed: dict[str, float] = {}
    coverage_total = 0
    coverage_hit = 0
    rows: list[tuple[str, int, int, float | None]] = []
    for q, official in FIXTURES:
        vals = per_fixture[q]
        if q in no_estimate:
            rows.append((q, 0, official, None))
            continue
        prices = [v[0] for v in vals]
        abs_errs = [abs(p - official) / official * 100.0 for p in prices]
        signed = [(p - official) / official * 100.0 for p in prices]
        per_fixture_err[q] = statistics.mean(abs_errs)
        per_fixture_signed[q] = statistics.mean(signed)
        coverage_total += len(vals)
        coverage_hit += sum(1 for v in vals if v[1])
        rows.append((q, round(statistics.mean(prices)), official, per_fixture_err[q]))

    print("\n\n========== PER-FIXTURE (mean over runs) ==========")
    print(f"  {'query':<46} {'est':>7} {'official':>8} {'err%':>7}")
    for q, mean_suggested, official, mean_abs in rows:
        err = "n/a" if mean_abs is None else f"{mean_abs:.1f}"
        marker = "  NO-EST" if q in no_estimate else ""
        print(f"  {q[:46]:<46} {mean_suggested:>7} {official:>8} {err:>7}{marker}")

    errs = list(per_fixture_err.values())
    signed_vals = list(per_fixture_signed.values())
    hits = sum(1 for e in errs if e < EXACT_HIT_PCT)
    print("\n========== SUMMARY ==========")
    print(f"  fixtures: {len(FIXTURES)}   estimated: {len(errs)}   "
          f"no-estimate: {len(no_estimate)} "
          f"({len(no_estimate) / len(FIXTURES) * 100:.0f}%)")
    if errs:
        print(f"  MAPE: {statistics.mean(errs):.1f}%   "
              f"median abs err: {statistics.median(errs):.1f}%   "
              f"mean signed err: {statistics.mean(signed_vals):+.1f}%")
        print(f"  exact-hit (<{EXACT_HIT_PCT:.0f}%): {hits}/{len(errs)} "
              f"({hits / len(errs) * 100:.0f}%)")
    if coverage_total:
        print(f"  range coverage: {coverage_hit}/{coverage_total} "
              f"({coverage_hit / coverage_total * 100:.0f}%)")
    if no_estimate:
        print(f"  no-estimate fixtures: {sorted(no_estimate)}")

    print("\n========== PROVENANCE ==========")
    print(f"  prompt_hash: {est._prompt_hash}")
    print(f"  git_commit: {_git(['rev-parse', '--short', 'HEAD'])}   "
          f"dirty: {bool(_git(['status', '--porcelain']))}")
    print(f"  embedding_model: {config.embedding_model}   "
          f"chat_model: {config.chat_model}")
    print(f"  fixtures: {len(FIXTURES)}   runs: {args.runs}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2：Type check**

Run: `.venv/bin/basedpyright scripts/analysis/eval_estimate.py`
Expected: 0 errors（檔頭 `# pyright: reportPrivateUsage=false` 已豁免私有存取）。

- [ ] **Step 3：Lint**

Run: `uvx ruff check scripts/analysis/eval_estimate.py`
Expected: `All checks passed!`（若報未使用 import，移除之）。

- [ ] **Step 4：Import smoke（不打 API）**

Run: `PYTHONPATH=. .venv/bin/python -c "import scripts.analysis.eval_estimate as m; print(len(m.FIXTURES))"`
Expected: 印出 `25`（確認模組可 import、fixtures 數正確、無語法錯）。

- [ ] **Step 5：Commit**

```bash
git add scripts/analysis/eval_estimate.py
git commit -m "feat(scripts): add /estimate accuracy eval with self-exclusion"
```

---

## Task 4：同步 `docs/data-pipeline.md` 階段 14

**Files:**
- Modify: `docs/data-pipeline.md:835-844`（階段 14 step 1 的 prompt 行為描述）與 `:869-873`（設計理由）

無 code、無測試。CLAUDE.md 強制：pipeline 行為改動須同 change 更新本文件。

- [ ] **Step 1：改寫階段 14 step 1 的 prompt 行為描述**

在 [data-pipeline.md:837-844](../../../docs/data-pipeline.md#L837) 找到自「`SYSTEM_PROMPT`...以 XML 區塊要求」起至「無強匹配仍給 `low` 估價而非捏造。」與其後「輸出欄位不在 prompt 重述...schema 強制。」的整段，替換為：

```markdown
   `SYSTEM_PROMPT`([estimator.py:16](../estimator_king/bot/estimator.py#L16))以 XML 區塊要求:
   每行一筆估價、同序不漏、**只能**用提供的參考(禁止引用參考以外的一般「相場」行情)、
   references 採嚴格優先序 **item_type > size/材質 > recency**(recency 僅作 tie-breaker)、
   **錨定 `<anchoring>`:預設錨在同類參考的「中位至上端」、不得低於中位數,除非查詢明確帶更
   便宜/更簡單的變體訊號;帶參考所無的溢價特徵(温感、もこもこ/あったか、加大、なりきり等)
   時錨上端**、**`<set_and_count>`:名稱中的種/個數(1種、2個セット、全4種)不是價格乘數,
   不在不同 set 大小間內插,按 item_type 與單品/套組層級比價**、價格為含稅且必為 **¥110
   整數倍**、**price_range 依信心分級且偏上(high −20/+30、medium −25/+45、low −30/+60)**、
   **confidence `high` 需近似同名同型 exact、查詢無參考所缺的額外修飾詞(聯名/尺寸/set count)、
   且 suggested 落在同類參考價格跨度內;泛用單字名 refs 價差大時降為 medium**、最多 3 筆
   `reference_products`、無強匹配仍給 `low` 估價而非捏造。
   輸出欄位不在 prompt 重述,由 `response_format=EstimateBatch` schema 強制。
```

- [ ] **Step 2：更新設計理由註記**

在 [data-pipeline.md:869-873](../../../docs/data-pipeline.md#L869) 的「prompt 與檢索設計呼應」bullet，找到結尾句「...這幾個欄位的原因。」，在其後（同一 bullet 內）接續一句：

```markdown
>   第二輪起 prompt 再以 `<anchoring>`(中位至上端、修正系統性低估)、
>   `<set_and_count>`(計數非價格乘數)、信心分級 range 與收緊的 `high` 判準補強估價推理;
>   net 效果以 `scripts/analysis/eval_estimate.py`(本尊排除的 25 筆 fixture)量測。
```

- [ ] **Step 3：人工確認 Markdown 結構未壞**

Run: `grep -n "<anchoring>\|set_and_count\|eval_estimate.py" docs/data-pipeline.md`
Expected: 階段 14 step 1 與設計理由各出現新關鍵字，確認替換生效。

- [ ] **Step 4：Commit**

```bash
git add docs/data-pipeline.md
git commit -m "docs(data-pipeline): sync chat-estimate prompt rules for round-2 tuning"
```

---

## 收尾：效果驗證（手動，非 CI）

實作完成後，依 spec 的 before/after 流程量測（需 live chroma + `.env`）：

- [ ] 在 baseline commit（Task 1 之前）跑：`set -a; source .env; set +a; PYTHONPATH=. .venv/bin/python scripts/analysis/eval_estimate.py --runs 3`，存下 MAPE / range 覆蓋率 / no-estimate 清單 / provenance。
- [ ] 在 candidate commit（Task 1-2 之後）以相同 `--runs 3` 再跑。
- [ ] 依 spec「相對驗收準則」判定：MAPE 不變差、range 覆蓋率不變差、candidate no-estimate 集合 ⊆ baseline、至少一筆目標 fixture（ポーチ/ボイス1種/ピンバッジ2個セット 等）per-fixture 平均誤差實質下降、兩份 run 皆 VALID。
- [ ] 把 before/after 兩份 stdout（含 provenance）記錄於 PR / commit 訊息。若不通過 → 回 Task 1 收斂措辭或放棄該規則（spec 已許可）。
