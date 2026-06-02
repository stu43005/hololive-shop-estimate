"""One-off calibration: measure how recency_weight reshapes real /estimate
candidate pools, to pick recency_weight (the anti-inflation knob — prefer newer
prices without letting recency override genuine relevance).

Replicates Estimator._estimate_chunk / _rerank retrieval faithfully:
  per query line -> classify_query -> N type-filtered queries + 1 plain query
  -> merge dedupe by vector id keeping min distance -> base = (1-dist) + rw*recency.

For a sweep of recency_weight values, per pool we measure vs the rw=0 baseline:
  - flips: top_k positions that differ from the pure-similarity order (activity)
  - top1_changed: did recency dethrone the single most-relevant hit
  - delta_age_days: how much NEWER (median published_at) the selected top_k is
                    -> the anti-inflation benefit
  - delta_sim: drop in mean similarity of the selected top_k -> relevance cost

Anchor: the adjacent *similarity* gap distribution (pure 1-dist). recency_weight
is the max boost a newest-in-pool item gets, so it can only flip a pair whose
similarity gap is below recency_weight. Compare the chosen value against the gap
percentiles and the ~0.10 most-relevant-hit cliff.

Run: set -a; source .env; set +a; PYTHONPATH=. .venv/bin/python scripts/analysis/calibrate_recency_weight.py
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any

from estimator_king.config_schema import load_config
from estimator_king.runtime import build_providers
from estimator_king.sync.typing import classify_query

RWS = [0.0, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15, 0.20]

QUERIES = [
    "RIONA ON THE ステージタペストリー",
    "リオナとおそろいネックレス",
    "くしゃみ連発ぬいキーホルダー",
    "博衣こより 誕生日記念 アクリルスタンド",
    "ホロライブ Tシャツ",
    "マフラータオル",
    "缶バッジ こより",
    "白銀ノエル アクリルキーホルダー",
]


def ymd(epoch: int) -> str:
    if epoch <= 0:
        return "?"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m")


def main() -> None:
    config = load_config()
    providers = build_providers(config, with_chat=False)
    embedder = providers.embedder
    vector_store = providers.vector_store
    typing_provider = providers.typing_provider

    top_k = getattr(config, "estimator_top_k", 10)
    item_types = config.item_types
    item_types_version = getattr(config, "item_types_version", 0)

    all_sim_gaps: list[float] = []
    # per-rw aggregates
    flips_by_rw: dict[float, list[int]] = {rw: [] for rw in RWS}
    dage_by_rw: dict[float, list[float]] = {rw: [] for rw in RWS}
    dsim_by_rw: dict[float, list[float]] = {rw: [] for rw in RWS}
    top1_changed_by_rw: dict[float, int] = {rw: 0 for rw in RWS}
    pools_with_recency_room = 0

    for q in QUERIES:
        emb = embedder.embed_query(q)
        types = classify_query(
            q, item_types=item_types, item_types_version=item_types_version,
            typing_provider=typing_provider, repository=None,
        )
        wheres: list[dict[str, Any] | None] = [{"item_type": t} for t in types]
        wheres.append(None)
        merged: dict[str, Any] = {}
        for where in wheres:
            for hit in vector_store.query(emb, top_k, where=where):
                prev = merged.get(hit.id)
                if prev is None or hit.distance < prev.distance:
                    merged[hit.id] = hit
        pool = list(merged.values())

        pubs = [int(h.metadata.get("published_at", 0) or 0) for h in pool]
        positive = [p for p in pubs if p > 0]
        min_pub = min(positive) if positive else 0
        max_pub = max(positive) if positive else 0
        span = max_pub - min_pub
        if span > 0:
            pools_with_recency_room += 1

        def base(h: Any, rw: float) -> float:
            sim = 1.0 - h.distance
            pub = int(h.metadata.get("published_at", 0) or 0)
            rec = (pub - min_pub) / span if (span > 0 and pub > 0) else 0.0
            return sim + rw * rec

        # pure-similarity adjacent gaps in the ranked window (anchor)
        sim_ranked = sorted(pool, key=lambda h: 1.0 - h.distance, reverse=True)
        sims_win = [1.0 - h.distance for h in sim_ranked[: max(top_k, 15)]]
        sim_gaps = [round(sims_win[i] - sims_win[i + 1], 4)
                    for i in range(len(sims_win) - 1)]
        all_sim_gaps.extend(sim_gaps)

        # rw=0 baseline selection (== pure similarity order)
        base_sel: dict[float, list[Any]] = {}
        for rw in RWS:
            base_sel[rw] = sorted(pool, key=lambda h, _rw=rw: base(h, _rw),
                                  reverse=True)[:top_k]
        ref = base_sel[0.0]
        ref_ids = [h.id for h in ref]
        ref_id_set = set(ref_ids)
        ref_pubs = [int(h.metadata.get("published_at", 0) or 0) for h in ref
                    if int(h.metadata.get("published_at", 0) or 0) > 0]
        ref_med_pub = statistics.median(ref_pubs) if ref_pubs else 0
        ref_mean_sim = statistics.mean([1.0 - h.distance for h in ref])

        print(f"\n=== {q}")
        print(f"  types={types}  pool={len(pool)}  span_days={span // 86400}  "
              f"dates={ymd(min_pub)}..{ymd(max_pub)}")
        print(f"  sim(top): {[round(s, 3) for s in sims_win[:top_k]]}")
        if sim_gaps:
            print(f"  sim adjacent gaps: median={statistics.median(sim_gaps):.4f} "
                  f"min={min(sim_gaps):.4f} max={max(sim_gaps):.4f}")
        for rw in RWS:
            sel = base_sel[rw]
            sel_ids = [h.id for h in sel]
            flips = sum(1 for a, b in zip(ref_ids, sel_ids) if a != b)
            entered = len(set(sel_ids) - ref_id_set)
            top1_changed = sel_ids[0] != ref_ids[0]
            sel_pubs = [int(h.metadata.get("published_at", 0) or 0) for h in sel
                        if int(h.metadata.get("published_at", 0) or 0) > 0]
            med_pub = statistics.median(sel_pubs) if sel_pubs else 0
            dage_days = (med_pub - ref_med_pub) / 86400.0 if (med_pub and ref_med_pub) else 0.0
            mean_sim = statistics.mean([1.0 - h.distance for h in sel])
            dsim = mean_sim - ref_mean_sim
            flips_by_rw[rw].append(flips)
            dage_by_rw[rw].append(dage_days)
            dsim_by_rw[rw].append(dsim)
            if top1_changed:
                top1_changed_by_rw[rw] += 1
            print(f"  rw={rw:<5} flips={flips} entered={entered} "
                  f"top1_changed={int(top1_changed)} "
                  f"medDate={ymd(int(med_pub))} Δage={dage_days:+.0f}d "
                  f"meanSim={mean_sim:.4f} Δsim={dsim:+.4f}")

    print("\n\n========== AGGREGATE ==========")
    if all_sim_gaps:
        sg = sorted(all_sim_gaps)
        n = len(sg)
        print(f"adjacent SIMILARITY gaps (pure 1-dist): n={n}")
        print(f"  median={statistics.median(sg):.4f}  "
              f"p25={sg[n // 4]:.4f}  p75={sg[3 * n // 4]:.4f}  "
              f"p90={sg[min(n - 1, 9 * n // 10)]:.4f}  mean={statistics.mean(sg):.4f}")
    print(f"pools with recency room (span>0): {pools_with_recency_room}/{len(QUERIES)}")
    print(f"\nper recency_weight (top_k={top_k}, vs rw=0 baseline):")
    print(f"  {'rw':<6} {'meanFlips':>9} {'top1Chg':>8} "
          f"{'ΔageDays':>9} {'ΔsimMean':>9} {'ΔsimWorst':>10}")
    for rw in RWS:
        fl = statistics.mean(flips_by_rw[rw])
        da = statistics.mean(dage_by_rw[rw])
        ds = statistics.mean(dsim_by_rw[rw])
        dw = min(dsim_by_rw[rw])
        print(f"  {rw:<6} {fl:>9.2f} {top1_changed_by_rw[rw]:>8} "
              f"{da:>9.0f} {ds:>+9.4f} {dw:>+10.4f}")


if __name__ == "__main__":
    main()
