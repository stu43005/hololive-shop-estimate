# pyright: reportPrivateUsage=false
"""experiment: candidate anchor_floor config calibration by same-type ref-count band."""

from __future__ import annotations

import argparse
import statistics
import sys
from typing import Any

import yaml

from estimator_king.bot.estimator import _anchor_floor, _snap_estimate
from estimator_king.config_schema import (
    AnchorFloorConfig, AnchorTier, load_config, parse_anchor_floor,
)
from estimator_king.runtime import build_providers
from scripts.analysis.eval_estimate import FIXTURES, InvalidRun, run_once

MIN_BUCKET_N = 5


def _candidate_from_cli(args: argparse.Namespace) -> AnchorFloorConfig:
    if args.candidate_config:
        # Parse the anchor_floor block directly (no store / full app config needed).
        with open(args.candidate_config, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        est = data.get("estimator", data)  # accept a full config or a bare estimator block
        cfg = parse_anchor_floor(est)
        if cfg is None:
            raise SystemExit(f"{args.candidate_config} has no estimator.anchor_floor block")
        cfg.validate()
        return cfg
    cfg = AnchorFloorConfig(
        general_percentile=args.general, min_refs=args.min_refs,
        full_percentile_min_refs=args.full_min_refs, max_lift_ratio=args.max_lift,
        premium_tiers=[AnchorTier(
            percentile=args.premium,
            keywords=[k for k in args.premium_keywords.split(",") if k])],
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
    unstable_queries: set[str] = set()
    for query, _ in FIXTURES:
        run_ns: list[int] = []
        b_signed, b_abs, c_signed, c_abs = [], [], [], []
        applied_any = False
        last_base = last_cand = 0
        no_estimate = False
        for run in runs:
            est_obj, prices, off = run[query]
            run_ns.append(len(prices))
            if est_obj.suggested_price_jpy == 0:  # no-estimate in ANY run -> drop the whole fixture
                no_estimate = True
                break
            bs, _ = _suggested(query, est_obj, prices, None)
            cs, applied = _suggested(query, est_obj, prices, cfg)
            applied_any = applied_any or applied
            last_base, last_cand = bs, cs
            b_signed.append((bs - off) / off * 100.0)
            b_abs.append(abs(bs - off) / off * 100.0)
            c_signed.append((cs - off) / off * 100.0)
            c_abs.append(abs(cs - off) / off * 100.0)
        n = run_ns[0] if run_ns else 0
        if no_estimate or not b_signed:  # sentinel in any run, or no usable estimate
            rows[query] = (n, None, None, None, None, False, 0, 0)
            continue
        if len(set(run_ns)) > 1:  # ref count not stable across runs -> can't band reliably, fail closed
            unstable_queries.add(query)
            rows[query] = (n, None, None, None, None, False, 0, 0)
            continue
        rows[query] = (n, statistics.mean(b_signed), statistics.mean(b_abs),
                       statistics.mean(c_signed), statistics.mean(c_abs),
                       applied_any, last_base, last_cand)

    # Per-fixture table.
    print("\n========== PER-FIXTURE (candidate vs baseline) ==========")
    print(f"  {'query':<34} {'n':>2} {'base':>6} {'cand':>6} marker")
    skipped_min_refs = 0
    for query, _ in FIXTURES:
        n, _bs, _ba, _cs, _ca, applied, base_s, cand_s = rows[query]
        if _bs is None:
            marker = "unstable" if query in unstable_queries else "sentinel"
        elif n < cfg.min_refs:
            marker = "skip:min_refs"
            skipped_min_refs += 1
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
    print(f"  excluded (ref count unstable across runs): {len(unstable_queries)} fixtures")
    print(f"  {'n':>3} {'N':>3} {'mrSkip':>6} {'applied':>7} {'baseSgn':>8} {'candSgn':>8} "
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
            region, verdict, mr_skip = "skip", "n/a", N
        else:
            region, mr_skip = ("clamp" if n < cfg.full_percentile_min_refs else "full"), 0
            powered = applied_n >= MIN_BUCKET_N
            not_regressing = abs(c_sgn) <= abs(b_sgn) and c_mape <= b_mape + 2.0
            verdict = "PASS" if (powered and not_regressing) else (
                "underpow" if not powered else "REGRESS")
        print(f"  {n:>3} {N:>3} {mr_skip:>6} {applied_n:>7} {b_sgn:>+7.1f}% {c_sgn:>+7.1f}% "
              f"{b_mape:>7.1f}% {c_mape:>7.1f}% {region:>7} {verdict:>9}")

    print("\nREAD: only open the aggressive percentile to a ref-count band whose verdict is")
    print("PASS (>= MIN_BUCKET_N floor-applied AND |signed| not worse AND MAPE within +2pp).")
    print("An 'underpow'/'REGRESS' band must stay clamped (raise full_percentile_min_refs)")
    print("or no-op (raise min_refs).")


if __name__ == "__main__":
    main()
