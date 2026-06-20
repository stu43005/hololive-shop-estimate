# Estimate Anchor Floor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a config-driven, tiered deterministic anchor floor that raises `/estimate` suggested prices toward the same-type reference percentile, killing the ~−10% systematic low-balling without damaging cheap correct items.

**Architecture:** A pure post-processing step (`_anchor_floor`) runs in `Estimator.estimate_products` between `_reconcile` and `_snap_estimate`. Per estimate, keyed by the **original query line**, it computes a percentile of the same-type top_k reference prices and lifts `suggested` to it, guarded by four machine checks (min_refs sparse gate, small-sample median clamp, max_lift_ratio outlier cap, sentinel/その他/None no-op). Provenance is prepended to `rationale`. Floor is **off** unless `stores_config.yaml` contains an `anchor_floor` block. Two-stage rollout: implementation PR ships the code with the block absent (disabled); a later config-only commit enables it after the eval acceptance gate passes.

**Tech Stack:** Python 3, dataclasses, pydantic `ProductEstimate` (`model_copy`), pytest, basedpyright, ruff (via uvx).

**Spec:** `docs/superpowers/specs/2026-06-20-estimate-anchor-floor-design.md`

**Repo facts (verified, use exactly):**
- Config loader is `load_config(path)` in `estimator_king/config_schema.py` (`AppConfig.from_yaml` just delegates to it). Store entries use key `id` (not `store_id`).
- Existing `tests/test_config_schema.py` uses `from estimator_king.config_schema import load_config` + a `_write_yaml(tmp_path, body)` helper + `monkeypatch.setenv("OPENAI_API_KEY", "k")`. Reuse that pattern.
- Discord formatter is `format_estimates(batch, max_length=2000)` in `estimator_king/bot/commands.py`; it truncates each rationale to 297 chars + "..." (line ~62). Its test file is `tests/test_bot_commands.py`.
- `estimator.py` already imports `normalize_text` (used by `_reconcile`).
- NFKC + casefold unifies full/half-width **latin/digits** and case (e.g. `ＢＩＧ`→`big`) but does **not** convert katakana→hiragana (`ﾓｺﾓｺ`→`モコモコ`, not `もこもこ`). Tests must reflect this.

**Verification after every change:**
`.venv/bin/basedpyright <paths>` (0 errors in `estimator_king/`), `uvx ruff check <paths>`, `.venv/bin/python -m pytest tests/test_estimator.py tests/test_config_schema.py tests/test_bot_commands.py -v -o addopts=""`.

**Commit discipline:** commits go through the **git-master** skill (repo CLAUDE.md). The `git commit` lines below show intended atomic grouping + message; execute via git-master adding only the listed paths (never `git add -A`).

---

## File Structure

- `estimator_king/config_schema.py` — `AnchorTier`, `AnchorFloorConfig` dataclasses + `validate()`; `_req_int`/`_req_num` parse guards; parse `estimator.anchor_floor` in `load_config`; `AppConfig.estimator_anchor_floor` field + validate call.
- `estimator_king/bot/estimator.py` — `_percentile`, `_norm_kw`, `_anchor_floor`; `_estimate_chunk` returns `(EstimateBatch, dict[str, list[int]])`; floor wired into `estimate_products` with alignment guard; `Estimator.__init__` `anchor_floor` param.
- `estimator_king/bot/runner.py` — pass `config.estimator_anchor_floor`.
- `scripts/analysis/eval_estimate.py` — collect same-type prices; one chat pass; compute paired baseline(no-floor)-vs-candidate(floor) metrics; fail-closed acceptance gate.
- `scripts/analysis/experiment_anchor_floor.py` — build a **candidate** `AnchorFloorConfig` from CLI args (defaults = spec starting values); apply real `_anchor_floor`; report paired baseline-vs-candidate metrics bucketed by same-type ref count with pass/fail.
- `tests/test_estimator.py`, `tests/test_config_schema.py`, `tests/test_bot_commands.py` — tests.
- `docs/data-pipeline.md` — document the stage.
- `stores_config.yaml` — **Task 9 only** (stage-2 enablement, separate commit after eval passes).

---

### Task 1: `_percentile` pure function

**Files:**
- Modify: `estimator_king/bot/estimator.py` (add after `snap_to_tax_grid`, ~line 119)
- Test: `tests/test_estimator.py`

- [ ] **Step 1: Write the failing test**

Update the import at the top of `tests/test_estimator.py`:
`from estimator_king.bot.estimator import Estimator, snap_to_tax_grid, _snap_estimate, _percentile`

Add:

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

Run: `.venv/bin/python -m pytest tests/test_estimator.py -k percentile -v -o addopts=""`
Expected: collection error / FAIL — `cannot import name '_percentile'`.

- [ ] **Step 3: Write minimal implementation**

In `estimator_king/bot/estimator.py`, after `snap_to_tax_grid` (~line 119), add:

```python
def _percentile(values: list[int], pct: float) -> float | None:
    """Linear-interpolated percentile of `values` (pct in 0-100). None if empty."""
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
- Modify: `estimator_king/config_schema.py` (dataclasses after `BundleSetPolicy` ~line 101; `AppConfig` field ~line 143; parse in `load_config` ~line 290; validate call ~line 176)
- Test: `tests/test_config_schema.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config_schema.py` (reuse the file's `_write_yaml` helper and `load_config`):

```python
import pytest
from estimator_king.config_schema import AnchorFloorConfig, AnchorTier, load_config

_STORE = """
        stores:
          - id: s
            base_url: https://x
            sitemap_url: https://x/sitemap.xml
"""


def _load(tmp_path, monkeypatch, body):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    return load_config(_write_yaml(tmp_path, _STORE + body))


def test_anchor_floor_absent_is_none(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, "")
    assert cfg.estimator_anchor_floor is None


