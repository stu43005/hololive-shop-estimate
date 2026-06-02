"""One-off calibration: measure base-score gaps and (item_type, price_jpy)
duplicate clusters in real /estimate candidate pools, to pick diversity_weight.

Replicates Estimator._estimate_chunk retrieval faithfully:
  per query line -> classify_query -> N type-filtered queries + 1 plain query
  -> merge dedupe by vector id keeping min distance -> base scores (1-dist + recency).

Run: set -a; source .env; set +a; PYTHONPATH=. .venv/bin/python scripts/analysis/calibrate_diversity_weight.py
"""

from __future__ import annotations

import statistics
from typing import Any

from estimator_king.config_schema import load_config
from estimator_king.runtime import build_providers
from estimator_king.sync.typing import classify_query

MUS = [0.0, 0.02, 0.03, 0.05, 0.10]


def greedy_rerank(pool: list[Any], base_of: dict[str, float], mu: float,
                  top_k: int) -> list[Any]:
    """Replicate the proposed A-scheme greedy MMR (exact (type,price) key,
    progressive count penalty). Tie-break by original pool order (stable)."""
    selected: list[Any] = []
    remaining = list(pool)
    while remaining and len(selected) < top_k:
        best_i = 0
        best_score = None
        for i, h in enumerate(remaining):
            key = (str(h.metadata.get("item_type", "")),
                   int(h.metadata.get("price_jpy", 0) or 0))
            dup = sum(1 for s in selected
                      if (str(s.metadata.get("item_type", "")),
                          int(s.metadata.get("price_jpy", 0) or 0)) == key)
            score = base_of[h.id] - mu * dup
            if best_score is None or score > best_score:
                best_score = score
                best_i = i
        selected.append(remaining.pop(best_i))
    return selected


def distinct_keys(hits: list[Any]) -> int:
    return len({(str(h.metadata.get("item_type", "")),
                 int(h.metadata.get("price_jpy", 0) or 0)) for h in hits})

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


def main() -> None:
    config = load_config()
    providers = build_providers(config, with_chat=False)
    embedder = providers.embedder
    vector_store = providers.vector_store
    typing_provider = providers.typing_provider

    top_k = getattr(config, "estimator_top_k", 10)
    recency_weight = getattr(config, "estimator_recency_weight", 0.05)
    item_types = config.item_types
    item_types_version = getattr(config, "item_types_version", 0)

    all_adjacent_gaps: list[float] = []
    pools_with_dups = 0
    all_cluster_sizes: list[int] = []
    distinct_by_mu: dict[float, list[int]] = {m: [] for m in MUS}

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

        # base scores, faithful to _rerank
        pubs = [int(h.metadata.get("published_at", 0) or 0) for h in pool]
        positive = [p for p in pubs if p > 0]
        min_pub = min(positive) if positive else 0
        max_pub = max(positive) if positive else 0
        span = max_pub - min_pub

        def base(h: Any) -> float:
            sim = 1.0 - h.distance
            pub = int(h.metadata.get("published_at", 0) or 0)
            rec = (pub - min_pub) / span if (span > 0 and pub > 0) else 0.0
            return sim + recency_weight * rec

        base_of = {h.id: base(h) for h in pool}
        ranked = sorted(pool, key=base, reverse=True)
        top = ranked[: max(top_k, 15)]
        bases = [base(h) for h in top]
        sims = [1.0 - h.distance for h in top]
        gaps = [round(bases[i] - bases[i + 1], 4) for i in range(len(bases) - 1)]
        all_adjacent_gaps.extend(gaps)

        # (item_type, price_jpy) clusters in the *selected* top_k window
        keys: dict[tuple[str, int], int] = {}
        for h in ranked[:top_k]:
            k = (str(h.metadata.get("item_type", "")),
                 int(h.metadata.get("price_jpy", 0) or 0))
            keys[k] = keys.get(k, 0) + 1
        dup_clusters = {k: v for k, v in keys.items() if v >= 2}
        if dup_clusters:
            pools_with_dups += 1
            all_cluster_sizes.extend(dup_clusters.values())

        print(f"\n=== {q}")
        print(f"  types={types}  pool={len(pool)}  top_k_window={top_k}")
        print(f"  sim(top): {[round(s, 3) for s in sims[:top_k]]}")
        print(f"  base(top): {[round(b, 3) for b in bases[:top_k]]}")
        print(f"  adjacent base gaps(top {len(gaps)}): {gaps}")
        if gaps:
            print(f"  median gap={statistics.median(gaps):.4f}  "
                  f"min={min(gaps):.4f}  max={max(gaps):.4f}")
        print(f"  (type,price) dup clusters in top_k: "
              f"{ {f'{k[0]}/¥{k[1]}': v for k, v in dup_clusters.items()} }")
        sim_line = []
        for m in MUS:
            sel = greedy_rerank(pool, base_of, m, top_k)
            d = distinct_keys(sel)
            distinct_by_mu[m].append(d)
            sim_line.append(f"μ={m}:{d}")
        print(f"  distinct (type,price) in top_k by μ: {'  '.join(sim_line)}")

    print("\n\n========== AGGREGATE ==========")
    if all_adjacent_gaps:
        sg = sorted(all_adjacent_gaps)
        n = len(sg)
        print(f"adjacent base gaps: n={n}")
        print(f"  median={statistics.median(sg):.4f}")
        print(f"  p25={sg[n // 4]:.4f}  p75={sg[3 * n // 4]:.4f}  "
              f"p90={sg[min(n - 1, 9 * n // 10)]:.4f}")
        print(f"  mean={statistics.mean(sg):.4f}")
    print(f"pools with >=1 (type,price) dup cluster in top_k: "
          f"{pools_with_dups}/{len(QUERIES)}")
    if all_cluster_sizes:
        print(f"dup cluster sizes: {sorted(all_cluster_sizes, reverse=True)}  "
              f"(max={max(all_cluster_sizes)}, mean={statistics.mean(all_cluster_sizes):.2f})")
    print("\nmean distinct (type,price) keys in top_k by μ "
          f"(top_k={top_k}, higher=more diverse):")
    for m in MUS:
        vals = distinct_by_mu[m]
        print(f"  μ={m}: mean={statistics.mean(vals):.2f}  per_query={vals}")


if __name__ == "__main__":
    main()
