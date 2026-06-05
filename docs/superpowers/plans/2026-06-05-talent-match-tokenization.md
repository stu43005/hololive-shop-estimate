# Talent-Match Tokenization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make talent-enumeration products merge correctly when talent names are glued to a bracketed qualifier (`ときのそら（日本語）`) or contain internal whitespace (`小雀 とと` vs dict `小雀とと`), name merged items by their common part, and add the product title to the `/estimate` reference line.

**Architecture:** Rewrite `estimator_king/sync/items.py::_canonical_key` to split tokens on whitespace + parentheses and match talents via greedy longest n-gram against a whitespace-stripped dict; simplify item naming to two branches and drop three now-dead helpers; add a `product_title` column (with equality dedup) to `estimator_king/bot/estimator.py::_format_reference`.

**Tech Stack:** Python, pytest, basedpyright, ruff.

**Spec:** [docs/superpowers/specs/2026-06-05-talent-match-tokenization-design.md](../specs/2026-06-05-talent-match-tokenization-design.md)

---

## Background (current behavior to change)

`_canonical_key` ([items.py:60-69](../../../estimator_king/sync/items.py)) tokenizes on whitespace only and compares single tokens to the `talents` frozenset. The naming block ([items.py:159-181](../../../estimator_king/sync/items.py)) has three branches (product-title for merged/whole-product; `{title} {residual}` for `_is_option_value` short options; raw residual otherwise) and uses `_SIZE_RE` ([items.py:16-18](../../../estimator_king/sync/items.py)) and `_is_option_value` ([items.py:72-74](../../../estimator_king/sync/items.py)). `_format_reference` ([estimator.py:173-182](../../../estimator_king/bot/estimator.py)) emits `- {item_name} | {item_type} | ¥{price} | {date} | {store_id}` with no product title.

Why items.py grouping + naming change together: after tokenization merges several same-price groups under one product (e.g. voice languages all ¥1000), naming them all by product title would collide their slugs (`_item_slug(item_name, price)`, [engine.py:77-80](../../../estimator_king/sync/engine.py)). The common-part naming is what keeps the slugs distinct, so both land in Task 1.

## File Structure

- **Modify:** `estimator_king/sync/items.py` — `_canonical_key` rewrite + `_talents_nospace` helper + `_TOKEN_SPLIT`/`_MAX_TALENT_TOKENS` constants; remove `_SIZE_RE`/`_is_option_value`; `_Item` gains `key`; naming reduced to two branches; remove `whole_product_single`.
- **Modify:** `estimator_king/bot/estimator.py` — `_format_reference` adds the product_title column with equality dedup.
- **Modify (tests):** `tests/test_items.py`, `tests/test_estimator.py`.

No other files change. `stores_config.yaml` is intentionally NOT touched (no talents added, no `item_types_version` bump — spec §5).

---

## Task 1: items.py — tokenization, n-gram talent matching, naming (TDD)

**Files:**
- Modify: `estimator_king/sync/items.py`
- Test: `tests/test_items.py`

- [ ] **Step 1: Update existing tests and add new tests in `tests/test_items.py`**

Replace `test_talent_variants_merge_to_product_title` (lines 41-54) with:

```python
def test_talent_variants_merge_named_by_common_part():
    snap = _snap("3Dアクリルスタンド Blue Journey衣装ver.", [
        ("グッズ / さくらみこ Blue Journey衣装ver.", "330"),
        ("グッズ / 白上フブキ Blue Journey衣装ver.", "330"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    items = result.items
    assert len(items) == 1
    assert items[0].item_name == "Blue Journey衣装ver."  # common part, not product title
    assert items[0].price_jpy == 330
    assert len(items[0].source_variant_ids) == 2
    assert set(items[0].talents) == {"さくらみこ", "白上フブキ"}
    assert result.excluded_set == 0
    assert result.excluded_zero == 0
```

Replace `test_short_option_value_prepends_product_title` (lines 70-77) with:

