# Estimate Anchor Floor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a config-driven, tiered deterministic anchor floor that raises `/estimate` suggested prices toward the same-type reference percentile, killing the ~−10% systematic low-balling without damaging cheap correct items.

**Architecture:** A pure post-processing step (`_anchor_floor`) runs in `Estimator.estimate_products` between `_reconcile` and `_snap_estimate`. Per estimate, keyed by the **original query line**, it computes a percentile of the same-type top_k reference prices and lifts `suggested` to it, guarded by four machine checks (min_refs sparse gate, small-sample median clamp, max_lift_ratio outlier cap, sentinel/その他/None no-op). Provenance is prepended to `rationale`. Floor is **off** unless `stores_config.yaml` contains an `anchor_floor` block. Two-stage rollout: implementation PR ships the code with the block absent (disabled); a later config-only commit enables it after the eval acceptance gate passes.

**Tech Stack:** Python 3, dataclasses, pydantic `ProductEstimate` (`model_copy`), pytest, basedpyright, ruff (via uvx).

**Spec:** [docs/superpowers/specs/2026-06-20-estimate-anchor-floor-design.md](../specs/2026-06-20-estimate-anchor-floor-design.md)

**Repo conventions (read before starting):**
- Verify after every change: `.venv/bin/basedpyright <paths>` (0 errors in `estimator_king/`), `uvx ruff check <paths>`, `.venv/bin/python -m pytest tests/test_estimator.py tests/test_config_schema.py -v -o addopts=""`.
- Commits go through the **git-master** skill (per repo CLAUDE.md). The `git commit` lines in this plan show the intended atomic grouping + message; execute them via git-master, adding only the listed paths (never `git add -A`).
- Test fakes live in [tests/test_estimator.py](../../../tests/test_estimator.py): `FakeEmbedder`, `FakeTypingProvider`, `RecordingVectorStore`, `FakeChat`, helpers `_hit`, `_est`, `_estimator`. Reuse them.

---

## File Structure

- `estimator_king/config_schema.py` — add `AnchorTier`, `AnchorFloorConfig` dataclasses + their `validate()`, parse `estimator.anchor_floor` in `from_yaml`, add `AppConfig.estimator_anchor_floor` field + call its validate in `AppConfig.validate`.
- `estimator_king/bot/estimator.py` — add `_percentile`, `_norm_kw`, `_anchor_floor` (module-level pure fns); make `_estimate_chunk` return `(EstimateBatch, dict[str, list[int]])`; wire floor into `estimate_products` with the alignment guard; add `anchor_floor` param to `Estimator.__init__`.
- `estimator_king/bot/runner.py` — pass `config.estimator_anchor_floor` into `Estimator`.
- `scripts/analysis/eval_estimate.py` — collect same-type prices in `build_context`, apply `_anchor_floor` in `run_once`, add a fail-closed acceptance-criteria exit.
- `scripts/analysis/experiment_anchor_floor.py` — read tiers/min_refs from config, report metrics bucketed by same-type ref count.
- `tests/test_estimator.py`, `tests/test_config_schema.py` — unit + integration tests.
- `docs/data-pipeline.md` — document the new stage.
- `stores_config.yaml` — **Task 9 only (stage-2 enablement, separate commit after eval passes).**

---

### Task 1: `_percentile` pure function

**Files:**
- Modify: `estimator_king/bot/estimator.py` (add module-level fn near `snap_to_tax_grid`, ~line 119)
- Test: `tests/test_estimator.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_estimator.py` (import update at top: `from estimator_king.bot.estimator import Estimator, snap_to_tax_grid, _snap_estimate, _percentile`):

```python
def test_percentile_linear_interpolation():
    assert _percentile([100, 200, 300, 400], 75) == 325.0
    assert _percentile([100, 200, 300, 400], 50) == 250.0
    assert _percentile([100, 200, 300, 400], 0) == 100.0
    assert _percentile([100, 200, 300, 400], 100) == 400.0


def test_percentile_single_and_empty():
    assert _percentile([500], 70) == 500.0
    assert _percentile([], 50) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_estimator.py::test_percentile_linear_interpolation -v -o addopts=""`
Expected: FAIL with `ImportError: cannot import name '_percentile'`.

- [ ] **Step 3: Write minimal implementation**

In `estimator_king/bot/estimator.py`, after `snap_to_tax_grid` (around line 119), add:

```python
def _percentile(values: list[int], pct: float) -> float | None:
    """Linear-interpolated percentile of `values` (pct in 0–100). None if empty."""
    s = sorted(values)
    if not s:
        return None
    if len(s) == 1:
        return float(s[0])
    pos = (pct / 100.0) * (len(s) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 < len(s):
        return s[lo] + (s[lo + 1] - s[lo]) * frac
    return float(s[lo])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -k percentile -v -o addopts=""`
Expected: PASS (2 tests).

- [ ] **Step 5: Type check + lint**

Run: `.venv/bin/basedpyright estimator_king/bot/estimator.py && uvx ruff check estimator_king/bot/estimator.py tests/test_estimator.py`
Expected: 0 errors in production code; ruff clean.

- [ ] **Step 6: Commit (via git-master)**

```bash
git add estimator_king/bot/estimator.py tests/test_estimator.py
git commit -m "feat(estimator): add _percentile helper for anchor floor"
```

---

### Task 2: `AnchorTier` / `AnchorFloorConfig` config dataclasses + parse + validate

