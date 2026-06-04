# Talent-Enumeration Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Relax the empty canonical-key guard in `decompose_items` so products whose variant residuals are purely talent names (e.g. voice packs) merge into a single product-title item instead of one talent-named vector each.

**Architecture:** Single-line change to the merge condition in `estimator_king/sync/items.py` (drop the `key.strip()` term, keep `removed_any`). Empty-key groups then flow through the existing `residual=None` → product-title naming branch unchanged. Three new tests in `tests/test_items.py` plus existing regression tests.

**Tech Stack:** Python, pytest, basedpyright, ruff.

---

## Background (what the existing code does)

`decompose_items` ([estimator_king/sync/items.py:108](../../../estimator_king/sync/items.py)) groups kept variants by price, then within each price computes a canonical key by dropping talent tokens (`_canonical_key`, [items.py:60-69](../../../estimator_king/sync/items.py)). The merge decision is [items.py:144](../../../estimator_king/sync/items.py):

```python
if len(group) >= 2 and key.strip() and removed_any:
```

When a variant residual is purely a talent name (e.g. `"さくらみこ"`), `_canonical_key` returns `("", ["さくらみこ"])`; `key.strip()` is falsy → the group is **not** merged → each variant becomes its own talent-named item. Dropping `key.strip()` lets such empty-key groups merge as a whole-group item (`residual=None`), which the naming branch [items.py:166](../../../estimator_king/sync/items.py) (`if ri.residual is None or whole_product_single:`) already names with `snapshot.title`. `removed_any` ([items.py:143](../../../estimator_king/sync/items.py)) stays to block all-blank, no-talent groups.

## File Structure

- **Modify:** `estimator_king/sync/items.py` — one boolean condition at line 144. No signature changes, no new functions, no naming-branch changes.
- **Modify (add tests):** `tests/test_items.py` — three new test functions appended, using the existing module-level `TALENTS` frozenset and `_snap` helper ([tests/test_items.py:4-13](../../../tests/test_items.py)).

No other files change. `stores_config.yaml` is intentionally NOT touched (no `talents` additions, no `item_types_version` bump — per spec §5).

---

## Task 1: Relax the empty canonical-key merge guard (TDD)

**Files:**
- Modify: `estimator_king/sync/items.py:144`
- Test: `tests/test_items.py`

Existing test conventions you will reuse verbatim (already in the file, do not redefine):

```python
from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.sync.items import DecomposeResult, decompose_items

TALENTS = frozenset({"さくらみこ", "白上フブキ", "博衣こより"})


def _snap(title, variants, html_details=None, pid=1):
    return ProductSnapshot(
        product_id=pid, title=title, description="",
        variants=[ProductVariant(variant_id=i + 1, title=t, price=p)
                  for i, (t, p) in enumerate(variants)],
        html_details=html_details or {},
    )
```

- [ ] **Step 1: Write the three new tests**

Append the following to the end of `tests/test_items.py`:

```python
def test_pure_talent_enumeration_merges_to_product_title():
    # Each variant residual is a bare talent name (empty canonical key) at one price.
    snap = _snap("隣人ボイス2026", [
        ("ボイス / さくらみこ", "140"),
        ("ボイス / 白上フブキ", "140"),
        ("ボイス / 博衣こより", "140"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    assert len(result.items) == 1
    item = result.items[0]
    assert item.item_name == "隣人ボイス2026"  # named by product title (residual=None branch)
    assert item.price_jpy == 140
    assert len(item.source_variant_ids) == 3
    assert set(item.talents) == {"さくらみこ", "白上フブキ", "博衣こより"}


def test_empty_residual_without_talent_not_merged():
    # Residual strips to "" but no talent removed -> removed_any False -> must NOT merge.
    # Assert on item COUNT, not item_name: both empty-residual items get name == product
    # title via _is_option_value("") (len("") < 4) -> f"{title} ".strip().
    snap = _snap("グッズセット", [
        ("グッズ / ", "500"),
        ("グッズ / ", "500"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    assert len(result.items) == 2
    assert all(len(i.source_variant_ids) == 1 for i in result.items)
    assert all(i.item_name == "グッズセット" for i in result.items)


def test_pure_talent_enumeration_coexists_with_distinct_item():
    # Pure-talent voices (¥140) merge to product title; a non-talent item (¥500,
    # non-empty key) stays separate and untouched.
    snap = _snap("誕生日記念", [
        ("ボイス / さくらみこ", "140"),
        ("ボイス / 白上フブキ", "140"),
        ("グッズ / アクリルスタンド", "500"),
    ])
    items = {i.item_name: i for i in decompose_items(snap, talents=TALENTS).items}
    assert set(items) == {"誕生日記念", "アクリルスタンド"}
    assert len(items["誕生日記念"].source_variant_ids) == 2
    assert set(items["誕生日記念"].talents) == {"さくらみこ", "白上フブキ"}
    assert items["アクリルスタンド"].price_jpy == 500
    assert len(items["アクリルスタンド"].source_variant_ids) == 1
```