```python
def test_short_option_value_named_by_residual():
    snap = _snap("ぶいすぽっ！オリジナルTシャツ", [
        ("バリエーション / 黒　M", "5500"),
        ("バリエーション / 白　L", "5500"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    names = sorted(i.item_name for i in result.items)
    # No product-title prefix; normalize_text collapses the full-width space (U+3000).
    assert names == ["白 L", "黒 M"]
```

Replace the body of `test_empty_residual_without_talent_not_merged` (lines 119-130) with:

```python
def test_empty_residual_without_talent_not_merged():
    # Residual strips to "" and no talent removed -> removed_any False -> must NOT merge.
    # With _is_option_value removed, the non-merged name is normalize_text("") == "".
    snap = _snap("グッズセット", [
        ("グッズ / ", "500"),
        ("グッズ / ", "500"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    assert len(result.items) == 2
    assert all(len(i.source_variant_ids) == 1 for i in result.items)
    assert all(i.item_name == "" for i in result.items)
```

Append these three new tests at the end of the file:

```python
def test_glued_language_suffix_merges_by_language():
    # Talent glued to a bracketed language tag: split on parens, remove talent, key = language.
    snap = _snap("秘密ボイス", [
        ("ボイス / さくらみこ（日本語）", "1000"),
        ("ボイス / 白上フブキ（日本語）", "1000"),
        ("ボイス / さくらみこ（英語）", "1000"),
        ("ボイス / 白上フブキ（英語）", "1000"),
    ])
    items = {i.item_name: i for i in decompose_items(snap, talents=TALENTS).items}
    assert set(items) == {"日本語", "英語"}  # two language groups, distinct names at one price
    assert len(items["日本語"].source_variant_ids) == 2
    assert len(items["英語"].source_variant_ids) == 2


def test_sp_voice_merges_by_common_part():
    # Talent + space + 'SPボイス' + glued language -> key = "SPボイス 日本語".
    snap = _snap("秘密ボイス", [
        ("ボイス / さくらみこ SPボイス（日本語）", "500"),
        ("ボイス / 白上フブキ SPボイス（日本語）", "500"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    assert len(result.items) == 1
    assert result.items[0].item_name == "SPボイス 日本語"
    assert len(result.items[0].source_variant_ids) == 2


def test_internal_space_talent_merges():
    # Talent written with an internal space (姓 名) still matches the no-space dict entry
    # via whitespace-insensitive greedy n-gram matching.
    snap = _snap("ぶいすぽっ！ジャージ", [
        ("バリエーション / さくら みこ", "7500"),
        ("バリエーション / 白上 フブキ", "7500"),
        ("バリエーション / 博衣こより", "7500"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    assert len(result.items) == 1
    item = result.items[0]
    assert item.item_name == "ぶいすぽっ！ジャージ"  # key empty -> product-title fallback
    assert len(item.source_variant_ids) == 3
    assert set(item.talents) == {"さくらみこ", "白上フブキ", "博衣こより"}  # original dict forms
```

- [ ] **Step 2: Run the test file and confirm the pre-change state**

Run:
```bash
.venv/bin/python -m pytest tests/test_items.py -v -o addopts=""
```

Expected (current unmodified `items.py`):
- FAIL: `test_talent_variants_merge_named_by_common_part` (current names it by product title).
- FAIL: `test_short_option_value_named_by_residual` (current prepends product title).
- FAIL: `test_empty_residual_without_talent_not_merged` (current names empty residual by product title).
- FAIL: `test_glued_language_suffix_merges_by_language` (current does not split parens → 4 separate items).
- FAIL: `test_sp_voice_merges_by_common_part` (current names the merged SP group by product title).
- FAIL: `test_internal_space_talent_merges` (current splits `小雀 とと`-style names → no match → 3 separate items).
- PASS: all other existing tests.

- [ ] **Step 3: Edit `estimator_king/sync/items.py`**

(3a) Replace the `_SIZE_RE` constant block (lines 16-18) — i.e. delete `_SIZE_RE` and add the two new constants. The block becomes:

