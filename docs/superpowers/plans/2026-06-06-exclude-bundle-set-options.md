# Bundle-Set Option Exclusion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Exclude whole-set bundle options (items whose `item_name` contains a configured
bundle keyword, or which are priced far above same-product peers) from the decomposed
priceable items, driven by config in `stores_config.yaml`.

**Architecture:** A post-pass inside `decompose_items` flags each `ProductItem` as a bundle
via (A) keyword substring match on `item_name`, or (B) a price tie-breaker
(`item_name` contains 「セット」 and `price_jpy / median(peer price_jpy) ≥ ratio`,
unless protected by a keep-keyword whitelist). A new `BundleSetPolicy` config object is
loaded from YAML and threaded through `cycle → async_process_queue → sync_products →
_rebuild_product_items`, unpacked into three primitive params at the `decompose_items`
call. Excluded counts flow `DecomposeResult → RebuildReport → SyncResult.excluded → log`.

**Tech Stack:** Python 3, stdlib `statistics`, dataclasses, pytest, basedpyright, ruff.

**Verification toolchain (per CLAUDE.md):**
- Type check: `.venv/bin/basedpyright <paths>` (0 errors in `estimator_king/`)
- Lint: `uvx ruff check <paths>`
- Single/per-file test: `.venv/bin/python -m pytest <path> -v -o addopts=""`

---

## File Structure

- **Modify** `estimator_king/config_schema.py` — add `BundleSetPolicy` dataclass, an
  `AppConfig.bundle_set` field, YAML parsing in `load_config`, and validation hook.
- **Modify** `estimator_king/sync/items.py` — add `import statistics`, an
  `excluded_bundle` field on `DecomposeResult`, three keyword-only bundle params on
  `decompose_items`, an `_is_bundle` helper, and the post-pass filter loop.
- **Modify** `estimator_king/sync/engine.py` — add `excluded_bundle` on `RebuildReport`,
  thread `bundle_set` through `sync_products` and `_rebuild_product_items`, unpack it at
  the `decompose_items` call, fold the count into `SyncResult.excluded`, and surface it
  in `_format_product_tree`.
- **Modify** `estimator_king/crawler/async_pipeline.py` — add `bundle_set` param on
  `async_process_queue` and forward it to `sync_products`.
- **Modify** `estimator_king/crawler/cycle.py` — pass `bundle_set=config.bundle_set` at
  the `async_process_queue` call.
- **Modify** `stores_config.yaml` — add the `bundle_set:` block and bump
  `item_types_version` 3 → 4 to force re-index.
- **Modify** `tests/test_config_schema.py` — parsing/default/validation tests.
- **Modify** `tests/test_items.py` — decomposition filter behavior tests.
- **Modify** `tests/test_engine_items.py` — engine-path exclusion + counter test.

---

## Task 1: `BundleSetPolicy` config object

**Files:**
- Modify: `estimator_king/config_schema.py` (add dataclass after `ProxyConfig` at line 88;
  add field on `AppConfig`; call validate in `AppConfig.validate`; parse in `load_config`)
- Test: `tests/test_config_schema.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config_schema.py`:

```python
def test_load_config_parses_bundle_set_section(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    path = _write_yaml(tmp_path, """
        stores:
          - id: s
            base_url: https://x
            sitemap_url: https://x/sitemap.xml
        bundle_set:
          keywords: [グッズセット, フルセット]
          price_ratio: 4.0
          keep_keywords: [ステッカーセット]
    """)
    cfg = load_config(path)
    assert cfg.bundle_set.keywords == frozenset({"グッズセット", "フルセット"})
    assert cfg.bundle_set.price_ratio == 4.0
    assert cfg.bundle_set.keep_keywords == frozenset({"ステッカーセット"})


def test_load_config_bundle_set_defaults_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    path = _write_yaml(tmp_path, """
        stores:
          - id: s
            base_url: https://x
            sitemap_url: https://x/sitemap.xml
    """)
    cfg = load_config(path)
    assert cfg.bundle_set.keywords == frozenset()
    assert cfg.bundle_set.keep_keywords == frozenset()
    assert cfg.bundle_set.price_ratio == 5.0


def test_bundle_set_policy_rejects_non_positive_ratio():
    import pytest
    from estimator_king.config_schema import BundleSetPolicy
    with pytest.raises(ValueError, match="price_ratio"):
        BundleSetPolicy(price_ratio=0.0).validate()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_config_schema.py -v -o addopts=""`