- [ ] **Step 2: Run the new tests and confirm the expected pre-change state**

Run:
```bash
.venv/bin/python -m pytest tests/test_items.py -v -o addopts=""
```

Expected (under the current, unmodified `items.py`):
- `test_pure_talent_enumeration_merges_to_product_title` → **FAIL** (current code produces 3 separate items, so `len(result.items) == 1` fails).
- `test_pure_talent_enumeration_coexists_with_distinct_item` → **FAIL** (current code produces 3 items named `さくらみこ` / `白上フブキ` / `アクリルスタンド`, so the `set(items)` assert fails).
- `test_empty_residual_without_talent_not_merged` → **PASS** (regression guard; `removed_any` already blocks the merge, so behaviour is identical before and after).
- All other existing tests → **PASS**.

- [ ] **Step 3: Make the minimal implementation change**

In `estimator_king/sync/items.py`, change the merge condition at line 144 by removing the `key.strip()` term:

```python
            if len(group) >= 2 and removed_any:
```

(was: `if len(group) >= 2 and key.strip() and removed_any:`)

Do not change anything else — the `_Item(residual=None, ...)` append below it and the naming branch at line 166 already handle product-title naming for the merged item.

- [ ] **Step 4: Run the full test file and confirm all pass**

Run:
```bash
.venv/bin/python -m pytest tests/test_items.py -v -o addopts=""
```

Expected: **all tests PASS**, including the two that previously failed and the existing regression tests (`test_talent_variants_merge_to_product_title`, `test_themed_series_not_merged_even_at_same_price`, `test_excludes_set_and_zero_price`, `test_unparseable_price_counts_as_excluded_zero`, `test_short_option_value_prepends_product_title`, `test_detail_snippet_substring_match`, `test_voice_item_has_no_snippet`).

- [ ] **Step 5: Run the verification toolchain**

Run (all three must be clean):
```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king tests
.venv/bin/python -m pytest tests/test_items.py -o addopts=""
```

Expected:
- basedpyright: `0 errors` in production code (`estimator_king/`).
- ruff: no new findings.
- pytest: all green.

- [ ] **Step 6: Commit**

```bash
git add estimator_king/sync/items.py tests/test_items.py
git commit -m "feat(items): merge pure-talent variant enumerations into one item"
```

(Commit message follows the repo's semantic + English convention; implementation file and its tests committed together as one atomic unit.)

---

## Acceptance (maps to spec §9)

1. Pure-talent enumeration (multi-variant, all-talent residuals, same price) merges to a single product-title item with talents collected and correct price → `test_pure_talent_enumeration_merges_to_product_title`.
2. Non-talent item (e.g. `アクリルスタンド`) is never folded into an empty-key group → `test_pure_talent_enumeration_coexists_with_distinct_item`.
3. All-blank, no-talent group is not merged (`removed_any` guard), verified by item count not `item_name` → `test_empty_residual_without_talent_not_merged`.
4. Existing merge/non-merge behaviour unchanged → `test_talent_variants_merge_to_product_title`, `test_themed_series_not_merged_even_at_same_price` stay green.
5. Verification toolchain green (basedpyright 0 errors, ruff, pytest) → Step 5.