```python
_TOKEN_SPLIT = re.compile(r"[\s（）()]+")  # split on whitespace + full/half-width parens
_MAX_TALENT_TOKENS = 4  # greedy n-gram cap (spaced 姓 名 names are 2 tokens; cap covers rare longer)
_SEGMENT_SPLIT = re.compile(r"[・◇\n]")
```

(3b) Replace `_canonical_key` (lines 60-69) with the `_talents_nospace` helper followed by the rewritten `_canonical_key`:

```python
def _talents_nospace(talents: frozenset[str]) -> dict[str, str]:
    """Map whitespace-stripped normalized talent -> original, for space-insensitive matching."""
    return {normalize_text(t).replace(" ", ""): t for t in talents}


def _canonical_key(residual: str, talents_nospace: dict[str, str]) -> tuple[str, list[str]]:
    """Drop talent tokens (greedy longest n-gram, whitespace-insensitive); return
    (canonical_key, removed_talent_originals)."""
    toks = [t for t in _TOKEN_SPLIT.split(normalize_text(residual)) if t]
    kept: list[str] = []
    removed: list[str] = []
    i = 0
    while i < len(toks):
        matched = False
        for j in range(min(len(toks), i + _MAX_TALENT_TOKENS), i, -1):  # longest first
            cand = "".join(toks[i:j])
            if cand in talents_nospace:
                removed.append(talents_nospace[cand])
                i = j
                matched = True
                break
        if not matched:
            kept.append(toks[i])
            i += 1
    return " ".join(kept), removed
```

(3c) Delete `_is_option_value` (lines 72-74) entirely. (`_extract_snippet`, `_strip_prefix`, `_price_to_int`, `_meaningful_tokens` are unchanged.)

(3d) In `decompose_items`, add the nospace map just before the `by_price` loop section and add a `key` field to `_Item`. Replace lines 124-157 (from the `# Step 3` comment through the end of the grouping `for` loop) with:

```python
    # Step 3: talent-gated canonical-key dedup, grouped by price.
    talents_nospace = _talents_nospace(talents)
    by_price: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for residual, price, vid in kept:
        by_price[price].append((residual, vid))

    @dataclass
    class _Item:
        residual: str | None  # None => merged group (name from key, or product title if key empty)
        key: str              # group canonical key (common part); "" for non-merged items
        price: int
        variant_ids: list[int]
        talents: list[str]

    raw_items: list[_Item] = []
    for price, members in by_price.items():
        groups: dict[str, list[tuple[str, int, list[str]]]] = defaultdict(list)
        for residual, vid in members:
            key, removed = _canonical_key(residual, talents_nospace)
            groups[key].append((residual, vid, removed))
        for key, group in groups.items():
            removed_any = any(r for _, _, r in group)
            if len(group) >= 2 and removed_any:
                merged_talents: list[str] = []
                for _, _, removed in group:
                    for t in removed:
                        if t not in merged_talents:
                            merged_talents.append(t)
                raw_items.append(_Item(
                    residual=None, key=key, price=price,
                    variant_ids=[vid for _, vid, _ in group], talents=merged_talents,
                ))
            else:
                for residual, vid, _ in group:
                    raw_items.append(_Item(residual=residual, key="", price=price,
                                           variant_ids=[vid], talents=[]))
```

(3e) Replace the naming block (lines 159-171, from the `# Step 4` comment through the `else: name = ri.residual` branch) with the two-branch version (drop `whole_product_single`):

```python
    # Step 4: naming (two branches) + snippet.
    items: list[ProductItem] = []
    for ri in raw_items:
        if ri.residual is None:
            name = ri.key.strip() or snapshot.title   # merged: common part; product title if key empty
        else:
            name = normalize_text(ri.residual)         # non-merged: normalized residual
```

(The `items.append(ProductItem(...))` block that follows, lines 172-181, is unchanged — it already uses `name`.)

- [ ] **Step 4: Run the test file and confirm all pass**

Run:
```bash
.venv/bin/python -m pytest tests/test_items.py -v -o addopts=""
```