Expected: FAIL — `AttributeError: 'AppConfig' object has no attribute 'bundle_set'`
and `ImportError: cannot import name 'BundleSetPolicy'`.

- [ ] **Step 3: Add the `BundleSetPolicy` dataclass**

In `estimator_king/config_schema.py`, after the `ProxyConfig` class (after line 87,
before `@dataclass` / `class AppConfig`), insert:

```python
@dataclass(frozen=True)
class BundleSetPolicy:
    """Policy for excluding whole-set bundle options from decomposed items."""

    keywords: frozenset[str] = frozenset()
    price_ratio: float = 5.0
    keep_keywords: frozenset[str] = frozenset()

    def validate(self):
        """Validate bundle-set policy."""
        if self.price_ratio <= 0:
            raise ValueError("bundle_set.price_ratio must be greater than 0")
```

- [ ] **Step 4: Add the `AppConfig.bundle_set` field**

In `estimator_king/config_schema.py`, in `class AppConfig`, immediately after the
`talents: frozenset[str] = field(default_factory=frozenset)` line (line 124), add:

```python
    bundle_set: "BundleSetPolicy" = field(default_factory=BundleSetPolicy)
```

- [ ] **Step 5: Validate it in `AppConfig.validate`**

In `estimator_king/config_schema.py`, in `AppConfig.validate` after
`self.proxy.validate()` (line 158), add:

```python
        # Validate bundle-set policy
        self.bundle_set.validate()
```

- [ ] **Step 6: Parse the `bundle_set` block in `load_config`**

In `estimator_king/config_schema.py`, inside `load_config`, after the proxy block
(after line 256, before the `_opt_int` helper at line 258), add:

```python
    # Parse bundle-set policy
    bundle_data = yaml_data.get("bundle_set", {}) or {}
    bundle_set = BundleSetPolicy(
        keywords=frozenset(bundle_data.get("keywords", []) or []),
        price_ratio=float(bundle_data.get("price_ratio", 5.0)),
        keep_keywords=frozenset(bundle_data.get("keep_keywords", []) or []),
    )
```

Then in the `AppConfig(...)` constructor, immediately after the
`talents=frozenset(yaml_data.get("talents", []) or []),` line (line 287), add:

```python
        bundle_set=bundle_set,
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_config_schema.py -v -o addopts=""`
Expected: PASS (all three new tests plus existing ones).

- [ ] **Step 8: Type check + lint**

Run: `.venv/bin/basedpyright estimator_king/config_schema.py`
Expected: 0 errors.
Run: `uvx ruff check estimator_king/config_schema.py tests/test_config_schema.py`
Expected: no findings.

- [ ] **Step 9: Commit**

```bash
git add estimator_king/config_schema.py tests/test_config_schema.py
git commit -m "feat(config): add BundleSetPolicy parsing and validation"
```

---

## Task 2: `decompose_items` bundle filter

**Files:**
- Modify: `estimator_king/sync/items.py` (add `import statistics`; `DecomposeResult`
  at lines 33-37; `decompose_items` signature at line 117; add `_is_bundle` helper;
  add post-pass before the `return DecomposeResult(...)` at lines 187-188)
- Test: `tests/test_items.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_items.py`:

