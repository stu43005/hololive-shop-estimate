# pyright: reportPrivateUsage=false
"""experiment: would a deterministic anchor floor kill the systematic low-balling?

Reuses eval_estimate's fixtures + faithful retrieval (with self-exclusion), but
captures, per query, the SAME-TYPE reference prices the model was grounded on.
The chat model is called ONCE per fixture; every floor policy is then applied to
the SAME raw estimate in post -- so policies are perfectly paired (no extra API
cost, no chat-noise between policies).

Floor policies compared (each only ever RAISES suggested, then re-snaps to ¥110):
  P0  none       -- current production behaviour (snap only); reproduces baseline
  P1  median     -- suggested >= median(same-type refs)
  P2  p75        -- suggested >= 75th pct(same-type refs)
  P3  premium    -- premium-keyword query -> p75 floor; otherwise median floor

Run: set -a; source .env; set +a; PYTHONPATH=. .venv/bin/python \\
    scripts/analysis/experiment_anchor_floor.py --runs 3
"""

from __future__ import annotations

import argparse
import statistics
import sys
from typing import Any

from estimator_king.bot.estimator import (
    SYSTEM_PROMPT, Estimator, _snap_estimate, snap_to_tax_grid,
)
from estimator_king.config_schema import load_config
from estimator_king.crawler.snapshot import normalize_text
from estimator_king.runtime import build_providers
from estimator_king.sync.typing import classify_query

# Reuse the same labeled fixtures as eval_estimate.py.
from scripts.analysis.eval_estimate import FIXTURES, SELF_SIM_THRESHOLD  # noqa: E402

# Mirrors the prompt's <premium_anchor> keyword list.
PREMIUM_KW = ("温感", "もこもこ", "あったか", "なりきり", "オーバーサイズ", "ビッグ")


class InvalidRun(Exception):
    """A run that could not produce a complete, aligned set of estimates."""