Expected: **all tests PASS** (the six previously-failing tests now pass; the untouched tests stay green).

- [ ] **Step 5: Run the verification toolchain**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king tests
```

Expected: basedpyright `0 errors` in production code; ruff `All checks passed!`. (`re` is still imported and used by `_TOKEN_SPLIT`/`_SEGMENT_SPLIT`, so no unused-import warning.)

- [ ] **Step 6: Commit**

```bash
git add estimator_king/sync/items.py tests/test_items.py
git commit -m "feat(items): whitespace-insensitive n-gram talent match, common-part naming"
```

---

## Task 2: estimator.py — product_title on the reference line (TDD)

**Files:**
- Modify: `estimator_king/bot/estimator.py`
- Test: `tests/test_estimator.py`

- [ ] **Step 1: Update `_hit` and the format test, add a dedup test in `tests/test_estimator.py`**

Replace `_hit` (lines 31-34) with the version that threads `product_title` into metadata:

```python
def _hit(id, item_type, price, pub, dist, product_title="P"):
    return QueryHit(id=id, document="", distance=dist, metadata={
        "item_name": id, "item_type": item_type, "price_jpy": price,
        "published_at": pub, "store_id": "s", "detail_snippet": "",
        "product_title": product_title})
```

Replace the assertion in `test_context_line_format_shape` (line 116) with:

```python
    assert "- itemX | ぬいぐるみ | P | ¥500 | ? | s" in chat.last_user_prompt
```

Append this new test at the end of the file:

```python
def test_reference_line_omits_product_when_equal_to_item_name():
    vs = RecordingVectorStore([_hit("P", "ぬいぐるみ", 500, 0, 0.1, product_title="P")])
    chat = FakeChat([_est("もちもちぬいぐるみ")])
    est = _estimator(vs, chat)
    est.estimate_products(["もちもちぬいぐるみ"], "u")
    prompt = chat.last_user_prompt
    assert "- P | ぬいぐるみ | ¥500 | ? | s" in prompt
    assert prompt.count("| P |") == 0  # product not repeated as its own column
```

- [ ] **Step 2: Run the test file and confirm the pre-change state**

Run:
```bash
.venv/bin/python -m pytest tests/test_estimator.py -v -o addopts=""
```

Expected (current unmodified `estimator.py`):
- FAIL: `test_context_line_format_shape` (current line has no product column → `| P |` absent).
- PASS: `test_reference_line_omits_product_when_equal_to_item_name` (guard — current never adds a product column, so both asserts already hold).
- PASS: all other existing tests (current `_format_reference` ignores the new metadata key).

- [ ] **Step 3: Edit `estimator_king/bot/estimator.py`**

Replace the `line = (...)` assignment in `_format_reference` (lines 177-178) with the field-list build that inserts product_title only when it differs from item_name:

```python
        item_name = str(m.get("item_name") or "")
        product_title = str(m.get("product_title") or "")
        fields = [item_name, str(m.get("item_type") or "")]
        if product_title and product_title != item_name:
            fields.append(product_title)
        fields += [f"¥{m.get('price_jpy')}", date, str(m.get("store_id") or "")]
        line = "- " + " | ".join(fields)
```

(The `pub`/`date` lines above and the `snippet` lines below, [estimator.py:175-176](../../../estimator_king/bot/estimator.py) and [estimator.py:179-182](../../../estimator_king/bot/estimator.py), are unchanged.)

- [ ] **Step 4: Run the test file and confirm all pass**

Run:
```bash
.venv/bin/python -m pytest tests/test_estimator.py -v -o addopts=""
```

Expected: **all tests PASS**.

- [ ] **Step 5: Run the verification toolchain**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king tests
.venv/bin/python -m pytest -o addopts=""
```

Expected: basedpyright `0 errors`; ruff clean; full suite green.

- [ ] **Step 6: Commit**

```bash
git add estimator_king/bot/estimator.py tests/test_estimator.py
git commit -m "feat(estimate): add product title column to reference line with dedup"
```