**Files:**
- Modify: `estimator_king/config_schema.py` (add dataclasses near `BundleSetPolicy` ~line 90; add `AppConfig` field ~line 143; parse in `from_yaml` ~line 290–318; call validate in `AppConfig.validate` ~line 176)
- Test: `tests/test_config_schema.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config_schema.py` (reuse the file's existing YAML-loading helper; if it loads from a temp file, mirror that. The assertions below assume a helper `load_cfg(yaml_text)` returning an `AppConfig`; if the existing tests build YAML differently, follow that exact pattern):

```python
from estimator_king.config_schema import AnchorFloorConfig, AnchorTier
import pytest


_BASE_STORE = """
stores:
  - store_id: s
    base_url: https://example.com
    sitemap_url: https://example.com/sitemap.xml
"""


def test_anchor_floor_absent_is_none(load_cfg):
    cfg = load_cfg(_BASE_STORE)
    assert cfg.estimator_anchor_floor is None


def test_anchor_floor_parsed(load_cfg):
    cfg = load_cfg(_BASE_STORE + """
estimator:
  anchor_floor:
    general_percentile: 60
    min_refs: 3
    full_percentile_min_refs: 5
    max_lift_ratio: 1.6
    premium_tiers:
      - percentile: 70
        keywords: ["温感", "もこもこ"]
""")
    af = cfg.estimator_anchor_floor
    assert af is not None
    assert af.general_percentile == 60
    assert af.min_refs == 3
    assert af.full_percentile_min_refs == 5
    assert af.max_lift_ratio == 1.6
    assert af.premium_tiers[0].percentile == 70
    assert af.premium_tiers[0].keywords == ("温感", "もこもこ")


def test_anchor_floor_defaults(load_cfg):
    cfg = load_cfg(_BASE_STORE + """
estimator:
  anchor_floor:
    general_percentile: 60
""")
    af = cfg.estimator_anchor_floor
    assert af.min_refs == 3 and af.full_percentile_min_refs == 5
    assert af.max_lift_ratio == 1.6 and af.premium_tiers == ()


@pytest.mark.parametrize("bad", [
    "general_percentile: 120",
    "general_percentile: 60\n    min_refs: 0",
    "general_percentile: 60\n    full_percentile_min_refs: 2",
    "general_percentile: 60\n    max_lift_ratio: 0.5",
    "general_percentile: 60\n    premium_tiers:\n      - percentile: 70\n        keywords: []",
])
def test_anchor_floor_validate_rejects(load_cfg, bad):
    with pytest.raises(ValueError):
        load_cfg(_BASE_STORE + f"\nestimator:\n  anchor_floor:\n    {bad}\n")
```

If `tests/test_config_schema.py` has no `load_cfg` fixture, add one at the top of the file:

```python
import pytest
from estimator_king.config_schema import AppConfig


@pytest.fixture
def load_cfg(tmp_path):
    def _load(yaml_text: str) -> AppConfig:
        p = tmp_path / "stores_config.yaml"
        p.write_text(yaml_text, encoding="utf-8")
        return AppConfig.from_yaml(str(p))
    return _load
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_config_schema.py -k anchor_floor -v -o addopts=""`
Expected: FAIL with `ImportError: cannot import name 'AnchorFloorConfig'`.

- [ ] **Step 3: Add the dataclasses**

In `estimator_king/config_schema.py`, after `BundleSetPolicy` (after line 101), add:

```python
@dataclass(frozen=True)
class AnchorTier:
    """One premium anchor-floor tier: a percentile + the keywords that select it."""

    percentile: int
    keywords: tuple[str, ...]

    def validate(self):
        if not (0 <= self.percentile <= 100):
            raise ValueError("anchor_floor tier percentile must be 0–100")
        if not self.keywords or any(not isinstance(k, str) or not k for k in self.keywords):
            raise ValueError("anchor_floor tier keywords must be a non-empty list of non-empty strings")


@dataclass(frozen=True)
class AnchorFloorConfig:
    """Deterministic anchor-floor policy (see specs/2026-06-20-estimate-anchor-floor-design.md)."""

    general_percentile: int
    min_refs: int = 3
    full_percentile_min_refs: int = 5
    max_lift_ratio: float = 1.6
    premium_tiers: tuple[AnchorTier, ...] = ()

    def validate(self):
        if not (0 <= self.general_percentile <= 100):
            raise ValueError("anchor_floor.general_percentile must be 0–100")
        if self.min_refs < 1:
            raise ValueError("anchor_floor.min_refs must be >= 1")
        if self.full_percentile_min_refs < self.min_refs:
            raise ValueError("anchor_floor.full_percentile_min_refs must be >= min_refs")
        if self.max_lift_ratio < 1.0:
            raise ValueError("anchor_floor.max_lift_ratio must be >= 1.0")
        for tier in self.premium_tiers:
            tier.validate()
```

- [ ] **Step 4: Add the `AppConfig` field**

In `estimator_king/config_schema.py`, in the `AppConfig` dataclass after line 143 (`estimator_fetch_multiplier: int = 2`), add:

```python
    estimator_anchor_floor: "AnchorFloorConfig | None" = None
```

- [ ] **Step 5: Parse in `from_yaml`**

In `from_yaml`, after `est = yaml_data.get("estimator", {}) or {}` (line 290), add:

```python
    af = est.get("anchor_floor")
    anchor_floor = None
    if af:
        anchor_floor = AnchorFloorConfig(
            general_percentile=int(af["general_percentile"]),
            min_refs=int(af.get("min_refs", 3)),
            full_percentile_min_refs=int(af.get("full_percentile_min_refs", 5)),
            max_lift_ratio=float(af.get("max_lift_ratio", 1.6)),
            premium_tiers=tuple(
                AnchorTier(
                    percentile=int(t["percentile"]),
                    keywords=tuple(t.get("keywords", []) or []),
                )
                for t in (af.get("premium_tiers", []) or [])
            ),
        )
```

Then add to the `AppConfig(...)` constructor call (after `estimator_fetch_multiplier=...`, line 318):

```python
        estimator_anchor_floor=anchor_floor,
```

- [ ] **Step 6: Validate in `AppConfig.validate`**

In `AppConfig.validate` (after `self.bundle_set.validate()`, line 176), add:

```python
        if self.estimator_anchor_floor is not None:
            self.estimator_anchor_floor.validate()
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_config_schema.py -k anchor_floor -v -o addopts=""`
Expected: PASS (all anchor_floor tests).

- [ ] **Step 8: Type check + lint**

Run: `.venv/bin/basedpyright estimator_king/config_schema.py && uvx ruff check estimator_king/config_schema.py tests/test_config_schema.py`
Expected: 0 errors in production code; ruff clean.

- [ ] **Step 9: Commit (via git-master)**

```bash
git add estimator_king/config_schema.py tests/test_config_schema.py
git commit -m "feat(config): add AnchorFloorConfig schema, parsing and validation"
```

---

### Task 3: `_anchor_floor` pure function (all guards + range recompute + provenance)

**Files:**
- Modify: `estimator_king/bot/estimator.py` (add `_norm_kw`, `_anchor_floor` after `_percentile`; ensure `import unicodedata` at top)
- Test: `tests/test_estimator.py`

- [ ] **Step 1: Write the failing tests**

Update the import at the top of `tests/test_estimator.py` to include the new symbols and config types:

```python
from estimator_king.bot.estimator import (
    Estimator, snap_to_tax_grid, _snap_estimate, _percentile, _anchor_floor,
)
from estimator_king.config_schema import AnchorFloorConfig, AnchorTier
```

Add helpers + tests:

```python
def _est_full(name, suggested, lo, hi, conf="medium", rationale="r"):
    return ProductEstimate(
        product_name=name, suggested_price_jpy=suggested,
        price_range_jpy=PriceRange(min=lo, max=hi), confidence=conf,
        rationale=rationale, reference_products=[])


_CFG = AnchorFloorConfig(
    general_percentile=60, min_refs=3, full_percentile_min_refs=5, max_lift_ratio=1.6,
    premium_tiers=(AnchorTier(percentile=70, keywords=("温感", "もこもこ")),),
)


def test_anchor_floor_no_op_when_cfg_none():
    e = _est_full("x", 1000, 800, 1300)
    assert _anchor_floor("x", e, [1000, 1100, 1200, 1300, 1400], None) is e


def test_anchor_floor_no_op_sentinel():
    e = _est_full("x", 0, 0, 0, conf="low")
    assert _anchor_floor("x", e, [1000, 1100, 1200, 1300, 1400], _CFG) is e


def test_anchor_floor_no_op_sparse():
    e = _est_full("x", 1000, 800, 1300)
    # only 2 same-type refs < min_refs(3) -> untouched
    assert _anchor_floor("x", e, [3000, 4000], _CFG) is e


def test_anchor_floor_no_op_empty_refs():
    e = _est_full("x", 1000, 800, 1300)
    assert _anchor_floor("x", e, [], _CFG) is e


def test_anchor_floor_raises_to_general_percentile():
    # 5 refs (>= full_percentile_min_refs) -> full p60; sorted [2000..4000], p60=3200
    e = _est_full("ポーチ", 2200, 1800, 2900)
    out = _anchor_floor("ポーチ", e, [2000, 2500, 3000, 3500, 4000], _CFG)
    assert out.suggested_price_jpy == 3200
    assert out.suggested_price_jpy > e.suggested_price_jpy
    assert out.rationale.startswith("[anchor floor:")


def test_anchor_floor_never_lowers():
    e = _est_full("x", 5000, 4000, 6000)
    out = _anchor_floor("x", e, [2000, 2500, 3000, 3500, 4000], _CFG)
    assert out is e  # floor (3200) < suggested -> untouched


def test_anchor_floor_premium_uses_higher_tier():
    # query carries 温感; n=5 -> p70. p70 of [2000..4000] = 3600 > p60 3200
    e = _est_full("温感マグカップ", 2200, 1800, 2900)
    out = _anchor_floor("温感マグカップ", e, [2000, 2500, 3000, 3500, 4000], _CFG)
    assert out.suggested_price_jpy == 3600


def test_anchor_floor_premium_keyed_on_query_not_product_name():
    # query has 温感, but the (model-rewritten) product_name does not
    e = _est_full("rewritten name", 2200, 1800, 2900)
    out = _anchor_floor("温感マグカップ", e, [2000, 2500, 3000, 3500, 4000], _CFG)
    assert out.suggested_price_jpy == 3600


def test_anchor_floor_keyword_nfkc_variant_hits_tier():
    # half-width katakana モコモコ should still match full-width もこもこ
    e = _est_full("ﾓｺﾓｺぬいぐるみ", 2200, 1800, 2900)
    out = _anchor_floor("ﾓｺﾓｺぬいぐるみ", e, [2000, 2500, 3000, 3500, 4000], _CFG)
    assert out.suggested_price_jpy == 3600


def test_anchor_floor_small_sample_clamped_to_median():
    # n=3 (>= min_refs, < full_percentile_min_refs=5) -> effective_pct clamped to 50
    # even though 温感 would request p70. median of [2000,3000,5000] = 3000.
    e = _est_full("温感マグカップ", 2200, 1800, 2900)
    out = _anchor_floor("温感マグカップ", e, [2000, 3000, 5000], _CFG)
    assert out.suggested_price_jpy == 3000


def test_anchor_floor_max_lift_ratio_no_op():
    # floor would be 9000 (>5 refs, p60), suggested 2200; 2200*1.6=3520 < 9000 -> skip
    e = _est_full("x", 2200, 1800, 2900)
    out = _anchor_floor("x", e, [8000, 8500, 9000, 9500, 10000], _CFG)
    assert out is e


def test_anchor_floor_recomputes_range_with_upward_skew():
    e = _est_full("ポーチ", 2200, 1800, 2900, conf="medium")
    out = _anchor_floor("ポーチ", e, [2000, 2500, 3000, 3500, 4000], _CFG)
    # medium skew (-25%/+45%) around 3200; max widened, min not above suggested
    assert out.price_range_jpy.max >= round(3200 * 1.45)
    assert out.price_range_jpy.min <= out.suggested_price_jpy


def test_anchor_floor_does_not_mutate_original():
    e = _est_full("ポーチ", 2200, 1800, 2900)
    _anchor_floor("ポーチ", e, [2000, 2500, 3000, 3500, 4000], _CFG)
    assert e.suggested_price_jpy == 2200 and e.rationale == "r"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -k anchor_floor -v -o addopts=""`
Expected: FAIL with `ImportError: cannot import name '_anchor_floor'`.

- [ ] **Step 3: Write the implementation**

In `estimator_king/bot/estimator.py`: add `import unicodedata` to the imports at the top (alongside the existing stdlib imports). Then after `_percentile`, add:

```python
_SKEW = {"high": (0.20, 0.30), "medium": (0.25, 0.45), "low": (0.30, 0.60)}


def _norm_kw(s: str) -> str:
    """NFKC + casefold, for width/case-insensitive premium-keyword matching."""
    return unicodedata.normalize("NFKC", s).casefold()


def _anchor_floor(query: str, est: ProductEstimate, same_type_prices: list[int],
                  cfg: "AnchorFloorConfig | None") -> ProductEstimate:
    """Raise est.suggested toward a percentile of same-type refs, keyed by the
    original `query`. No-op (returns est unchanged) when disabled, on the no-estimate
    sentinel, on sparse refs, on an over-large lift, or when the floor is below
    suggested. On a real lift it recomputes the range (upward skew) and prepends a
    provenance note to rationale. See specs/2026-06-20-estimate-anchor-floor-design.md."""
    if cfg is None or est.suggested_price_jpy == 0 or not same_type_prices:
        return est
    n = len(same_type_prices)
    if n < cfg.min_refs:
        return est
    nq = _norm_kw(query)
    effective = cfg.general_percentile
    for tier in cfg.premium_tiers:
        if any(_norm_kw(kw) in nq for kw in tier.keywords):
            effective = max(effective, tier.percentile)
    if n < cfg.full_percentile_min_refs:
        effective = min(effective, 50)
    floor_value = _percentile(same_type_prices, effective)
    if floor_value is None:
        return est
    floor_int = round(floor_value)
    suggested = est.suggested_price_jpy
    if floor_int <= suggested:
        return est
    if floor_int > round(suggested * cfg.max_lift_ratio):
        logger.info("anchor_floor skip lift>%.2f: %r %d->%d @p%d n=%d",
                    cfg.max_lift_ratio, query, suggested, floor_int, effective, n)
        return est
    down, up = _SKEW.get(est.confidence, _SKEW["medium"])
    new_min = min(est.price_range_jpy.min, round(floor_int * (1 - down)))
    new_max = max(est.price_range_jpy.max, round(floor_int * (1 + up)))
    note = f"[anchor floor: ¥{suggested}→¥{floor_int} @p{effective}, n={n}] "
    logger.info("anchor_floor applied: %r %d->%d @p%d n=%d",
                query, suggested, floor_int, effective, n)
    return est.model_copy(update={
        "suggested_price_jpy": floor_int,
        "price_range_jpy": PriceRange(min=new_min, max=new_max),
        "rationale": note + est.rationale,
    })
```

Add a `TYPE_CHECKING` import for the annotation at the top of the file (so there is no import cycle at runtime):

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from estimator_king.config_schema import AnchorFloorConfig
```

(If `from typing import ...` already exists, add `TYPE_CHECKING` to it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -k anchor_floor -v -o addopts=""`
Expected: PASS (all `anchor_floor` unit tests).

- [ ] **Step 5: Type check + lint**

Run: `.venv/bin/basedpyright estimator_king/bot/estimator.py && uvx ruff check estimator_king/bot/estimator.py tests/test_estimator.py`
Expected: 0 errors in production code; ruff clean.

- [ ] **Step 6: Commit (via git-master)**

```bash
git add estimator_king/bot/estimator.py tests/test_estimator.py
git commit -m "feat(estimator): add _anchor_floor with sparse/clamp/outlier guards"
```

---

### Task 4: Wire the floor into the estimate pipeline (chunk prices, alignment guard, Estimator param, runner)

**Files:**
- Modify: `estimator_king/bot/estimator.py` (`_estimate_chunk` return type ~line 199–226; `estimate_products` ~line 186–197; `Estimator.__init__` ~line 161–177)
- Modify: `estimator_king/bot/runner.py:47-55`
- Test: `tests/test_estimator.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_estimator.py` (these go through the real pipeline; the floor is enabled by passing `anchor_floor=_CFG` to a new `_estimator` kwarg):

```python
def _estimator_af(vs, chat, anchor_floor, typing=None, item_type="ぬいぐるみ"):
    return Estimator(FakeEmbedder(), chat, vs, typing_provider=(typing or FakeTypingProvider(item_type)),
                     item_types=[item_type], item_types_version=1,
                     top_k=10, recency_weight=0.05, diversity_weight=0.0, fetch_multiplier=1,
                     anchor_floor=anchor_floor)


def test_pipeline_floor_raises_low_estimate():
    hits = [_hit(f"r{i}", "ぬいぐるみ", p, 0, 0.1)
            for i, p in enumerate([2000, 2500, 3000, 3500, 4000])]
    vs = RecordingVectorStore(hits)
    chat = FakeChat([_est_full("ぬいぐるみ", 2200, 1800, 2900)])
    est = _estimator_af(vs, chat, _CFG, typing=FakeTypingProvider("ぬいぐるみ"))
    batch = est.estimate_products(["ぬいぐるみ"], "u")
    out = batch.estimates[0]
    assert out.suggested_price_jpy == 3190  # 3200 floor snapped to ¥110 grid
    assert "anchor floor" in out.rationale


def test_pipeline_floor_disabled_when_no_config():
    hits = [_hit(f"r{i}", "ぬいぐるみ", p, 0, 0.1)
            for i, p in enumerate([2000, 2500, 3000, 3500, 4000])]
    vs = RecordingVectorStore(hits)
    chat = FakeChat([_est_full("ぬいぐるみ", 2200, 1800, 2900)])
    est = _estimator_af(vs, chat, None, typing=FakeTypingProvider("ぬいぐるみ"))
    out = est.estimate_products(["ぬいぐるみ"], "u").estimates[0]
    assert out.suggested_price_jpy == 2200  # only snapped, not floored
    assert "anchor floor" not in out.rationale


def test_pipeline_floor_skipped_on_length_mismatch(caplog):
    import logging
    hits = [_hit(f"r{i}", "ぬいぐるみ", p, 0, 0.1)
            for i, p in enumerate([2000, 2500, 3000, 3500, 4000])]
    vs = RecordingVectorStore(hits)
    chat = FakeChat([_est_full("ぬいぐるみ", 2200, 1800, 2900)])
    est = _estimator_af(vs, chat, _CFG, typing=FakeTypingProvider("ぬいぐるみ"))
    # Force a reconcile that returns the wrong length.
    est._reconcile = lambda names, ests: []  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        batch = est.estimate_products(["ぬいぐるみ"], "u")
    assert any("anchor_floor skipped" in r.message for r in caplog.records)
    assert batch.estimates == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -k "pipeline_floor" -v -o addopts=""`
Expected: FAIL — `Estimator.__init__` has no `anchor_floor` kwarg.

- [ ] **Step 3: Add the `anchor_floor` constructor param**

In `estimator_king/bot/estimator.py`, `Estimator.__init__` signature (line 161-166), add a keyword-only param and store it. Change the signature tail from:

```python
                 fetch_multiplier: int = 2) -> None:
```

to:

```python
                 fetch_multiplier: int = 2,
                 anchor_floor: "AnchorFloorConfig | None" = None) -> None:
```

and after `self._fetch_multiplier = fetch_multiplier` (line 176) add:

```python
        self._anchor_floor = anchor_floor
```

- [ ] **Step 4: Make `_estimate_chunk` return same-type prices**

In `_estimate_chunk` (line 199-226), change it to also collect and return per-query same-type prices. Replace the body so it builds a `prices_by_name` dict and returns a tuple:

```python
    def _estimate_chunk(self, chunk: list[str]) -> tuple[EstimateBatch, dict[str, list[int]]]:
        context_blocks: list[str] = []
        prices_by_name: dict[str, list[int]] = {}
        for name in chunk:
            embedding = self._embedder.embed_query(name)
            types = classify_query(
                name, item_types=self._item_types,
                item_types_version=self._item_types_version,
                typing_provider=self._typing_provider, repository=None,
            )
            type_set = set(types)
            merged: dict[str, _Hit] = {}
            queries: list[dict[str, Any] | None] = [{"item_type": t} for t in types]
            queries.append(None)  # always one plain query
            fetch_n = self._top_k * self._fetch_multiplier
            for where in queries:
                for hit in self._vector_store.query(embedding, fetch_n, where=where):
                    prev = merged.get(hit.id)
                    if prev is None or hit.distance < prev.distance:
                        merged[hit.id] = hit
            ranked = self._rerank(list(merged.values()))[: self._top_k]
            prices_by_name[normalize_text(name)] = [
                int(h.metadata.get("price_jpy", 0) or 0)
                for h in ranked
                if str(h.metadata.get("item_type", "") or "") in type_set
                and int(h.metadata.get("price_jpy", 0) or 0) > 0
            ]
            refs = "\n".join(self._format_reference(h) for h in ranked)
            context_blocks.append(f"### Query: {name}\n{refs or '(no matches)'}")
        user_prompt = (
            "Products to estimate (one per line):\n"
            + "\n".join(chunk)
            + "\n\nReference context:\n"
            + "\n\n".join(context_blocks)
        )
        return self._chat.estimate(SYSTEM_PROMPT, user_prompt), prices_by_name
```

- [ ] **Step 5: Wire floor into `estimate_products`**

In `estimate_products` (line 186-197), replace the chunk loop + post-processing with:

```python
        all_estimates: list[ProductEstimate] = []
        prices_by_name: dict[str, list[int]] = {}
        for start_idx in range(0, len(product_names), self.CHUNK_SIZE):
            chunk = product_names[start_idx:start_idx + self.CHUNK_SIZE]
            logger.debug("chunk %d/%d: %d products",
                         start_idx // self.CHUNK_SIZE + 1, total_chunks, len(chunk))
            batch, chunk_prices = self._estimate_chunk(chunk)
            all_estimates.extend(batch.estimates)
            for k, v in chunk_prices.items():
                prices_by_name.setdefault(k, v)
        reconciled = self._reconcile(product_names, all_estimates)
        if self._anchor_floor is not None and len(reconciled) == len(product_names):
            reconciled = [
                _anchor_floor(line, e,
                              prices_by_name.get(normalize_text(line), []),
                              self._anchor_floor)
                for line, e in zip(product_names, reconciled)
            ]
        elif self._anchor_floor is not None:
            logger.error("anchor_floor skipped: reconcile len %d != names %d",
                         len(reconciled), len(product_names))
        reconciled = [_snap_estimate(est) for est in reconciled]
```

- [ ] **Step 6: Pass config through `runner.py`**

In `estimator_king/bot/runner.py`, the `Estimator(...)` call (line 47-55), add the new kwarg after `fetch_multiplier=config.estimator_fetch_multiplier,`:

```python
        anchor_floor=config.estimator_anchor_floor,
```

- [ ] **Step 7: Run the full estimator suite**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -v -o addopts=""`
Expected: PASS — new pipeline_floor tests pass AND all pre-existing tests stay green (floor off by default in `_estimator`).

- [ ] **Step 8: Type check + lint**

Run: `.venv/bin/basedpyright estimator_king/bot/estimator.py estimator_king/bot/runner.py && uvx ruff check estimator_king/bot/estimator.py estimator_king/bot/runner.py tests/test_estimator.py`
Expected: 0 errors in production code; ruff clean.

- [ ] **Step 9: Commit (via git-master)**

```bash
git add estimator_king/bot/estimator.py estimator_king/bot/runner.py tests/test_estimator.py
git commit -m "feat(estimator): apply anchor floor in estimate_products with alignment guard"
```

---

### Task 5: Provenance survives Discord rationale truncation

**Files:**
- Test: `tests/test_commands.py` (create if absent; otherwise add to it)

The provenance is **prepended** in `_anchor_floor` (Task 3), so it lands inside the formatter's 297-char window. This task locks that with a test against the real formatter; no production code change is expected. If the test fails, the fix is in `_anchor_floor` (keep prepending) — do **not** change the truncation in `commands.py`.

- [ ] **Step 1: Identify the formatter entry point**

Run: `grep -n "def .*embed\|rationale\[:297\]\|def format" estimator_king/bot/commands.py`
Expected: shows the embed-formatting function (around line 37) and the truncation at line 62-63. Note the function name and signature for the test below.

- [ ] **Step 2: Write the failing test**

Create `tests/test_commands.py` (or append). Replace `<FORMAT_FN>` and its call signature with the actual function found in Step 1:

```python
from estimator_king.bot.commands import <FORMAT_FN>
from estimator_king.llm.chat import EstimateBatch, ProductEstimate, PriceRange


def test_floor_provenance_survives_rationale_truncation():
    long_tail = "あ" * 400  # model rationale well over the 300-char cap
    est = ProductEstimate(
        product_name="ポーチ",
        suggested_price_jpy=3190,
        price_range_jpy=PriceRange(min=2420, max=4620),
        confidence="medium",
        rationale="[anchor floor: ¥2200→¥3190 @p60, n=6] " + long_tail,
        reference_products=[],
    )
    embeds = <FORMAT_FN>(EstimateBatch(estimates=[est]))
    rendered = " ".join(e.description for e in embeds)
    assert "anchor floor" in rendered
```

- [ ] **Step 3: Run test to verify it passes (prepend already places it in-window)**

Run: `.venv/bin/python -m pytest tests/test_commands.py -v -o addopts=""`
Expected: PASS. (If it FAILS, the provenance was appended, not prepended — fix `_anchor_floor` in Task 3 to prepend.)

- [ ] **Step 4: Type check + lint**

Run: `.venv/bin/basedpyright tests/test_commands.py 2>/dev/null; uvx ruff check tests/test_commands.py`
Expected: ruff clean. (Test-file pyright noise from duck-typed fakes is acceptable per repo convention.)

- [ ] **Step 5: Commit (via git-master)**

```bash
git add tests/test_commands.py
git commit -m "test(bot): assert anchor-floor provenance survives rationale truncation"
```

---

### Task 6: eval_estimate.py applies the floor + fail-closed acceptance gate

**Files:**
- Modify: `scripts/analysis/eval_estimate.py`

- [ ] **Step 1: Collect same-type prices in `build_context`**

In `scripts/analysis/eval_estimate.py`, `build_context` currently returns `(context_block, excluded_self_descriptions)`. Extend it to also return the same-type prices from the final `ranked` list (mirroring `experiment_anchor_floor.py`). After `ranked = est._rerank(list(merged.values()))[: est._top_k]`, add:

```python
    type_set = set(types)
    same_type_prices = [
        int(h.metadata.get("price_jpy", 0) or 0)
        for h in ranked
        if str(h.metadata.get("item_type", "") or "") in type_set
        and int(h.metadata.get("price_jpy", 0) or 0) > 0
    ]
```

and change the return to `return f"### Query: {query}\n{refs or '(no matches)'}", list(selves.values()), same_type_prices`. Update the function's return type annotation to `tuple[str, list[str], list[int]]`.

- [ ] **Step 2: Apply `_anchor_floor` in `run_once`**

In `run_once`, where it currently does `block, selves = build_context(...)`, change to capture prices, and after the chat returns each `est_obj`, apply the floor with the same config the production Estimator uses, before `_snap_estimate`. Concretely:

- Import: add `_anchor_floor` to the existing `from estimator_king.bot.estimator import ...` line.
- In the per-query build loop, capture `block, selves, stp = build_context(est, query, official)` and stash `stp` in a dict keyed by `normalize_text(query)`.
- In the per-query result loop, replace `snapped = _snap_estimate(est_obj)` with:

```python
                floored = _anchor_floor(query, est_obj,
                                        stp_by_query.get(normalize_text(query), []),
                                        config.estimator_anchor_floor)
                snapped = _snap_estimate(floored)
```

where `config` is in scope from `main()` (pass it into `run_once`, or read `est`'s config). Since `run_once(est)` does not currently receive `config`, change its signature to `run_once(est, anchor_floor)` and pass `config.estimator_anchor_floor` from `main()`; use `anchor_floor` in the `_anchor_floor` call. This makes baseline (anchor_floor absent in config → None) vs candidate (config has the block) an explicit disabled-vs-enabled comparison.

- [ ] **Step 3: Add the fail-closed acceptance gate**

At the end of `main()`, after the SUMMARY block is printed, add an acceptance check that exits non-zero when criteria fail. Insert before the final provenance print (so provenance still shows on failure, then exit):

```python
    # Acceptance gate (only meaningful when anchor_floor is enabled in config).
    if config.estimator_anchor_floor is not None:
        mean_signed = statistics.mean(signed_vals) if signed_vals else 0.0
        failures = []
        # signed err magnitude must be materially better than the known ~-10% baseline.
        if mean_signed <= -7.0:
            failures.append(f"mean signed err {mean_signed:+.1f}% not improved (want > -7%)")
        if failures:
            print("\n========== ACCEPTANCE: FAIL ==========", file=sys.stderr)
            for f in failures:
                print(f"  - {f}", file=sys.stderr)
            sys.exit(3)
        print("\n========== ACCEPTANCE: PASS ==========")
```

> NOTE: This gate runs the **enabled** config. The operator runs eval twice — once with `anchor_floor` absent (baseline numbers) and once present (candidate + gate) — and records both in the stage-2 commit (Task 9). The MAPE/coverage/no-estimate comparisons remain a human read of the two stdout blocks per the spec; the machine gate enforces the primary signed-error criterion fail-closed so a regressed candidate cannot exit 0.

- [ ] **Step 4: Verify the script still type-checks, lints, and imports**

Run: `.venv/bin/basedpyright scripts/analysis/eval_estimate.py && uvx ruff check scripts/analysis/eval_estimate.py`
Expected: 0 errors in production code (`estimator_king/`); ruff clean. (basedpyright may emit `reportPrivateUsage` on `est._rerank` etc.; the file already carries `# pyright: reportPrivateUsage=false`.)

- [ ] **Step 5: Commit (via git-master)**

```bash
git add scripts/analysis/eval_estimate.py
git commit -m "feat(eval): apply anchor floor and add fail-closed acceptance gate"
```

---

### Task 7: experiment_anchor_floor.py reads config tiers + reports bucketed by ref count

**Files:**
- Modify: `scripts/analysis/experiment_anchor_floor.py`

- [ ] **Step 1: Drive the floor from config, not the hard-coded sweep**

In `scripts/analysis/experiment_anchor_floor.py`, replace the hard-coded `PREMIUM_KW` + single-percentile sweep machinery so it applies the real `_anchor_floor` with `config.estimator_anchor_floor`. Import `_anchor_floor` from `estimator_king.bot.estimator`. `build_context` already returns `same_type_prices`; keep that. For each fixture, compute the floored estimate via `_anchor_floor(query, est_obj, same_type_prices, config.estimator_anchor_floor)` then `_snap_estimate`.

- [ ] **Step 2: Report metrics bucketed by same-type ref count**

Add buckets `n<min_refs (skipped)`, `n in [min_refs, full_percentile_min_refs)`, `n in [full_percentile_min_refs, ...)`. For each bucket print: bucket label, **bucket N** (fixtures landing there), **skipped-by-min_refs count**, **floor-applied count**, mean signed err, MAPE, and a **pass/fail** marker (`floor-applied count >= 5` = powered). This output feeds the §5 stage-2 gate decision. Keep the per-fixture table for eyeballing.

```python
def bucket_of(n: int, cfg) -> str:
    if cfg is None or n < cfg.min_refs:
        return "skipped(<min_refs)"
    if n < cfg.full_percentile_min_refs:
        return f"small[{cfg.min_refs},{cfg.full_percentile_min_refs})"
    return f"full[>={cfg.full_percentile_min_refs}]"
```

Accumulate per-bucket lists of (signed%, abs%, applied?) and print the summary described above. `MIN_BUCKET_N = 5`.

- [ ] **Step 3: Verify type-check, lint, and a dry run**

Run: `.venv/bin/basedpyright scripts/analysis/experiment_anchor_floor.py && uvx ruff check scripts/analysis/experiment_anchor_floor.py`
Expected: 0 errors in production code; ruff clean.

(A live run needs `.env` + chroma and chat API; it is run during stage-2 calibration, not in this task's verification.)

- [ ] **Step 4: Commit (via git-master)**

```bash
git add scripts/analysis/experiment_anchor_floor.py
git commit -m "feat(analysis): drive anchor-floor experiment from config, report by ref-count bucket"
```

---

### Task 8: Sync docs/data-pipeline.md

**Files:**
- Modify: `docs/data-pipeline.md`

- [ ] **Step 1: Locate the reconcile→snap region**

Run: `grep -n "reconcile\|_snap_estimate\|snap_to_tax_grid\|chat-estimate" docs/data-pipeline.md`
Expected: find the query-path stage covering reconcile + snap (stages 13–14 region).

- [ ] **Step 2: Insert the anchor-floor stage**

Between the reconcile and snap stages, add a stage entry documenting `_anchor_floor` with: the controlling config key (`estimator.anchor_floor`), the function reference (`estimator_king/bot/estimator.py` `_anchor_floor`), and the mechanics — keyed by original query; NFKC+casefold keyword match; `min_refs` sparse no-op; `full_percentile_min_refs` small-sample median clamp; `effective_pct = max(general, matched tiers)`; `max_lift_ratio` outlier no-op; raise-only; range recomputed with confidence-based upward skew; rationale-prefixed provenance; その他/sentinel/None no-op; alignment length guard fail-closed. Match the existing doc's table/format (limit · config key · function · "why"). Note the two-stage rollout (disabled by default; enabled via config after the eval gate).

- [ ] **Step 3: Commit (via git-master)**

```bash
git add docs/data-pipeline.md
git commit -m "docs(data-pipeline): document anchor-floor post-processing stage"
```

---

### Task 9: Stage-2 enablement (separate commit, AFTER the eval gate passes)

> **Do NOT do this in the implementation PR.** This is the second rollout stage. Run it only after Tasks 1–8 are merged and the calibration/eval gate passes. This task changes `stores_config.yaml` only.

- [ ] **Step 1: Run the calibration experiment**

Run: `set -a; source .env; set +a; PYTHONPATH=. .venv/bin/python scripts/analysis/experiment_anchor_floor.py --runs 3`
Inspect the per-bucket report. Confirm the `small[...]` and `full[...]` buckets that the chosen `general/premium/min_refs/full_percentile_min_refs` would activate are **powered** (floor-applied count ≥ 5) and not regressing. If a small bucket is empty/underpowered, raise `full_percentile_min_refs` (or `min_refs`) so those cases stay clamped/no-op.

- [ ] **Step 2: Run the baseline (disabled) eval**

With `anchor_floor` **absent** from `stores_config.yaml`:
Run: `set -a; source .env; set +a; PYTHONPATH=. .venv/bin/python scripts/analysis/eval_estimate.py --runs 3 | tee /tmp/eval_baseline.txt`
Record MAPE / mean signed err / coverage / no-estimate set.

- [ ] **Step 3: Add the config block**

Add to `stores_config.yaml` under `estimator:` (use the values confirmed in Step 1):

```yaml
  anchor_floor:
    general_percentile: 60
    min_refs: 3
    full_percentile_min_refs: 5
    max_lift_ratio: 1.6
    premium_tiers:
      - percentile: 70
        keywords: ["温感", "もこもこ", "あったか", "なりきり"]
```

- [ ] **Step 4: Run the candidate (enabled) eval gate**

Run: `set -a; source .env; set +a; PYTHONPATH=. .venv/bin/python scripts/analysis/eval_estimate.py --runs 3 | tee /tmp/eval_candidate.txt`
Expected: exits 0 with `ACCEPTANCE: PASS`. Compare `/tmp/eval_candidate.txt` vs `/tmp/eval_baseline.txt`: signed err materially less negative; MAPE not worse beyond +2pp; coverage not worse; no-estimate a subset. If the gate exits non-zero, adjust percentiles and repeat — do **not** commit.

- [ ] **Step 5: Commit the enablement with evidence (via git-master)**

```bash
git add stores_config.yaml
git commit  # message body: paste baseline-vs-candidate signed/MAPE/coverage/no-estimate numbers
```

Message summarizes: "feat(config): enable anchor_floor" + the before/after acceptance data proving the gate passed.

- [ ] **Step 6: Restart the bot** to load the enabled config. Rollback at any time = remove the `anchor_floor` block + restart.

---

## Done criteria

- Tasks 1–8 merged: code present, floor **disabled** (no `anchor_floor` in `stores_config.yaml`), full suite green, type-check + lint clean, docs synced.
- Task 9 (separate commit): eval acceptance gate passed, config block added with recorded before/after data, bot restarted.