```python
BUNDLE_KW = frozenset({"グッズセット", "フルセット", "応援セット", "語セット"})
BUNDLE_KEEP = frozenset({"クリアファイルセット", "缶バッジセット", "ボイスセット"})


def test_bundle_keyword_excluded_regardless_of_price():
    # "バースデーグッズセット" matches keyword "グッズセット"; excluded even though its
    # price (1500) is below the peer median (2 peers at 3000) -> ratio 0.5 < 5.
    snap = _snap("誕生日", [
        ("グッズ / バースデーグッズセット", "1500"),
        ("グッズ / アクリルスタンド", "3000"),
        ("グッズ / タペストリー", "3000"),
    ])
    result = decompose_items(
        snap, talents=TALENTS,
        bundle_keywords=BUNDLE_KW, bundle_price_ratio=5.0, bundle_keep_keywords=BUNDLE_KEEP)
    names = [i.item_name for i in result.items]
    assert "バースデーグッズセット" not in names
    assert set(names) == {"アクリルスタンド", "タペストリー"}
    assert result.excluded_bundle == 1


def test_keep_keyword_protects_high_ratio_set():
    # "クリアファイルセット" is a single-product type: high ratio (3600 vs median 660)
    # but on the keep whitelist -> kept.
    snap = _snap("クリアファイル", [
        ("グッズ / クリアファイルセット", "3600"),
        ("グッズ / ステッカー", "660"),
        ("グッズ / ポストカード", "660"),
    ])
    result = decompose_items(
        snap, talents=TALENTS,
        bundle_keywords=BUNDLE_KW, bundle_price_ratio=5.0, bundle_keep_keywords=BUNDLE_KEEP)
    assert "クリアファイルセット" in [i.item_name for i in result.items]
    assert result.excluded_bundle == 0


def test_price_tiebreaker_excludes_non_keyword_set():
    # "stage セット" has no bundle keyword, not on keep list, ratio 30000/1000 = 30 >= 5
    # and name contains セット -> excluded by (B).
    snap = _snap("アクリルスタンド", [
        ("グッズ / hololive stageセット", "30000"),
        ("グッズ / 単品A", "1000"),
        ("グッズ / 単品B", "1000"),
    ])
    result = decompose_items(
        snap, talents=TALENTS,
        bundle_keywords=BUNDLE_KW, bundle_price_ratio=5.0, bundle_keep_keywords=BUNDLE_KEEP)
    names = [i.item_name for i in result.items]
    assert "hololive stageセット" not in names
    assert result.excluded_bundle == 1


def test_bundle_keyword_excluded_with_no_peers():
    # Single variant whose name matches a keyword: (A) fires with no peers needed.
    snap = _snap("フルセット商品", [
        ("グッズ / フルセット", "20000"),
    ])
    result = decompose_items(
        snap, talents=TALENTS,
        bundle_keywords=BUNDLE_KW, bundle_price_ratio=5.0, bundle_keep_keywords=BUNDLE_KEEP)
    assert result.items == []
    assert result.excluded_bundle == 1


def test_low_ratio_non_keyword_set_kept():
    # "缶バッジセット" non-keyword, on keep list, and ratio low -> kept; excluded_bundle 0.
    snap = _snap("缶バッジ", [
        ("グッズ / 缶バッジセット", "1000"),
        ("グッズ / タペストリー", "5000"),
        ("グッズ / アクリルスタンド", "5000"),
    ])
    result = decompose_items(
        snap, talents=TALENTS,
        bundle_keywords=BUNDLE_KW, bundle_price_ratio=5.0, bundle_keep_keywords=BUNDLE_KEEP)
    assert "缶バッジセット" in [i.item_name for i in result.items]
    assert result.excluded_bundle == 0


def test_bundle_filter_default_params_noop():
    # Default (no bundle params): no exclusion, excluded_bundle 0, field present.
    snap = _snap("P", [
        ("グッズ / アクリルスタンド", "500"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    assert result.excluded_bundle == 0
    assert [i.item_name for i in result.items] == ["アクリルスタンド"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_items.py -v -o addopts=""`
Expected: FAIL — `TypeError: decompose_items() got an unexpected keyword argument
'bundle_keywords'` and `AttributeError: 'DecomposeResult' object has no attribute
'excluded_bundle'`.

- [ ] **Step 3: Add the `statistics` import**

In `estimator_king/sync/items.py`, after `import re` (line 10), add:

```python
import statistics
```

- [ ] **Step 4: Add the `excluded_bundle` field to `DecomposeResult`**

In `estimator_king/sync/items.py`, in `class DecomposeResult` (lines 33-37), add the
field after `excluded_zero: int`:

```python
@dataclass(frozen=True)
class DecomposeResult:
    items: list[ProductItem]
    excluded_set: int
    excluded_zero: int
    excluded_bundle: int = 0
```

- [ ] **Step 5: Add the `_is_bundle` helper**

In `estimator_king/sync/items.py`, immediately before `def decompose_items` (before
line 117), add:

