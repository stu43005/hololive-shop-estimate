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

from estimator_king.bot.estimator import SYSTEM_PROMPT, Estimator, _snap_estimate
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


def build_context(est: Estimator, query: str, official: int) -> tuple[str, list[str]]:
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
    refs = "\n".join(est._format_reference(h) for h in ranked)
    return f"### Query: {query}\n{refs or '(no matches)'}", list(selves.values())


def run_once(est: Estimator) -> dict[str, tuple[int, bool, str, bool]]:
    """One full pass over FIXTURES. Returns {query: (snapped_jpy, in_range,
    confidence, no_estimate)}; no_estimate is the model's RAW suggested == 0
    checked BEFORE snapping, so a malformed ¥1-¥54 counts as a large error, not
    a hidden no-estimate.

    Raises InvalidRun on any embedding, vector-retrieval, or chat failure, on a
    dropped line, or when the aligned result count does not equal the fixture
    count (fail-closed). classify_query never raises -- it degrades to the
    'その他' bucket exactly as production does -- so a classification API hiccup
    does not invalidate a run."""
    out: dict[str, tuple[int, bool, str, bool]] = {}
    try:
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
                    raise InvalidRun(f"chat returned no estimate for {query!r}")
                no_est = est_obj.suggested_price_jpy == 0
                snapped = _snap_estimate(est_obj)
                in_range = (snapped.price_range_jpy.min <= official
                            <= snapped.price_range_jpy.max)
                out[query] = (snapped.suggested_price_jpy, in_range,
                              est_obj.confidence, no_est)
        if len(out) != len(FIXTURES):
            raise InvalidRun(f"aligned {len(out)} of {len(FIXTURES)} fixtures")
    except InvalidRun:
        raise
    except Exception as exc:  # fail-closed: embedding/vector/chat failure
        raise InvalidRun(f"run failed: {exc}") from exc
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate /estimate accuracy.")
    parser.add_argument("--runs", type=int, default=3,
                        help="runs per fixture (>=3 for ship decisions)")
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

    per_fixture: dict[str, list[tuple[int, bool, str, bool]]] = {q: [] for q, _ in FIXTURES}
    try:
        for r in range(args.runs):
            print(f"\n===== run {r + 1}/{args.runs} =====")
            result = run_once(est)
            for q, _ in FIXTURES:
                per_fixture[q].append(result[q])
    except InvalidRun as exc:
        print(f"\nINVALID run: {exc}; not reporting summary.", file=sys.stderr)
        sys.exit(2)

    # no-estimate set: a fixture whose RAW suggested was ¥0 in ANY run (conservative).
    no_estimate = {q for q, vals in per_fixture.items() if any(v[3] for v in vals)}
    majority = args.runs // 2 + 1

    per_fixture_err: dict[str, float] = {}
    per_fixture_signed: dict[str, float] = {}
    covered_fixtures = 0
    rows: list[tuple[str, int, int, float | None, str, str]] = []
    for q, official in FIXTURES:
        vals = per_fixture[q]
        conf = statistics.mode([v[2] for v in vals])
        in_range_str = f"{sum(1 for v in vals if v[1])}/{len(vals)}"
        if q in no_estimate:
            rows.append((q, 0, official, None, in_range_str, conf))
            continue
        prices = [v[0] for v in vals]
        abs_errs = [abs(p - official) / official * 100.0 for p in prices]
        signed = [(p - official) / official * 100.0 for p in prices]
        per_fixture_err[q] = statistics.mean(abs_errs)
        per_fixture_signed[q] = statistics.mean(signed)
        if sum(1 for v in vals if v[1]) >= majority:
            covered_fixtures += 1
        rows.append((q, round(statistics.mean(prices)), official,
                     per_fixture_err[q], in_range_str, conf))

    print("\n\n========== PER-FIXTURE (mean over runs) ==========")
    print(f"  {'query':<46} {'est':>7} {'official':>8} {'err%':>7} "
          f"{'in-rng':>7} {'conf':>6}")
    for q, est_price, official, mean_abs, in_range_str, conf in rows:
        err = "n/a" if mean_abs is None else f"{mean_abs:.1f}"
        marker = "  NO-EST" if q in no_estimate else ""
        print(f"  {q[:46]:<46} {est_price:>7} {official:>8} {err:>7} "
              f"{in_range_str:>7} {conf:>6}{marker}")

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
        print(f"  range coverage (per-fixture majority): "
              f"{covered_fixtures}/{len(errs)} "
              f"({covered_fixtures / len(errs) * 100:.0f}%)")
    # Always print the no-estimate set (even empty) for baseline/candidate subset diff.
    print(f"  no-estimate fixtures: {sorted(no_estimate)}")

    prompt_hash = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:8]
    print("\n========== PROVENANCE ==========")
    print(f"  prompt_hash: {prompt_hash}")
    print(f"  git_commit: {_git(['rev-parse', '--short', 'HEAD'])}   "
          f"dirty: {bool(_git(['status', '--porcelain']))}")
    print(f"  embedding_model: {config.embedding_model}   "
          f"chat_model: {config.chat_model}")
    print(f"  fixtures: {len(FIXTURES)}   runs: {args.runs}")


if __name__ == "__main__":
    main()
