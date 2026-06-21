# pyright: reportPrivateUsage=false
"""eval: measure /estimate accuracy on labeled (query, official price) fixtures.

The fixture products have since been ingested into the store DB, so a bare name
query would retrieve the exact same product at sim~1.0 and the model would just
copy its price -> inflated, meaningless accuracy. This script reproduces the
Estimator retrieval path but DROPS each query's own product (price == official
AND an exact-name or high-similarity match) before building the context, so it
measures estimation skill when the DB has no exact match.

Workflow: run once on the current prompt for a baseline; change the prompt; run
again with the same --runs N and the same fixtures; then compare MAPE, range
coverage and the no-estimate set. A run that cannot obtain a complete, aligned
set of estimates exits non-zero (INVALID) and prints no summary, so a broken or
partial batch can never be mistaken for an improvement.

Run: set -a; source .env; set +a; PYTHONPATH=. .venv/bin/python \\
    scripts/analysis/eval_estimate.py --runs 3
"""

from __future__ import annotations

import argparse
import hashlib
import statistics
import subprocess
import sys
from typing import Any

from estimator_king.bot.estimator import (
    SYSTEM_PROMPT,
    Estimator,
    _anchor_floor,
    _same_type_prices,
    _snap_estimate,
)
from estimator_king.config_schema import load_config
from estimator_king.crawler.snapshot import normalize_text
from estimator_king.runtime import build_providers
from estimator_king.sync.typing import classify_query

# (query, official_jpy) labeled fixtures, from two measured /estimate batches.
FIXTURES: list[tuple[str, int]] = [
    # batch A
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
    # batch B
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


class InvalidRun(Exception):
    """A run that could not produce a complete, aligned set of estimates."""


def _git(args: list[str]) -> str:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def build_context(est: Estimator, query: str, official: int) -> tuple[str, list[str], list[int]]:
    """Reproduce _estimate_chunk retrieval for one query with the query's own
    product absent from the index. Production fetches the closest fetch_n hits
    per where-query; to faithfully simulate the self item not being indexed we
    over-fetch, drop the self hits, then keep the closest fetch_n NON-self hits
    per path -- so the candidate that self would otherwise push past the fetch_n
    boundary is still seen. Returns (context_block, excluded_self_descriptions).

    A self hit is the held-out fixture product, identified by exact normalized
    name AND exact official price. The sim>=SELF_SIM_THRESHOLD branch is only a
    backstop for name-format drift on the SAME product. To avoid silently
    dropping a *different* same-priced product (which would make the eval context
    unlike production and corrupt the before/after comparison), the run fails
    closed (InvalidRun) when an exclusion is sim-only with a non-matching name,
    or when more than one distinct product is excluded for one fixture."""
    embedding = est._embedder.embed_query(query)
    types = classify_query(
        query, item_types=est._item_types,
        item_types_version=est._item_types_version,
        typing_provider=est._typing_provider, repository=None,
    )
    queries: list[dict[str, Any] | None] = [{"item_type": t} for t in types]
    queries.append(None)
    nq = normalize_text(query)
    fetch_n = est._top_k * est._fetch_multiplier
    overfetch = fetch_n + est._top_k  # headroom for self hits removed pre-truncation
    merged: dict[str, Any] = {}
    selves: dict[str, str] = {}
    for where in queries:
        non_self: list[Any] = []
        for hit in est._vector_store.query(embedding, overfetch, where=where):
            price = int(hit.metadata.get("price_jpy", 0) or 0)
            name = str(hit.metadata.get("item_name") or "")
            sim = 1.0 - hit.distance
            name_match = normalize_text(name) == nq
            if price == official and (name_match or sim >= SELF_SIM_THRESHOLD):
                ident = (f"{name}|¥{price}|sim={sim:.3f}"
                         f"|store={hit.metadata.get('store_id')}|id={hit.id}")
                if not name_match:
                    raise InvalidRun(
                        f"ambiguous self-exclusion for {query!r}: dropped a "
                        f"same-price high-similarity row whose name does not "
                        f"match [{ident}] -- may be a different product")
                selves[hit.id] = ident
            else:
                non_self.append(hit)
        non_self.sort(key=lambda h: h.distance)
        for hit in non_self[:fetch_n]:
            prev = merged.get(hit.id)
            if prev is None or hit.distance < prev.distance:
                merged[hit.id] = hit
    if len(selves) > 1:
        raise InvalidRun(
            f"multiple distinct products excluded as self for {query!r}: "
            f"{list(selves.values())} -- identity is ambiguous")
    ranked = est._rerank(list(merged.values()))[: est._top_k]
    same_type_prices = _same_type_prices(ranked, types)
    refs = "\n".join(est._format_reference(h) for h in ranked)
    return f"### Query: {query}\n{refs or '(no matches)'}", list(selves.values()), same_type_prices


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


def _metrics(runs: list[dict[str, tuple[Any, list[int], int]]], cfg: Any) -> dict[str, Any]:
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate /estimate accuracy.")
    parser.add_argument("--runs", type=int, default=3,
                        help="runs per fixture (>=3 for ship decisions)")
    parser.add_argument("--baseline-only", action="store_true",
                        help="record disabled baseline numbers without running the acceptance "
                             "gate (use only when no anchor_floor candidate is configured)")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be >= 1")

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
        fails: list[str] = []
        # Require directional bias toward 0 by >= 1pp (also fails over-correction).
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
    elif args.baseline_only:
        print("\n========== BASELINE-ONLY (no candidate floor; gate not run) ==========")
    else:
        print("\n========== ACCEPTANCE: NO CANDIDATE ==========", file=sys.stderr)
        print("  estimator.anchor_floor is not configured; the acceptance gate cannot run.",
              file=sys.stderr)
        print("  Add the anchor_floor block to stores_config.yaml, or pass --baseline-only to "
              "record disabled baseline numbers without gating.", file=sys.stderr)
        sys.exit(4)


if __name__ == "__main__":
    main()