---

## Task 3: Operational verification on real data

**Files:** none (verification only; no commit unless findings require a fix).

This confirms spec §7.1 (acceptance #6) against live product data. It is network-dependent; if the store is unreachable, record that and rely on the Task 1 unit tests.

- [ ] **Step 1: Run the real-data decompose check**

Run (sources `.env` for nothing needed here — this only fetches public product JSON and calls the pure `decompose_items`):

```bash
.venv/bin/python - <<'PY'
import json, urllib.request, yaml
from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.sync.items import decompose_items

talents = frozenset(yaml.safe_load(open("stores_config.yaml")).get("talents", []))

def fetch(store, pid):
    for pg in range(1, 6):
        url = f"https://{store}/products.json?limit=250&page={pg}"
        for p in json.load(urllib.request.urlopen(url)).get("products", []):
            if p["id"] == pid:
                return p
    raise SystemExit(f"product {pid} not found on {store}")

def run(store, pid, label):
    p = fetch(store, pid)
    snap = ProductSnapshot(
        product_id=p["id"], title=p["title"], description="",
        variants=[ProductVariant(variant_id=v["id"], title=v["title"], price=v["price"])
                  for v in p["variants"]],
        html_details={})
    res = decompose_items(snap, talents=talents)
    merged = [i for i in res.items if len(i.source_variant_ids) >= 2]
    print(f"\n=== {label}: {len(res.items)} items, {len(merged)} merged ===")
    for i in merged:
        print(f"  {i.item_name!r} x{len(i.source_variant_ids)} ¥{i.price_jpy}")

run("shop.hololivepro.com", 9384273215708, "秘密の雨の日ボイス")
run("store.vspo.jp", 7866043990203, "ぶいすぽっ！ジャージ")
PY
```

Expected output:
- `秘密の雨の日ボイス`: 5 merged items — `'日本語' x27`, `'英語' x12`, `'インドネシア語' x6`, `'SPボイス 日本語' x12`, `'SPボイス 英語' x3` (item_names distinct; other non-voice items may also appear).
- `ぶいすぽっ！ジャージ`: 1 merged item — `'ぶいすぽっ！ジャージ' x25`.

If the merges match, §7.1 is verified. If a store is unreachable, note it and proceed (Task 1 unit tests cover the same logic on synthetic snapshots).

- [ ] **Step 2: Note the snippet hit-rate follow-up (spec §7.3)**

`_extract_snippet` matches on the new `item_name`. `products.json` carries no `html_details`, so a true hit-rate comparison needs a real crawl. Record this as a post-deploy observation: after the next full `crawl`, spot-check that merged merch items (e.g. `アクリルスタンド`) still populate `detail_snippet` where the source lists per-item specs. This is best-effort and non-blocking (snippet absence degrades safely; it does not affect price/type/retrieval).

---

## Acceptance (maps to spec §10)

1. Glued-suffix and internal-space talent enumerations both merge correctly; same-price language groups get distinct item_names → Task 1 tests `test_glued_language_suffix_merges_by_language`, `test_sp_voice_merges_by_common_part`, `test_internal_space_talent_merges`; no spurious merges confirmed during design (§7.2).
2. Merged items named by common part; product-title fallback when key empty → `test_talent_variants_merge_named_by_common_part`, `test_pure_talent_enumeration_merges_to_product_title`.
3. `whole_product_single`/`_is_option_value`/`_SIZE_RE` removed; non-merged named by `normalize_text(residual)`; empty residual → `""` → `test_short_option_value_named_by_residual`, `test_empty_residual_without_talent_not_merged`.
4. Reference line adds product_title when `!= item_name`, omits when equal → `test_context_line_format_shape`, `test_reference_line_omits_product_when_equal_to_item_name`.
5. Real `秘密の雨の日ボイス` → 5 merged, `ぶいすぽっ！ジャージ` → 1 merged → Task 3.
6. Verification toolchain green (basedpyright 0 errors, ruff, full pytest) → Task 1/2 Step 5.