```python
def _is_bundle(
    item: ProductItem,
    peers: list[ProductItem],
    keywords: frozenset[str],
    price_ratio: float,
    keep_keywords: frozenset[str],
) -> bool:
    """True if item is a whole-set bundle option to exclude.

    (A) item_name contains any bundle keyword -> exclude regardless of price.
    (B) item_name contains 「セット」, is not whitelisted, and its price is at least
        `price_ratio` times the median of same-product peer prices.
    """
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

- [ ] **Step 6: Add the bundle params and post-pass filter to `decompose_items`**

In `estimator_king/sync/items.py`, change the `decompose_items` signature (line 117)
from:

```python
def decompose_items(snapshot: ProductSnapshot, *, talents: frozenset[str]) -> DecomposeResult:
```

to:

```python
def decompose_items(
    snapshot: ProductSnapshot,
    *,
    talents: frozenset[str],
    bundle_keywords: frozenset[str] = frozenset(),
    bundle_price_ratio: float = 5.0,
    bundle_keep_keywords: frozenset[str] = frozenset(),
) -> DecomposeResult:
```

Then replace the final return block (lines 187-188):

```python
    return DecomposeResult(
        items=items, excluded_set=excluded_set, excluded_zero=excluded_zero)
```

with the post-pass filter followed by the augmented return:

```python
    # Post-pass: drop whole-set bundle options (leave-one-out peer comparison over the
    # full item set computed before any removal).
    kept_items: list[ProductItem] = []
    excluded_bundle = 0
    for item in items:
        peers = [other for other in items if other is not item]
        if _is_bundle(item, peers, bundle_keywords, bundle_price_ratio, bundle_keep_keywords):
            excluded_bundle += 1
            continue
        kept_items.append(item)

    return DecomposeResult(
        items=kept_items, excluded_set=excluded_set, excluded_zero=excluded_zero,
        excluded_bundle=excluded_bundle)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_items.py -v -o addopts=""`
Expected: PASS (all new tests plus all existing `test_items.py` tests unchanged).

- [ ] **Step 8: Type check + lint**

Run: `.venv/bin/basedpyright estimator_king/sync/items.py`
Expected: 0 errors.
Run: `uvx ruff check estimator_king/sync/items.py tests/test_items.py`
Expected: no findings.

- [ ] **Step 9: Commit**

```bash
git add estimator_king/sync/items.py tests/test_items.py
git commit -m "feat(items): exclude whole-set bundle options in decompose_items"
```

---

## Task 3: Thread `bundle_set` through the engine

**Files:**
- Modify: `estimator_king/sync/engine.py` (TYPE_CHECKING import; `RebuildReport` at
  lines 70-74; `_format_product_tree` at lines 100-108 + its caller at lines 185-187;
  `sync_products` signature at lines 127-139 + accumulation at line 178 + call at
  lines 166-169; `_rebuild_product_items` signature at lines 212-223 + `decompose_items`
  call at line 228 + `return RebuildReport(...)` at lines 272-276)
- Test: `tests/test_engine_items.py` (new exclusion test)
- Test: `tests/test_engine_logging.py` (update the head-string assertion at line 89 to
  the new `(SET×.., ¥0×.., bundle×..)` format)

- [ ] **Step 1: Write the failing test**

`tests/test_engine_items.py` already defines everything this test needs:
`ProductSnapshot`/`ProductVariant` (imported line 2), `sync_products` (line 4),
`ProductStateRepository` (line 3), `TALENTS`/`ITEM_TYPES` (lines 6-7), the `Fake*`
providers (classes at lines 10/15/35), and a `_repo()` helper (lines 51-54) that returns
an **opened** in-memory repo (`ProductStateRepository(":memory:")` + `repo.open()`).
`sync_products` calls `repository.get_by_external_key(...)`/`upsert(...)`, which require
an open connection — always use `_repo()`, never construct a repo inline.

Add this import near the top of `tests/test_engine_items.py`:

```python
from estimator_king.config_schema import BundleSetPolicy
```

Append:

```python
def test_sync_excludes_bundle_option():
    repo, vs = _repo(), FakeVectorStore()
    snap = ProductSnapshot(
        product_id=1, title="誕生日", description="",
        variants=[
            ProductVariant(variant_id=1, title="グッズ / バースデーグッズセット", price="9000"),
            ProductVariant(variant_id=2, title="グッズ / アクリルスタンド", price="3000"),
            ProductVariant(variant_id=3, title="グッズ / タペストリー", price="3000"),
        ],
        html_details={},
    )
    res = sync_products(
        [("http://x/products/1", snap)], "hololive", repo,
        FakeEmbedder(), vs,
        typing_provider=FakeTypingProvider(), talents=TALENTS,
        item_types=ITEM_TYPES, item_types_version=1,
        bundle_set=BundleSetPolicy(keywords=frozenset({"グッズセット"})),
    )
    assert res.items == 2          # bundle dropped, two singles kept
    assert res.excluded == 1       # the one bundle option (matched keyword グッズセット)
    names = {m["item_name"] for _, m in vs.docs.values()}
    assert "バースデーグッズセット" not in names
    assert names == {"アクリルスタンド", "タペストリー"}