def percentile(values: list[int], q: float) -> float | None:
    s = sorted(values)
    if not s:
        return None
    if len(s) == 1:
        return float(s[0])
    pos = q * (len(s) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 < len(s):
        return s[lo] + (s[lo + 1] - s[lo]) * frac
    return float(s[lo])


def is_premium(query: str) -> bool:
    return any(k in query for k in PREMIUM_KW)


def build_context(est: Estimator, query: str, official: int
                  ) -> tuple[str, list[int]]:
    """Faithful _estimate_chunk retrieval with self-exclusion (same logic as
    eval_estimate.build_context), additionally returning the SAME-TYPE reference
    prices among the final top_k context (item_type in the query's classified
    types) -- the set a deterministic floor would anchor to."""
    embedding = est._embedder.embed_query(query)
    types = classify_query(
        query, item_types=est._item_types,
        item_types_version=est._item_types_version,
        typing_provider=est._typing_provider, repository=None,
    )
    type_set = set(types)
    queries: list[dict[str, Any] | None] = [{"item_type": t} for t in types]
    queries.append(None)
    nq = normalize_text(query)
    fetch_n = est._top_k * est._fetch_multiplier
    overfetch = fetch_n + est._top_k
    merged: dict[str, Any] = {}
    n_self = 0
    for where in queries:
        non_self: list[Any] = []
        for hit in est._vector_store.query(embedding, overfetch, where=where):
            price = int(hit.metadata.get("price_jpy", 0) or 0)
            name = str(hit.metadata.get("item_name") or "")
            sim = 1.0 - hit.distance
            name_match = normalize_text(name) == nq
            if price == official and (name_match or sim >= SELF_SIM_THRESHOLD):
                n_self += 1
                continue
            non_self.append(hit)
        non_self.sort(key=lambda h: h.distance)
        for hit in non_self[:fetch_n]:
            prev = merged.get(hit.id)
            if prev is None or hit.distance < prev.distance:
                merged[hit.id] = hit
    ranked = est._rerank(list(merged.values()))[: est._top_k]
    same_type_prices = [
        int(h.metadata.get("price_jpy", 0) or 0)
        for h in ranked
        if str(h.metadata.get("item_type", "") or "") in type_set
        and int(h.metadata.get("price_jpy", 0) or 0) > 0
    ]
    refs = "\n".join(est._format_reference(h) for h in ranked)
    return f"### Query: {query}\n{refs or '(no matches)'}", same_type_prices


# (suggested_snapped, min_snapped, max_snapped, same_type_prices, no_estimate)
RawRow = tuple[int, int, int, list[int], bool]


def run_once(est: Estimator) -> dict[str, RawRow]:
    out: dict[str, RawRow] = {}
    try:
        for start in range(0, len(FIXTURES), est.CHUNK_SIZE):
            chunk = FIXTURES[start:start + est.CHUNK_SIZE]
            blocks: list[str] = []
            stp: dict[str, list[int]] = {}
            for query, official in chunk:
                block, prices = build_context(est, query, official)
                blocks.append(block)
                stp[query] = prices
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
            for query, _ in chunk:
                est_obj = by_name.get(normalize_text(query))
                if est_obj is None:
                    raise InvalidRun(f"chat returned no estimate for {query!r}")
                no_est = est_obj.suggested_price_jpy == 0
                snapped = _snap_estimate(est_obj)
                out[query] = (
                    snapped.suggested_price_jpy,
                    snapped.price_range_jpy.min,
                    snapped.price_range_jpy.max,
                    stp[query], no_est,
                )
        if len(out) != len(FIXTURES):
            raise InvalidRun(f"aligned {len(out)} of {len(FIXTURES)} fixtures")
    except InvalidRun:
        raise
    except Exception as exc:
        raise InvalidRun(f"run failed: {exc}") from exc
    return out


def floor_at(prices: list[int], pct: float | None) -> int | None:
    """Single-percentile floor snapped to ¥110 grid (None = no-op / no refs)."""
    if not prices or pct is None:
        return None
    p = percentile(prices, pct)
    return None if p is None else snap_to_tax_grid(int(round(p)))


def apply_floor(row: RawRow, pct: float | None) -> tuple[int, int, int]:
    """Return (suggested, min, max) after a single-percentile floor; sentinel
    (0) rows pass through untouched."""
    sug, lo, hi, prices, no_est = row
    if no_est or sug == 0:
        return sug, lo, hi
    fp = floor_at(prices, pct)
    if fp is not None and fp > sug:
        sug = fp
    lo = min(lo, sug)
    hi = max(hi, sug)
    return sug, lo, hi


# None = baseline (no floor); rest are single-percentile knob settings to sweep.
PCTS: list[float | None] = [None, 0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75]


def subset_metrics(runs: list[dict[str, RawRow]], fixtures: list[tuple[str, int]],
                   pct: float | None) -> tuple[float, float, int]:
    """(MAPE, mean signed err, n_estimated) over a fixture subset for one pct."""
    per_abs: list[float] = []
    per_signed: list[float] = []
    for q, official in fixtures:
        abs_errs: list[float] = []
        signed: list[float] = []
        skip = False
        for run in runs:
            row = run[q]
            if row[4] or row[0] == 0:
                skip = True
                break
            sug, _, _ = apply_floor(row, pct)
            abs_errs.append(abs(sug - official) / official * 100.0)
            signed.append((sug - official) / official * 100.0)
        if skip or not abs_errs:
            continue
        per_abs.append(statistics.mean(abs_errs))
        per_signed.append(statistics.mean(signed))
    if not per_abs:
        return 0.0, 0.0, 0
    return statistics.mean(per_abs), statistics.mean(per_signed), len(per_abs)


def label(pct: float | None) -> str:
    return "none" if pct is None else f"p{int(pct * 100)}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Anchor-floor percentile sweep.")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be >= 1")

    config = load_config()
    providers = build_providers(config, with_chat=True)
    assert providers.chat is not None, "needs chat; check chat_api_key in .env"
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

    runs: list[dict[str, RawRow]] = []
    try:
        for r in range(args.runs):
            print(f"===== run {r + 1}/{args.runs} =====", file=sys.stderr)
            runs.append(run_once(est))
    except InvalidRun as exc:
        print(f"\nINVALID run: {exc}; not reporting.", file=sys.stderr)
        sys.exit(2)

    allf = list(FIXTURES)
    premium = [(q, o) for q, o in FIXTURES if is_premium(q)]
    normal = [(q, o) for q, o in FIXTURES if not is_premium(q)]

    print("\n========== SINGLE-PERCENTILE SWEEP (paired, same chat outputs) ==========")
    print(f"  premium fixtures ({len(premium)}): "
          f"{', '.join(q[:14] for q, _ in premium)}")
    print(f"\n  {'knob':>5} | {'ALL signed':>11} {'ALL MAPE':>9} | "
          f"{'NORM signed':>12} {'NORM MAPE':>10} | "
          f"{'PREM signed':>12} {'PREM MAPE':>10}")
    print("  " + "-" * 78)
    for pct in PCTS:
        a_m, a_s, _ = subset_metrics(runs, allf, pct)
        n_m, n_s, _ = subset_metrics(runs, normal, pct)
        p_m, p_s, _ = subset_metrics(runs, premium, pct)
        print(f"  {label(pct):>5} | {a_s:>+10.1f}% {a_m:>8.1f}% | "
              f"{n_s:>+11.1f}% {n_m:>9.1f}% | {p_s:>+11.1f}% {p_m:>9.1f}%")

    print("\n========== READ ==========")
    print("  'signed' near 0 = bias killed; positive = over-corrected.")
    print("  Normal vs Premium columns show the single-knob tension:")
    print("  the knob that zeroes NORM signed will leave PREM still low,")
    print("  and the knob that zeroes PREM signed will push NORM over.")


if __name__ == "__main__":
    main()