def test_anchor_floor_parsed(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, """
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
    assert isinstance(af, AnchorFloorConfig)
    assert af.general_percentile == 60
    assert af.min_refs == 3
    assert af.full_percentile_min_refs == 5
    assert af.max_lift_ratio == 1.6
    assert isinstance(af.premium_tiers[0], AnchorTier)
    assert af.premium_tiers[0].percentile == 70
    assert af.premium_tiers[0].keywords == ("温感", "もこもこ")


def test_anchor_floor_defaults(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, """
        estimator:
          anchor_floor:
            general_percentile: 60
""")
    af = cfg.estimator_anchor_floor
    assert af.min_refs == 3 and af.full_percentile_min_refs == 5
    assert af.max_lift_ratio == 1.6 and af.premium_tiers == ()


@pytest.mark.parametrize("block", [
    "anchor_floor: {}",  # present but missing required general_percentile
    "anchor_floor:\n            general_percentile: 120",
    "anchor_floor:\n            general_percentile: 60.5",  # non-integer rejected, not truncated
    "anchor_floor:\n            general_percentile: 60\n            min_refs: 0",
    "anchor_floor:\n            general_percentile: 60\n            full_percentile_min_refs: 2",
    "anchor_floor:\n            general_percentile: 60\n            max_lift_ratio: 0.5",
    "anchor_floor:\n            general_percentile: 60\n            premium_tiers:\n              - percentile: 70\n                keywords: []",
    "anchor_floor:\n            general_percentile: 60\n            premium_tiers:\n              - percentile: 70\n                keywords: 温感",  # scalar string, not a list
])
def test_anchor_floor_validate_rejects(tmp_path, monkeypatch, block):
    with pytest.raises(ValueError):
        _load(tmp_path, monkeypatch, f"        estimator:\n          {block}\n")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_config_schema.py -k anchor_floor -v -o addopts=""`
Expected: FAIL — `cannot import name 'AnchorFloorConfig'`.

- [ ] **Step 3: Add the dataclasses + parse guards**

In `estimator_king/config_schema.py`, after `BundleSetPolicy` (after line 101), add:

```python
def _req_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"anchor_floor.{name} must be an integer")
    return value


def _req_num(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"anchor_floor.{name} must be a number")
    return float(value)


def _req_str_list(name: str, value: object) -> tuple[str, ...]:
    if (not isinstance(value, list) or not value
            or any(not isinstance(k, str) or not k for k in value)):
        raise ValueError(f"anchor_floor.{name} must be a non-empty list of non-empty strings")
    return tuple(value)


@dataclass(frozen=True)
class AnchorTier:
    """One premium anchor-floor tier: a percentile + the keywords that select it."""

    percentile: int
    keywords: tuple[str, ...]

    def validate(self):
        if isinstance(self.percentile, bool) or not isinstance(self.percentile, int):
            raise ValueError("anchor_floor tier percentile must be an integer")
        if not (0 <= self.percentile <= 100):
            raise ValueError("anchor_floor tier percentile must be 0-100")
        if not self.keywords or any(not isinstance(k, str) or not k for k in self.keywords):
            raise ValueError("anchor_floor tier keywords must be a non-empty list of non-empty strings")


@dataclass(frozen=True)
class AnchorFloorConfig:
    """Deterministic anchor-floor policy. Lifts a low suggested price toward a
    percentile of same-type reference prices, guarded by sparse/clamp/outlier
    checks. None on the AppConfig means the floor is disabled."""

    general_percentile: int
    min_refs: int = 3
    full_percentile_min_refs: int = 5
    max_lift_ratio: float = 1.6
    premium_tiers: tuple[AnchorTier, ...] = ()

    def validate(self):
        for name in ("general_percentile", "min_refs", "full_percentile_min_refs"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int):
                raise ValueError(f"anchor_floor.{name} must be an integer")
        if isinstance(self.max_lift_ratio, bool) or not isinstance(self.max_lift_ratio, (int, float)):
            raise ValueError("anchor_floor.max_lift_ratio must be a number")
        if not (0 <= self.general_percentile <= 100):
            raise ValueError("anchor_floor.general_percentile must be 0-100")
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

In the `AppConfig` dataclass, after line 143 (`estimator_fetch_multiplier: int = 2`), add:

```python
    estimator_anchor_floor: "AnchorFloorConfig | None" = None
```

- [ ] **Step 5: Parse in `load_config`**

In `load_config`, after `est = yaml_data.get("estimator", {}) or {}` (line 290), add (note `if af is not None:` so a present-but-empty `anchor_floor: {}` is parsed and fails on the missing required key, instead of being silently disabled):

```python
    af = est.get("anchor_floor")
    anchor_floor = None
    if af is not None:
        if "general_percentile" not in af:
            raise ValueError("anchor_floor requires general_percentile")
        anchor_floor = AnchorFloorConfig(
            general_percentile=_req_int("general_percentile", af["general_percentile"]),
            min_refs=_req_int("min_refs", af.get("min_refs", 3)),
            full_percentile_min_refs=_req_int(
                "full_percentile_min_refs", af.get("full_percentile_min_refs", 5)),
            max_lift_ratio=_req_num("max_lift_ratio", af.get("max_lift_ratio", 1.6)),
            premium_tiers=tuple(
                AnchorTier(
                    percentile=_req_int("tier.percentile", t["percentile"]),
                    keywords=_req_str_list("tier.keywords", t.get("keywords", [])),
                )
                for t in (af.get("premium_tiers", []) or [])
            ),
        )
```

Add to the `AppConfig(...)` constructor call (after `estimator_fetch_multiplier=...`, line 318):

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
Expected: PASS (all anchor_floor tests, including the rejection cases).

- [ ] **Step 8: Type check + lint**

Run: `.venv/bin/basedpyright estimator_king/config_schema.py && uvx ruff check estimator_king/config_schema.py tests/test_config_schema.py`
Expected: 0 errors in production code; ruff clean.

- [ ] **Step 9: Commit (via git-master)**

```bash
git add estimator_king/config_schema.py tests/test_config_schema.py
git commit -m "feat(config): add AnchorFloorConfig schema, parsing and validation"
```

---

### Task 3: `_anchor_floor` pure function (guards + range recompute + provenance + audit log)

**Files:**
- Modify: `estimator_king/bot/estimator.py` (add `import unicodedata`; `TYPE_CHECKING` import of `AnchorFloorConfig`; `_norm_kw`, `_SKEW`, `_anchor_floor` after `_percentile`)
- Test: `tests/test_estimator.py`

- [ ] **Step 1: Write the failing tests**

Update the import at the top of `tests/test_estimator.py`:

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
_REFS5 = [2000, 2500, 3000, 3500, 4000]  # linear: p50=3000, p60=3200, p70=3400


def test_anchor_floor_no_op_when_cfg_none():
    e = _est_full("x", 1000, 800, 1300)
    assert _anchor_floor("x", e, _REFS5, None) is e


def test_anchor_floor_no_op_sentinel():
    e = _est_full("x", 0, 0, 0, conf="low")
    assert _anchor_floor("x", e, _REFS5, _CFG) is e


def test_anchor_floor_no_op_sparse():
    e = _est_full("x", 1000, 800, 1300)
    assert _anchor_floor("x", e, [3000, 4000], _CFG) is e  # 2 refs < min_refs


def test_anchor_floor_no_op_empty_refs():
    e = _est_full("x", 1000, 800, 1300)
    assert _anchor_floor("x", e, [], _CFG) is e


def test_anchor_floor_raises_to_general_percentile():
    e = _est_full("ポーチ", 2200, 1800, 2900)
    out = _anchor_floor("ポーチ", e, _REFS5, _CFG)
    assert out.suggested_price_jpy == 3200
    assert out.rationale.startswith("[anchor floor:")


def test_anchor_floor_never_lowers():
    e = _est_full("x", 5000, 4000, 6000)
    assert _anchor_floor("x", e, _REFS5, _CFG) is e  # floor 3200 < suggested


def test_anchor_floor_premium_uses_higher_tier():
    e = _est_full("温感マグカップ", 2200, 1800, 2900)
    out = _anchor_floor("温感マグカップ", e, _REFS5, _CFG)
    assert out.suggested_price_jpy == 3400  # p70


def test_anchor_floor_premium_keyed_on_query_not_product_name():
    e = _est_full("rewritten name", 2200, 1800, 2900)
    out = _anchor_floor("温感マグカップ", e, _REFS5, _CFG)
    assert out.suggested_price_jpy == 3400


def test_anchor_floor_multi_tier_takes_max():
    cfg = AnchorFloorConfig(
        general_percentile=60, min_refs=3, full_percentile_min_refs=5, max_lift_ratio=1.6,
        premium_tiers=(AnchorTier(percentile=65, keywords=("温感",)),
                       AnchorTier(percentile=70, keywords=("もこもこ",))))
    e = _est_full("温感もこもこ", 2200, 1800, 2900)
    out = _anchor_floor("温感もこもこ", e, _REFS5, cfg)
    assert out.suggested_price_jpy == 3400  # max(65,70)=70 -> p70


def test_anchor_floor_keyword_nfkc_fullwidth_latin():
    # NFKC+casefold: full-width latin ＢＩＧ -> big matches keyword "big"
    cfg = AnchorFloorConfig(
        general_percentile=60, min_refs=3, full_percentile_min_refs=5, max_lift_ratio=1.6,
        premium_tiers=(AnchorTier(percentile=70, keywords=("big",)),))
    e = _est_full("ＢＩＧぬいぐるみ", 2200, 1800, 2900)
    out = _anchor_floor("ＢＩＧぬいぐるみ", e, _REFS5, cfg)
    assert out.suggested_price_jpy == 3400


def test_anchor_floor_small_sample_clamped_to_median():
    # n=3 in [min_refs,5): even premium 温感 clamps to p50; median([2000,3000,5000])=3000
    e = _est_full("温感マグカップ", 2200, 1800, 2900)
    out = _anchor_floor("温感マグカップ", e, [2000, 3000, 5000], _CFG)
    assert out.suggested_price_jpy == 3000


def test_anchor_floor_max_lift_ratio_no_op():
    e = _est_full("x", 2200, 1800, 2900)  # 2200*1.6=3520 < floor 9000 -> skip
    assert _anchor_floor("x", e, [8000, 8500, 9000, 9500, 10000], _CFG) is e


def test_anchor_floor_recomputes_range_with_upward_skew():
    e = _est_full("ポーチ", 2200, 1800, 2900, conf="medium")
    out = _anchor_floor("ポーチ", e, _REFS5, _CFG)  # floor 3200, medium +45%
    assert out.price_range_jpy.max >= round(3200 * 1.45)
    assert out.price_range_jpy.min <= out.suggested_price_jpy


def test_anchor_floor_does_not_mutate_original():
    e = _est_full("ポーチ", 2200, 1800, 2900)
    _anchor_floor("ポーチ", e, _REFS5, _CFG)
    assert e.suggested_price_jpy == 2200 and e.rationale == "r"


def test_anchor_floor_logs_apply_and_skip(caplog):
    import logging
    with caplog.at_level(logging.INFO):
        _anchor_floor("ポーチ", _est_full("ポーチ", 2200, 1800, 2900), _REFS5, _CFG)
    assert any("anchor_floor applied" in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        _anchor_floor("x", _est_full("x", 2200, 1800, 2900),
                      [8000, 8500, 9000, 9500, 10000], _CFG)
    assert any("anchor_floor skip" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -k anchor_floor -v -o addopts=""`
Expected: FAIL — `cannot import name '_anchor_floor'`.

- [ ] **Step 3: Write the implementation**

In `estimator_king/bot/estimator.py`: add `import unicodedata` with the other stdlib imports. Add (or extend) a `TYPE_CHECKING` import block near the top:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from estimator_king.config_schema import AnchorFloorConfig
```

After `_percentile`, add:

```python
_SKEW = {"high": (0.20, 0.30), "medium": (0.25, 0.45), "low": (0.30, 0.60)}


def _norm_kw(s: str) -> str:
    """NFKC + casefold, for width/case-insensitive premium-keyword matching."""
    return unicodedata.normalize("NFKC", s).casefold()


def _anchor_floor(query: str, est: ProductEstimate, same_type_prices: list[int],
                  cfg: "AnchorFloorConfig | None") -> ProductEstimate:
    """Raise est.suggested toward a percentile of same-type refs, keyed by the
    original `query`. Returns est unchanged when disabled (cfg None), on the
    no-estimate sentinel, on sparse refs (< min_refs), when the lift exceeds
    max_lift_ratio, or when the floor is below suggested. On a real lift it
    recomputes the range with upward skew and prepends a provenance note to
    rationale. Logs apply/skip for audit."""
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
    if floor_value > round(suggested * cfg.max_lift_ratio):
        logger.info("anchor_floor skip lift>%.2f: %r %d->%d @p%d n=%d",
                    cfg.max_lift_ratio, query, suggested, floor_int, effective, n)
        return est
    down, up = _SKEW.get(est.confidence, _SKEW["medium"])
    new_min = min(est.price_range_jpy.min, round(floor_int * (1 - down)))
    new_max = max(est.price_range_jpy.max, round(floor_int * (1 + up)))
    note = f"[anchor floor: ¥{suggested}->¥{floor_int} @p{effective}, n={n}] "
    logger.info("anchor_floor applied: %r %d->%d @p%d n=%d",
                query, suggested, floor_int, effective, n)
    return est.model_copy(update={
        "suggested_price_jpy": floor_int,
        "price_range_jpy": PriceRange(min=new_min, max=new_max),
        "rationale": note + est.rationale,
    })
```

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

### Task 4: Wire the floor into the pipeline (chunk prices, alignment guard, param, runner)

**Files:**
- Modify: `estimator_king/bot/estimator.py` (`_estimate_chunk` ~line 199-226; `estimate_products` ~line 186-197; `Estimator.__init__` ~line 161-177)
- Modify: `estimator_king/bot/runner.py:47-55`
- Test: `tests/test_estimator.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_estimator.py`:

```python
def _estimator_af(vs, chat, anchor_floor, typing=None, item_type="ぬいぐるみ"):
    return Estimator(FakeEmbedder(), chat, vs,
                     typing_provider=(typing or FakeTypingProvider(item_type)),
                     item_types=[item_type], item_types_version=1,
                     top_k=10, recency_weight=0.05, diversity_weight=0.0, fetch_multiplier=1,
                     anchor_floor=anchor_floor)


def _nui_hits():
    return [_hit(f"r{i}", "ぬいぐるみ", p, 0, 0.1)
            for i, p in enumerate([2000, 2500, 3000, 3500, 4000])]


def test_pipeline_floor_raises_low_estimate():
    vs = RecordingVectorStore(_nui_hits())
    chat = FakeChat([_est_full("ぬいぐるみ", 2200, 1800, 2900)])
    est = _estimator_af(vs, chat, _CFG, typing=FakeTypingProvider("ぬいぐるみ"))
    out = est.estimate_products(["ぬいぐるみ"], "u").estimates[0]
    assert out.suggested_price_jpy == 3190  # 3200 floor snapped to ¥110 grid
    assert "anchor floor" in out.rationale


def test_pipeline_floor_disabled_when_no_config():
    vs = RecordingVectorStore(_nui_hits())
    chat = FakeChat([_est_full("ぬいぐるみ", 2200, 1800, 2900)])
    est = _estimator_af(vs, chat, None, typing=FakeTypingProvider("ぬいぐるみ"))
    out = est.estimate_products(["ぬいぐるみ"], "u").estimates[0]
    assert out.suggested_price_jpy == 2200  # snapped only
    assert "anchor floor" not in out.rationale


def test_pipeline_floor_noop_for_sonota_query():
    # classify_query returns [] for その他 -> empty same-type set -> no floor
    hits = [_hit(f"r{i}", "その他", p, 0, 0.2) for i, p in enumerate([2000, 2500, 3000, 3500, 4000])]
    vs = RecordingVectorStore(hits)
    chat = FakeChat([_est_full("謎の物体", 2200, 1800, 2900)])
    est = _estimator_af(vs, chat, _CFG, typing=FakeTypingProvider("その他"))
    out = est.estimate_products(["謎の物体"], "u").estimates[0]
    assert out.suggested_price_jpy == 2200
    assert "anchor floor" not in out.rationale


def test_pipeline_floor_skipped_on_length_mismatch_short(caplog):
    import logging
    vs = RecordingVectorStore(_nui_hits())
    chat = FakeChat([_est_full("ぬいぐるみ", 2200, 1800, 2900)])
    est = _estimator_af(vs, chat, _CFG, typing=FakeTypingProvider("ぬいぐるみ"))
    est._reconcile = lambda names, ests: []  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        batch = est.estimate_products(["ぬいぐるみ"], "u")
    assert any("anchor_floor skipped" in r.message for r in caplog.records)
    assert batch.estimates == []


def test_pipeline_floor_skipped_on_length_mismatch_long(caplog):
    import logging
    vs = RecordingVectorStore(_nui_hits())
    chat = FakeChat([_est_full("ぬいぐるみ", 2200, 1800, 2900)])
    est = _estimator_af(vs, chat, _CFG, typing=FakeTypingProvider("ぬいぐるみ"))
    # reconcile returns TWO rows for ONE input line -> length mismatch (too many)
    est._reconcile = lambda names, ests: [_est_full("ぬいぐるみ", 2200, 1800, 2900),
                                          _est_full("extra", 2200, 1800, 2900)]  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        batch = est.estimate_products(["ぬいぐるみ"], "u")
    assert any("anchor_floor skipped" in r.message for r in caplog.records)
    # no estimate was floored (both stay at their snapped model value)
    assert all("anchor floor" not in e.rationale for e in batch.estimates)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -k "pipeline_floor" -v -o addopts=""`
Expected: FAIL — `Estimator.__init__` has no `anchor_floor` kwarg.

- [ ] **Step 3: Add the `anchor_floor` constructor param**

In `Estimator.__init__` (line 161-166), change the signature tail from `fetch_multiplier: int = 2) -> None:` to:

```python
                 fetch_multiplier: int = 2,
                 anchor_floor: "AnchorFloorConfig | None" = None) -> None:
```

After `self._fetch_multiplier = fetch_multiplier` (line 176) add:

```python
        self._anchor_floor = anchor_floor
```

- [ ] **Step 4: Make `_estimate_chunk` return same-type prices**

Replace the whole `_estimate_chunk` (line 199-226) with:

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

In `estimate_products`, replace the chunk loop + post-processing (line 186-197) — **keep the trailing `logger.info(... done ...)` and `return EstimateBatch(estimates=reconciled)` exactly as they are** — with:

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

(The existing `logger.info("estimate done ...")` + `return EstimateBatch(estimates=reconciled)` lines below this block are unchanged.)

- [ ] **Step 6: Pass config through `runner.py`**

In `estimator_king/bot/runner.py`, the `Estimator(...)` call (line 47-55), add after `fetch_multiplier=config.estimator_fetch_multiplier,`:

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
- Test: `tests/test_bot_commands.py` (append; formatter is `format_estimates`)

The provenance is **prepended** in `_anchor_floor` (Task 3), so it lands inside `format_estimates`' 297-char window. This locks it. No production change expected; if it fails, keep prepending in `_anchor_floor` — do **not** change `commands.py` truncation.

- [ ] **Step 1: Write the test**

Append **only the function** to `tests/test_bot_commands.py`. It already imports `format_estimates` (from `estimator_king.bot.commands`) and `EstimateBatch`, `PriceRange`, `ProductEstimate` (from `estimator_king.llm.chat`) at the top — do **not** re-import them (ruff F811/F401 would fail):

```python
def test_floor_provenance_survives_rationale_truncation():
    long_tail = "あ" * 400  # model rationale far over the 297-char cap
    est = ProductEstimate(
        product_name="ポーチ",
        suggested_price_jpy=3190,
        price_range_jpy=PriceRange(min=2420, max=4620),
        confidence="medium",
        rationale="[anchor floor: ¥2200->¥3190 @p60, n=6] " + long_tail,
        reference_products=[],
    )
    embeds = format_estimates(EstimateBatch(estimates=[est]))
    rendered = " ".join(e.description or "" for e in embeds)
    assert "anchor floor" in rendered
```

- [ ] **Step 2: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_bot_commands.py -k provenance -v -o addopts=""`
Expected: PASS. (If FAIL, the provenance was appended not prepended — fix `_anchor_floor` in Task 3.)

- [ ] **Step 3: Lint**

Run: `uvx ruff check tests/test_bot_commands.py`
Expected: ruff clean.

- [ ] **Step 4: Commit (via git-master)**

```bash
git add tests/test_bot_commands.py
git commit -m "test(bot): assert anchor-floor provenance survives rationale truncation"
```

---

### Task 6: eval_estimate.py — one chat pass, paired baseline-vs-candidate, fail-closed gate

The eval already self-excludes each fixture's own product. We add: (a) collect same-type prices in `build_context`; (b) `run_once` returns the **raw** model estimate + same-type prices per query (no floor, no snap) so floor-on vs floor-off are computed from the *same* chat output (paired, no extra API cost); (c) `main` computes a baseline (no floor) and a candidate (floor) metric bundle and runs a fail-closed acceptance gate on the full spec criteria.

**Files:**
- Modify: `scripts/analysis/eval_estimate.py`

- [ ] **Step 1: build_context also returns same-type prices**

After `ranked = est._rerank(list(merged.values()))[: est._top_k]`, add:

```python
    type_set = set(types)
    same_type_prices = [
        int(h.metadata.get("price_jpy", 0) or 0)
        for h in ranked
        if str(h.metadata.get("item_type", "") or "") in type_set
        and int(h.metadata.get("price_jpy", 0) or 0) > 0
    ]
```

Change the return to `return f"### Query: {query}\n{refs or '(no matches)'}", list(selves.values()), same_type_prices` and the return type annotation to `tuple[str, list[str], list[int]]`.

- [ ] **Step 2: run_once returns raw estimate + prices + official per query**

Add `_anchor_floor` to the existing `from estimator_king.bot.estimator import ...` import. Replace `run_once` so it returns, per query, the raw `ProductEstimate`, its same-type prices, and the official price — **without** snapping or flooring (those are applied per-policy in `main`):

```python
def run_once(est: Estimator) -> dict[str, tuple[Any, list[int], int]]:
    """One chat pass. Returns {query: (raw_estimate, same_type_prices, official)}.
    Floor and snap are applied later per policy so baseline/candidate are paired."""
    out: dict[str, tuple[Any, list[int], int]] = {}
    try:
        for start in range(0, len(FIXTURES), est.CHUNK_SIZE):
            chunk = FIXTURES[start:start + est.CHUNK_SIZE]
            blocks: list[str] = []
            stp: dict[str, list[int]] = {}
            for query, official in chunk:
                block, selves, prices = build_context(est, query, official)
                blocks.append(block)
                stp[query] = prices
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
                    raise InvalidRun(f"chat returned no estimate for {query!r}")
                out[query] = (est_obj, stp[query], official)
        if len(out) != len(FIXTURES):
            raise InvalidRun(f"aligned {len(out)} of {len(FIXTURES)} fixtures")
    except InvalidRun:
        raise
    except Exception as exc:
        raise InvalidRun(f"run failed: {exc}") from exc
    return out
```

- [ ] **Step 3: main computes paired metrics + fail-closed gate**

Add this helper above `main`:

```python
def _metrics(runs: list[dict[str, tuple[Any, list[int], int]]], cfg) -> dict[str, Any]:
    """Apply (or skip) the floor per policy on the SAME chat outputs, then snap;
    return aggregate metrics + the no-estimate set (raw suggested==0 in any run)."""
    per_abs: dict[str, list[float]] = {q: [] for q, _ in FIXTURES}
    per_signed: dict[str, list[float]] = {q: [] for q, _ in FIXTURES}
    covered: dict[str, int] = {q: 0 for q, _ in FIXTURES}
    no_estimate: set[str] = set()
    for run in runs:
        for query, (est_obj, prices, official) in run.items():
            if est_obj.suggested_price_jpy == 0:
                no_estimate.add(query)
            floored = _anchor_floor(query, est_obj, prices, cfg) if cfg else est_obj
            snapped = _snap_estimate(floored)
            p = snapped.suggested_price_jpy
            per_abs[query].append(abs(p - official) / official * 100.0)
            per_signed[query].append((p - official) / official * 100.0)
            if snapped.price_range_jpy.min <= official <= snapped.price_range_jpy.max:
                covered[query] += 1
    majority = len(runs) // 2 + 1
    est_qs = [q for q, _ in FIXTURES if q not in no_estimate]
    abs_means = [statistics.mean(per_abs[q]) for q in est_qs]
    signed_means = [statistics.mean(per_signed[q]) for q in est_qs]
    return {
        "mape": statistics.mean(abs_means) if abs_means else 0.0,
        "signed": statistics.mean(signed_means) if signed_means else 0.0,
        "coverage": sum(1 for q in est_qs if covered[q] >= majority),
        "n_est": len(est_qs),
        "no_estimate": no_estimate,
    }
```

Then **replace the entire body of `main` after the `Estimator(...)` is constructed** (i.e. remove the old `per_fixture` accumulation loop, the PER-FIXTURE table, the old SUMMARY block, and the no-estimate computation — keep only the argparse + `load_config` + `build_providers` + `Estimator(...)` setup) with:

```python
    runs: list[dict[str, tuple[Any, list[int], int]]] = []
    try:
        for r in range(args.runs):
            print(f"\n===== run {r + 1}/{args.runs} =====")
            runs.append(run_once(est))
    except InvalidRun as exc:
        print(f"\nINVALID run: {exc}; not reporting.", file=sys.stderr)
        sys.exit(2)

    baseline = _metrics(runs, None)
    cfg = config.estimator_anchor_floor
    candidate = _metrics(runs, cfg) if cfg is not None else None

    def _show(label: str, m: dict[str, Any]) -> None:
        print(f"\n========== {label} ==========")
        print(f"  MAPE {m['mape']:.1f}%  signed {m['signed']:+.1f}%  "
              f"coverage {m['coverage']}/{m['n_est']}  "
              f"no-estimate {sorted(m['no_estimate'])}")

    _show("BASELINE (floor disabled)", baseline)
    if candidate is not None:
        _show("CANDIDATE (floor enabled)", candidate)

    prompt_hash = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:8]
    print("\n========== PROVENANCE ==========")
    print(f"  prompt_hash: {prompt_hash}")
    print(f"  git_commit: {_git(['rev-parse', '--short', 'HEAD'])}   "
          f"dirty: {bool(_git(['status', '--porcelain']))}")
    print(f"  embedding_model: {config.embedding_model}   chat_model: {config.chat_model}")
    print(f"  fixtures: {len(FIXTURES)}   runs: {args.runs}   "
          f"anchor_floor: {'on' if cfg is not None else 'off'}")

    if candidate is not None:
        fails = []
        # spec: |signed| must substantially decrease (handles over-correction either sign).
        if abs(candidate["signed"]) > abs(baseline["signed"]) - 1.0:
            fails.append(f"|signed| not improved ({baseline['signed']:+.1f} -> {candidate['signed']:+.1f})")
        if candidate["mape"] > baseline["mape"] + 2.0:
            fails.append(f"MAPE worse ({baseline['mape']:.1f} -> {candidate['mape']:.1f}, > +2pp)")
        if candidate["coverage"] < baseline["coverage"]:
            fails.append(f"coverage dropped ({baseline['coverage']} -> {candidate['coverage']})")
        if not candidate["no_estimate"].issubset(baseline["no_estimate"]):
            fails.append("no-estimate set grew beyond baseline")
        if fails:
            print("\n========== ACCEPTANCE: FAIL ==========", file=sys.stderr)
            for f in fails:
                print(f"  - {f}", file=sys.stderr)
            sys.exit(3)
        print("\n========== ACCEPTANCE: PASS ==========")
```

(Keep the module's existing helpers `_git`, `InvalidRun`, `build_context`, and the imports of `hashlib`/`statistics`/`sys`. The PER-FIXTURE per-query table is dropped in favor of the BASELINE/CANDIDATE summary; that is intentional.)

> The gate is **abs-based**: `|signed|` must drop by at least 1pp (so over-correcting to a large positive bias also fails), MAPE must not worsen beyond +2pp, coverage must not drop, and the no-estimate set must stay a subset. Both runs are VALID by construction (`InvalidRun` → exit 2 earlier). When `anchor_floor` is absent, only BASELINE prints and no gate runs (used to record disabled baseline numbers).

- [ ] **Step 4: Type-check + lint**

Run: `.venv/bin/basedpyright scripts/analysis/eval_estimate.py && uvx ruff check scripts/analysis/eval_estimate.py`
Expected: 0 errors in production code (`estimator_king/`); ruff clean. (`# pyright: reportPrivateUsage=false` already heads the file.)

- [ ] **Step 5: Commit (via git-master)**

```bash
git add scripts/analysis/eval_estimate.py
git commit -m "feat(eval): paired baseline-vs-candidate floor metrics with fail-closed gate"
```

---

### Task 7: experiment_anchor_floor.py — candidate config (multi-tier) + ref-count bands

The experiment is a **calibration** tool. It must test **candidate** values while the shipped config has `anchor_floor` absent. It loads a candidate `AnchorFloorConfig` either from a YAML file (arbitrary multi-tier, via the same `load_config` parser) or from CLI flags (single tier, defaults = spec starting values), reuses `eval_estimate.run_once` (one chat pass) and the production `_anchor_floor`, and reports paired baseline-vs-candidate metrics **per same-type ref count** with a powered/non-regressing pass/fail.

**Files:**
- Rewrite: `scripts/analysis/experiment_anchor_floor.py` (replace the whole file body below the module docstring + `# pyright: reportPrivateUsage=false` header)

- [ ] **Step 1: Replace the script body with the candidate-config + banded reporter**

Replace everything after the file's docstring/header with this complete implementation (it deletes the old `PREMIUM_KW`, `percentile`, `floor_at`, `apply_floor`, `PCTS`, `subset_metrics`, `is_premium`, `label`, `build_context`, `run_once` machinery and reuses `eval_estimate`):

```python
from __future__ import annotations

import argparse
import statistics
import sys
from typing import Any

from estimator_king.bot.estimator import _anchor_floor, _snap_estimate
from estimator_king.config_schema import (
    AnchorFloorConfig, AnchorTier, load_config,
)
from estimator_king.runtime import build_providers
from scripts.analysis.eval_estimate import FIXTURES, InvalidRun, run_once

MIN_BUCKET_N = 5


def _candidate_from_cli(args: argparse.Namespace) -> AnchorFloorConfig:
    if args.candidate_config:
        cfg = load_config(args.candidate_config).estimator_anchor_floor
        if cfg is None:
            raise SystemExit(f"{args.candidate_config} has no estimator.anchor_floor block")
        return cfg
    cfg = AnchorFloorConfig(
        general_percentile=args.general, min_refs=args.min_refs,
        full_percentile_min_refs=args.full_min_refs, max_lift_ratio=args.max_lift,
        premium_tiers=(AnchorTier(
            percentile=args.premium,
            keywords=tuple(k for k in args.premium_keywords.split(",") if k)),),
    )
    cfg.validate()
    return cfg


def _suggested(query: str, est_obj: Any, prices: list[int],
               cfg: AnchorFloorConfig | None) -> tuple[int, bool]:
    """Return (snapped suggested, floor_applied) for one fixture under a policy."""
    floored = _anchor_floor(query, est_obj, prices, cfg) if cfg else est_obj
    return _snap_estimate(floored).suggested_price_jpy, (floored is not est_obj)


def main() -> None:
    parser = argparse.ArgumentParser(description="Anchor-floor calibration by ref-count band.")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--candidate-config",
                        help="YAML file with estimator.anchor_floor (multi-tier); "
                             "overrides the single-tier CLI flags below")
    parser.add_argument("--general", type=int, default=60)
    parser.add_argument("--premium", type=int, default=70)
    parser.add_argument("--premium-keywords", default="温感,もこもこ,あったか,なりきり")
    parser.add_argument("--min-refs", type=int, default=3)
    parser.add_argument("--full-min-refs", type=int, default=5)
    parser.add_argument("--max-lift", type=float, default=1.6)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be >= 1")

    cfg = _candidate_from_cli(args)
    config = load_config()
    providers = build_providers(config, with_chat=True)
    assert providers.chat is not None, "needs chat; check chat_api_key in .env"
    from estimator_king.bot.estimator import Estimator
    est = Estimator(
        providers.embedder, providers.chat, providers.vector_store,
        providers.typing_provider,
        item_types=config.item_types, item_types_version=config.item_types_version,
        top_k=config.estimator_top_k, recency_weight=config.estimator_recency_weight,
        diversity_weight=config.estimator_diversity_weight,
        fetch_multiplier=config.estimator_fetch_multiplier,
    )

    try:
        runs = [run_once(est) for _ in range(args.runs)]
    except InvalidRun as exc:
        print(f"INVALID run: {exc}", file=sys.stderr)
        sys.exit(2)

    # Per fixture, paired baseline vs candidate from the same chat output.
    # rows[query] = (n, base_signed_mean, base_abs_mean, cand_signed_mean,
    #                cand_abs_mean, applied_any, base_sugg, cand_sugg)
    rows: dict[str, tuple[Any, ...]] = {}
    for query, official in FIXTURES:
        n = len(runs[0][query][1]) if query in runs[0] else 0
        b_signed, b_abs, c_signed, c_abs = [], [], [], []
        applied_any = False
        last_base = last_cand = 0
        for run in runs:
            est_obj, prices, off = run[query]
            n = len(prices)
            if est_obj.suggested_price_jpy == 0:
                continue
            bs, _ = _suggested(query, est_obj, prices, None)
            cs, applied = _suggested(query, est_obj, prices, cfg)
            applied_any = applied_any or applied
            last_base, last_cand = bs, cs
            b_signed.append((bs - off) / off * 100.0)
            b_abs.append(abs(bs - off) / off * 100.0)
            c_signed.append((cs - off) / off * 100.0)
            c_abs.append(abs(cs - off) / off * 100.0)
        if not b_signed:  # sentinel / no-estimate in every run
            rows[query] = (n, None, None, None, None, False, 0, 0)
            continue
        rows[query] = (n, statistics.mean(b_signed), statistics.mean(b_abs),
                       statistics.mean(c_signed), statistics.mean(c_abs),
                       applied_any, last_base, last_cand)

    # Per-fixture table.
    print(f"\n========== PER-FIXTURE (candidate vs baseline) ==========")
    print(f"  {'query':<34} {'n':>2} {'base':>6} {'cand':>6} marker")
    skipped_min_refs = 0
    for query, _ in FIXTURES:
        n, _bs, _ba, _cs, _ca, applied, base_s, cand_s = rows[query]
        if _bs is None:
            marker = "sentinel"
        elif n < cfg.min_refs:
            marker = "skip:min_refs"; skipped_min_refs += 1
        elif applied:
            marker = "clamped" if n < cfg.full_percentile_min_refs else "lifted"
        else:
            marker = "no-lift"  # floor <= suggested, or capped by max_lift_ratio
        print(f"  {query[:34]:<34} {n:>2} {base_s:>6} {cand_s:>6} {marker}")

    # Bands by exact same-type ref count (fine-grained so each small-n bucket is visible).
    bands: dict[int, list[tuple[float, float, float, float, bool]]] = {}
    for query, _ in FIXTURES:
        n, bs, ba, cs, ca, applied, *_ = rows[query]
        if bs is None:
            continue
        bands.setdefault(n, []).append((bs, ba, cs, ca, applied))

    print(f"\n========== BANDS by same-type ref count (MIN_BUCKET_N={MIN_BUCKET_N}) ==========")
    print(f"  skipped by min_refs (n<{cfg.min_refs}): {skipped_min_refs} fixtures")
    print(f"  {'n':>3} {'N':>3} {'applied':>7} {'baseSgn':>8} {'candSgn':>8} "
          f"{'baseMAPE':>8} {'candMAPE':>8} {'region':>7} {'verdict':>9}")
    for n in sorted(bands):
        rs = bands[n]
        N = len(rs)
        applied_n = sum(1 for r in rs if r[4])
        b_sgn = statistics.mean(r[0] for r in rs)
        c_sgn = statistics.mean(r[2] for r in rs)
        b_mape = statistics.mean(r[1] for r in rs)
        c_mape = statistics.mean(r[3] for r in rs)
        if n < cfg.min_refs:
            region, verdict = "skip", "n/a"
        else:
            region = "clamp" if n < cfg.full_percentile_min_refs else "full"
            powered = applied_n >= MIN_BUCKET_N
            not_regressing = abs(c_sgn) <= abs(b_sgn) + 1.0 and c_mape <= b_mape + 2.0
            verdict = "PASS" if (powered and not_regressing) else (
                "underpow" if not powered else "REGRESS")
        print(f"  {n:>3} {N:>3} {applied_n:>7} {b_sgn:>+7.1f}% {c_sgn:>+7.1f}% "
              f"{b_mape:>7.1f}% {c_mape:>7.1f}% {region:>7} {verdict:>9}")

    print("\nREAD: only open the aggressive percentile to a ref-count band whose verdict is")
    print("PASS (>= MIN_BUCKET_N floor-applied AND |signed| not worse AND MAPE within +2pp).")
    print("An 'underpow'/'REGRESS' band must stay clamped (raise full_percentile_min_refs)")
    print("or no-op (raise min_refs).")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Type-check + lint**

Run: `.venv/bin/basedpyright scripts/analysis/experiment_anchor_floor.py && uvx ruff check scripts/analysis/experiment_anchor_floor.py`
Expected: 0 errors in production code; ruff clean. (Live run needs `.env` + chroma + chat; it runs during Task 9 calibration, not here. If `eval_estimate.py` keeps its `# pyright: reportPrivateUsage=false` header, mirror it here since this file also touches `est._*` via the reused `run_once`.)

- [ ] **Step 3: Commit (via git-master)**

```bash
git add scripts/analysis/experiment_anchor_floor.py
git commit -m "feat(analysis): candidate-config calibration with per-ref-count bands"
```

---

### Task 8: Sync docs/data-pipeline.md

**Files:**
- Modify: `docs/data-pipeline.md`

- [ ] **Step 1: Locate the reconcile→snap region**

Run: `grep -n "reconcile\|_snap_estimate\|snap_to_tax_grid\|chat-estimate" docs/data-pipeline.md`
Expected: find the query-path stage covering reconcile + snap.

- [ ] **Step 2: Insert the anchor-floor stage**

Between reconcile and snap, add a stage documenting `_anchor_floor` (match the doc's existing format: limit/condition · config key · function `file:line` · "why"). Cover all mechanics:
- controlling config key `estimator.anchor_floor` (floor **disabled** unless present; two-stage rollout — enabled via a separate config commit after the eval gate);
- function `estimator_king/bot/estimator.py` `_anchor_floor`, applied in `estimate_products` between `_reconcile` and `_snap_estimate`, keyed by the **original query**;
- **the floor value = a percentile (`effective_pct`) of the same-type top_k reference prices** (the refs the model was grounded on — `item_type ∈ classify_query(query)`, `price_jpy > 0`), lifting `suggested` to it;
- keyword match via NFKC+casefold on both sides;
- `min_refs` sparse no-op; `full_percentile_min_refs` small-sample median clamp; `effective_pct = max(general, matched tiers)`; `max_lift_ratio` outlier no-op; raise-only;
- range recomputed with confidence-based upward skew; provenance **prepended** to rationale (survives the 297-char embed truncation);
- **audit log**: `logger.info` on floor applied and on max_lift_ratio skip (query, original→floored, effective pct, ref count);
- no-op on その他 (`classify_query == []` → empty same-type set), sentinel, cfg None; batch-level alignment length guard fail-closes (logs error, skips floor for the whole batch);
- **設計理由 (why)**: prompt-level anchoring failed to move the directional bias on gpt-5.4-mini (two rounds, signed err stuck ~−10%), so this deterministic post-processing hardens it; the floor is **tiered** because the calibrated optimal percentile for ordinary items (~p60) and premium items (~p70) differ by roughly 10pp.

- [ ] **Step 3: Verify it landed**

Run: `grep -n "anchor_floor\|anchor floor" docs/data-pipeline.md`
Expected: the new stage appears in the reconcile→snap region.

- [ ] **Step 4: Commit (via git-master)**

```bash
git add docs/data-pipeline.md
git commit -m "docs(data-pipeline): document anchor-floor post-processing stage"
```

---

### Task 9: Stage-2 enablement (separate commit, AFTER the eval gate passes)

> **Do NOT do this in the implementation PR.** Second rollout stage; run only after Tasks 1–8 are merged. Changes `stores_config.yaml` only.

- [ ] **Step 1: Calibrate candidate values (config still disabled)**

The experiment takes candidate values from CLI (single tier) or `--candidate-config <yaml>` (arbitrary multi-tier), so it works while `stores_config.yaml` has no `anchor_floor`:
Run: `set -a; source .env; set +a; PYTHONPATH=. .venv/bin/python scripts/analysis/experiment_anchor_floor.py --runs 3 --general 60 --premium 70 --min-refs 3 --full-min-refs 5 --max-lift 1.6`
Read the **BANDS by same-type ref count** table. The `region` column shows `clamp` (n in `[min_refs, full_percentile_min_refs)`, forced to median) vs `full` (n ≥ `full_percentile_min_refs`, gets the configured percentile). Every band you are **opening to the aggressive percentile** — i.e. every `full` band, plus any band you would move from `clamp` to `full` by lowering `--full-min-refs` — must read `verdict = PASS` (≥ `MIN_BUCKET_N` floor-applied AND `|signed|` not worse AND MAPE within +2pp). If a band reads `underpow` or `REGRESS`, do **not** open it: raise `--full-min-refs` (keep those n clamped to median) or `--min-refs` (no-op). Re-run until every opened band is PASS. Record the final values for Step 2.

- [ ] **Step 2: Add the config block with the calibrated values**

Add to `stores_config.yaml` under `estimator:` (use the values confirmed in Step 1; example shows the spec starting values):

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

- [ ] **Step 3: Run the eval acceptance gate (does baseline-vs-candidate internally)**

Run: `set -a; source .env; set +a; PYTHONPATH=. set -o pipefail; .venv/bin/python scripts/analysis/eval_estimate.py --runs 3 2>&1 | tee /tmp/eval_anchor.txt; echo "exit=${PIPESTATUS[0]}"`
(`${PIPESTATUS[0]}` reports the eval process status, not `tee`'s — `| tee` would otherwise always report 0.)
Expected: prints BASELINE and CANDIDATE blocks and ends `ACCEPTANCE: PASS` with `exit=0`. The gate already enforces |signed|/MAPE/coverage/no-estimate fail-closed (Task 6), so a regressed candidate exits non-zero — **if `exit` is not 0, revert the Step 2 edit, adjust values, and repeat; do not commit.**

- [ ] **Step 4: Commit the enablement with evidence (via git-master)**

```bash
git add stores_config.yaml
git commit  # body: paste the BASELINE vs CANDIDATE block from /tmp/eval_anchor.txt (signed/MAPE/coverage/no-estimate) + ACCEPTANCE: PASS
```

Message: `feat(config): enable anchor_floor` + the recorded before/after acceptance data.

- [ ] **Step 5: Restart the bot** to load the enabled config. Rollback at any time = remove the `anchor_floor` block + restart.

---

## Done criteria

- Tasks 1–8 merged: code present, floor **disabled** (no `anchor_floor` in `stores_config.yaml`), full suite green, type-check + lint clean, docs synced.
- Task 9 (separate commit): experiment buckets PASS, `eval_estimate.py` exits 0 with `ACCEPTANCE: PASS`, config block added with recorded before/after data, bot restarted.