```

(`バースデーグッズセット` contains the keyword `グッズセット` → excluded by rule (A),
regardless of its 9000 price; the two singles remain.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_engine_items.py::test_sync_excludes_bundle_option -v -o addopts=""`
Expected: FAIL — `TypeError: sync_products() got an unexpected keyword argument
'bundle_set'`.

- [ ] **Step 3: Add the TYPE_CHECKING import for `BundleSetPolicy`**

`engine.py` already imports `Iterable, Literal, Protocol, Sequence` from `typing` (line 9).
Extend that line to also import `TYPE_CHECKING`:

```python
from typing import Iterable, Literal, Protocol, Sequence, TYPE_CHECKING
```

Then, after the existing imports (after line 19, before the
`logger = logging.getLogger(__name__)` line at line 21), add the guarded import block:

```python
if TYPE_CHECKING:
    from estimator_king.config_schema import BundleSetPolicy
```

- [ ] **Step 4: Add `excluded_bundle` to `RebuildReport`**

In `estimator_king/sync/engine.py`, change `RebuildReport` (lines 70-74) to:

```python
@dataclass
class RebuildReport:
    item_rows: list[ItemRow]
    excluded_set: int
    excluded_zero: int
    excluded_bundle: int
```

- [ ] **Step 5: Surface `excluded_bundle` in `_format_product_tree`**

In `estimator_king/sync/engine.py`, change the `_format_product_tree` signature and head
logic (lines 100-108) to:

```python
def _format_product_tree(
    store_id: str, product_id: str, title: str, verb: str,
    rows: list[ItemRow], excluded_set: int, excluded_zero: int, excluded_bundle: int,
) -> str:
    n = len(rows)
    excluded = excluded_set + excluded_zero + excluded_bundle
    head = f'product {store_id}:{product_id} "{title}" ({verb}): {n} items'
    if excluded:
        head += (
            f", {excluded} excluded "
            f"(SET×{excluded_set}, ¥0×{excluded_zero}, bundle×{excluded_bundle})"
        )
```

(Leave the rest of the function — the `lines = [head]` loop — unchanged.)

- [ ] **Step 6: Add `bundle_set` to `sync_products` and thread it**

In `estimator_king/sync/engine.py`, add the param to `sync_products` (signature lines
127-139). After the `item_types_version: int,` line (line 137), add:

```python
    bundle_set: "BundleSetPolicy | None" = None,
```

At the `_rebuild_product_items(...)` call (lines 166-169), add `bundle_set` as the final
argument:

```python
                report = _rebuild_product_items(
                    snapshot, store_id, product_url, repository, embedder,
                    vector_store, typing_provider, talents, item_types, item_types_version,
                    bundle_set,
                )
```

Change the accumulation line (line 178) from:

```python
                result.excluded += report.excluded_set + report.excluded_zero
```

to:

```python
                result.excluded += (
                    report.excluded_set + report.excluded_zero + report.excluded_bundle
                )
```

Change the `_format_product_tree(...)` call (lines 185-187) from:

```python
                    logger.info(_format_product_tree(
                        store_id, str(snapshot.product_id), snapshot.title, verb,
                        rows, report.excluded_set, report.excluded_zero))
```

to:

```python
                    logger.info(_format_product_tree(
                        store_id, str(snapshot.product_id), snapshot.title, verb,
                        rows, report.excluded_set, report.excluded_zero,
                        report.excluded_bundle))
```

- [ ] **Step 7: Add `bundle_set` to `_rebuild_product_items` and unpack at `decompose_items`**

In `estimator_king/sync/engine.py`, add the param to `_rebuild_product_items` (signature
lines 212-223). After the `item_types_version: int,` line (line 222), add:

```python
    bundle_set: "BundleSetPolicy | None" = None,
```

Change the `decompose_items(...)` call (line 228) from:

```python
    decomposed = decompose_items(snapshot, talents=talents)
```

to:

```python
    from estimator_king.config_schema import BundleSetPolicy
    policy = bundle_set if bundle_set is not None else BundleSetPolicy()
    decomposed = decompose_items(
        snapshot, talents=talents,
        bundle_keywords=policy.keywords,
        bundle_price_ratio=policy.price_ratio,
        bundle_keep_keywords=policy.keep_keywords,
    )
```

Change the `return RebuildReport(...)` (lines 272-276) from:

```python
    return RebuildReport(
        item_rows=rows,
        excluded_set=decomposed.excluded_set,
        excluded_zero=decomposed.excluded_zero,
    )
```

to:

```python
    return RebuildReport(
        item_rows=rows,
        excluded_set=decomposed.excluded_set,
        excluded_zero=decomposed.excluded_zero,
        excluded_bundle=decomposed.excluded_bundle,
    )
```

- [ ] **Step 8: Update the head-string assertion in `test_engine_logging.py`**

Step 5 changed the head format to include `bundle×{excluded_bundle}`, so the existing
assertion in `tests/test_engine_logging.py` at line 89 (which pins the old two-field
format) must be updated. That test's snapshot has no bundle item, so `excluded_bundle`
is 0. Change line 89 from:

```python
    assert "2 excluded (SET×1, ¥0×1)" in msg
```

to:

```python
    assert "2 excluded (SET×1, ¥0×1, bundle×0)" in msg
```

- [ ] **Step 9: Run the new test + full engine suite to verify**

Run: `.venv/bin/python -m pytest tests/test_engine_items.py tests/test_engine_logging.py -v -o addopts=""`
Expected: PASS — `test_sync_excludes_bundle_option` passes and all existing engine tests
stay green (including the updated `test_engine_logging.py` head-string assertion).

- [ ] **Step 10: Type check + lint**

Run: `.venv/bin/basedpyright estimator_king/sync/engine.py`
Expected: 0 errors.
Run: `uvx ruff check estimator_king/sync/engine.py tests/test_engine_items.py tests/test_engine_logging.py`
Expected: no findings.

- [ ] **Step 11: Commit**

```bash
git add estimator_king/sync/engine.py tests/test_engine_items.py tests/test_engine_logging.py
git commit -m "feat(sync): thread bundle_set through engine and count exclusions"
```

---

## Task 4: Thread `bundle_set` through pipeline + cycle

**Files:**
- Modify: `estimator_king/crawler/async_pipeline.py` (TYPE_CHECKING import at lines 12-17;
  `async_process_queue` signature at lines 53-66; `sync_products(...)` call at lines 82-88)
- Modify: `estimator_king/crawler/cycle.py` (`async_process_queue(...)` call at lines 60-65)

- [ ] **Step 1: Add `BundleSetPolicy` to the pipeline's TYPE_CHECKING block**

In `estimator_king/crawler/async_pipeline.py`, extend the existing TYPE_CHECKING import
(lines 12-17) to include `BundleSetPolicy`:

```python
if TYPE_CHECKING:
    from estimator_king.config_schema import BundleSetPolicy, CrawlerPolicy, ProxyConfig
    from estimator_king.database.repository import ProductStateRepository
    from estimator_king.llm.embeddings import EmbeddingProvider
    from estimator_king.llm.typing_provider import TypingProvider
    from estimator_king.vectorstore.store import VectorStore
```

- [ ] **Step 2: Add the `bundle_set` param to `async_process_queue`**

In `estimator_king/crawler/async_pipeline.py`, in the `async_process_queue` signature
(lines 53-66), after `item_types_version: int,` (line 63), add:

```python
    bundle_set: "BundleSetPolicy | None" = None,
```

- [ ] **Step 3: Forward `bundle_set` to `sync_products`**

In `estimator_king/crawler/async_pipeline.py`, in the `sync_products(...)` call (lines
82-88), add `bundle_set=bundle_set` to the keyword args:

```python
                sync_result = await asyncio.to_thread(
                    sync_products, [(product_url, snapshot)], store_id,
                    state_repo, embedder, vector_store,
                    typing_provider=typing_provider, talents=talents,
                    item_types=item_types, item_types_version=item_types_version,
                    log_item_trees=log_item_trees, bundle_set=bundle_set,
                )
```

- [ ] **Step 4: Pass `config.bundle_set` from `cycle.py`**

In `estimator_king/crawler/cycle.py`, in the `async_process_queue(...)` call (lines
60-65), add `bundle_set=config.bundle_set`:

```python
                    result = await async_process_queue(
                        store.id, config.crawler, repo, embedder, vector_store,
                        typing_provider=typing_provider, talents=config.talents,
                        item_types=config.item_types,
                        item_types_version=config.item_types_version,
                        bundle_set=config.bundle_set,
                        log_item_trees=log_item_trees, proxy=config.proxy)
```

- [ ] **Step 5: Type check + lint + run dependent suites**

Run: `.venv/bin/basedpyright estimator_king/crawler/async_pipeline.py estimator_king/crawler/cycle.py`
Expected: 0 errors (signatures line up across the chain).
Run: `uvx ruff check estimator_king/crawler/async_pipeline.py estimator_king/crawler/cycle.py`
Expected: no findings.
Run: `.venv/bin/python -m pytest tests/test_async_pipeline.py tests/test_crawl_cycle.py tests/test_integration_async_pipeline.py -v -o addopts=""`
Expected: PASS (existing tests unaffected — `bundle_set` defaults to `None`, a no-op).

- [ ] **Step 6: Commit**

```bash
git add estimator_king/crawler/async_pipeline.py estimator_king/crawler/cycle.py
git commit -m "feat(crawler): forward bundle_set from cycle through async pipeline"
```

---

## Task 5: Enable in `stores_config.yaml` + force re-index

**Files:**
- Modify: `stores_config.yaml` (add `bundle_set:` block; bump `item_types_version`
  3 → 4 at line 169)

- [ ] **Step 1: Bump `item_types_version` to force a full re-index**

In `stores_config.yaml`, change line 169 from:

```yaml
item_types_version: 3
```

to:

```yaml
item_types_version: 4
```

- [ ] **Step 2: Add the `bundle_set` block**

In `stores_config.yaml`, after the `item_types_version: 4` line (line 169) and its
following blank line (line 170), insert:

```yaml
# Whole-set bundle options to exclude from priceable items. (A) any item whose name
# contains a `keywords` substring is dropped regardless of price; (B) any item whose
# name contains 「セット」, is not on `keep_keywords`, and is priced >= `price_ratio` ×
# the same-product median is also dropped (catches set bundles with no keyword).
bundle_set:
  keywords: [グッズセット, フルセット, 応援セット, 語セット]
  price_ratio: 5.0
  keep_keywords: [ステッカーセット, 缶バッジセット, クリアファイルセット, キーホルダーセット,
                  カードセット, ブロマイドセット, ポスターセット, ボイスセット,
                  カトラリーセット, ステーショナリーセット, チャームセット]
```

- [ ] **Step 3: Verify the real config loads with the new block**

Run:

```bash
.venv/bin/python -c "from estimator_king.config_schema import load_config; c = load_config('stores_config.yaml'); print(sorted(c.bundle_set.keywords)); print(c.bundle_set.price_ratio); print(c.item_types_version)"
```

Expected output (order of the sorted keywords is deterministic):

```
['グッズセット', 'フルセット', '応援セット', '語セット']
5.0
4
```

- [ ] **Step 4: Commit**

```bash
git add stores_config.yaml
git commit -m "feat(config): enable bundle_set exclusion and bump item_types_version"
```

---

## Final verification

- [ ] **Step 1: Full type check on all touched production files**

Run:

```bash
.venv/bin/basedpyright estimator_king/config_schema.py estimator_king/sync/items.py \
  estimator_king/sync/engine.py estimator_king/crawler/async_pipeline.py \
  estimator_king/crawler/cycle.py
```

Expected: 0 errors.

- [ ] **Step 2: Lint all touched files**

Run:

```bash
uvx ruff check estimator_king/ tests/
```

Expected: no findings.

- [ ] **Step 3: Full test suite**

Run: `.venv/bin/python -m pytest`
Expected: all pass (coverage gate per `pytest.ini`).

- [ ] **Step 4: Operational re-index (post-merge, manual)**

Because the decomposition output changed, the existing ChromaDB vectors for bundle items
are now stale. Re-index per CLAUDE.md:

```bash
set -a; source .env; set +a
.venv/bin/python -m estimator_king crawl --force-refetch
```

Then spot-check that bundle items are gone, e.g. confirm
`バースデーグッズセット` / `グッズセット` no longer appear as `item_name` in ChromaDB.
(The `item_types_version` bump means a normal `crawl` also re-indexes; `--force-refetch`
guarantees every product is re-fetched immediately.)
