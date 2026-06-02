"""One-off calibration: joint grid over fetch-multiplier x recency_weight x
diversity_weight, validating the proposed greedy-MMR rerank (design spec §3)
together with the fetch-size lever.

Scoring under validation (design spec §3):
  base(h)   = (1 - dist) + recency_weight * recency_norm(h)   # recency unchanged
  greedy    : repeatedly pick argmax  base(h) - diversity_weight * dup_count(h)
              where dup_count = #already-selected with same (item_type, price_jpy)
  fetch     : each where-query pulls top_k*mult candidates into the pool

Per (fetch, rw, dw) we measure the final top_k vs the pure-similarity baseline
(fetch=1x, rw=0, dw=0):
  distinct = distinct (item_type, price_jpy) keys      -> diversity benefit
  dAge     = median-published_at shift (days newer)     -> recency / anti-inflation
  dSim     = mean-similarity change                     -> relevance cost
  changed  = how many of the final top_k differ

Run: set -a; source .env; set +a; PYTHONPATH=. .venv/bin/python scripts/analysis/calibrate_rerank_grid.py
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any

from estimator_king.config_schema import load_config
from estimator_king.runtime import build_providers
from estimator_king.sync.typing import classify_query

FETCHES = [1, 2]
RWS = [0.0, 0.05]
DWS = [0.0, 0.05, 0.10]

# the named points we care about, for the spotlight per-query table
SPOTLIGHT = (2, 0.05, 0.05)

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
    return "?" if epoch <= 0 else datetime.fromtimestamp(
        epoch, tz=timezone.utc).strftime("%Y-%m")


def key_of(h: Any) -> tuple[str, int]:
    return (str(h.metadata.get("item_type", "")),
            int(h.metadata.get("price_jpy", 0) or 0))


def build_pool(vector_store: Any, emb: Any, wheres: list[Any],
               fetch: int) -> list[Any]:
    merged: dict[str, Any] = {}
    for where in wheres:
        for hit in vector_store.query(emb, fetch, where=where):
            prev = merged.get(hit.id)
            if prev is None or hit.distance < prev.distance:
                merged[hit.id] = hit
    return list(merged.values())


def base_scores(pool: list[Any], rw: float) -> dict[str, float]:
    pubs = [int(h.metadata.get("published_at", 0) or 0) for h in pool]
    positive = [p for p in pubs if p > 0]
    min_pub = min(positive) if positive else 0
    max_pub = max(positive) if positive else 0
    span = max_pub - min_pub
    out: dict[str, float] = {}
    for h in pool:
        sim = 1.0 - h.distance
        pub = int(h.metadata.get("published_at", 0) or 0)
        rec = (pub - min_pub) / span if (span > 0 and pub > 0) else 0.0
        out[h.id] = sim + rw * rec
    return out


def greedy_rerank(pool: list[Any], base_of: dict[str, float], dw: float,
                  top_k: int) -> list[Any]:
    """Design spec §3 greedy MMR: exact (type,price) key, progressive count
    penalty, stable tie-break by original pool order. dw=0 -> plain sort."""
    selected: list[Any] = []
    remaining = list(pool)
    while remaining and len(selected) < top_k:
        best_i = 0
        best_score = None
        for i, h in enumerate(remaining):
            dup = sum(1 for s in selected if key_of(s) == key_of(h))
            score = base_of[h.id] - dw * dup
            if best_score is None or score > best_score:
                best_score = score
                best_i = i
        selected.append(remaining.pop(best_i))
    return selected


def med_pub(hits: list[Any]) -> int:
    pubs = [int(h.metadata.get("published_at", 0) or 0) for h in hits
            if int(h.metadata.get("published_at", 0) or 0) > 0]
    return int(statistics.median(pubs)) if pubs else 0


def mean_sim(hits: list[Any]) -> float:
    return statistics.mean([1.0 - h.distance for h in hits]) if hits else 0.0


def main() -> None:
    config = load_config()
    providers = build_providers(config, with_chat=False)
    embedder = providers.embedder
    vector_store = providers.vector_store
    typing_provider = providers.typing_provider

    top_k = getattr(config, "estimator_top_k", 10)
    item_types = config.item_types
    item_types_version = getattr(config, "item_types_version", 0)

    configs = [(f, rw, dw) for f in FETCHES for rw in RWS for dw in DWS]
    agg: dict[tuple[int, float, float], dict[str, list[float]]] = {
        c: {"distinct": [], "dage": [], "dsim": [], "changed": []}
        for c in configs}
    spot_rows: list[str] = []

    for q in QUERIES:
        emb = embedder.embed_query(q)
        types = classify_query(
            q, item_types=item_types, item_types_version=item_types_version,
            typing_provider=typing_provider, repository=None,
        )
        wheres: list[dict[str, Any] | None] = [{"item_type": t} for t in types]
        wheres.append(None)

        pools = {f: build_pool(vector_store, emb, wheres, top_k * f)
                 for f in FETCHES}

        # baseline: pure similarity, fetch=1x (rw=0, dw=0)
        base1 = base_scores(pools[1], 0.0)
        ref = greedy_rerank(pools[1], base1, 0.0, top_k)
        ref_ids = {h.id for h in ref}
        ref_med = med_pub(ref)
        ref_sim = mean_sim(ref)

        for (f, rw, dw) in configs:
            pool = pools[f]
            bof = base_scores(pool, rw)
            sel = greedy_rerank(pool, bof, dw, top_k)
            distinct = len({key_of(h) for h in sel})
            md = med_pub(sel)
            dage = (md - ref_med) / 86400.0 if (md and ref_med) else 0.0
            dsim = mean_sim(sel) - ref_sim
            changed = len({h.id for h in sel} - ref_ids)
            agg[(f, rw, dw)]["distinct"].append(distinct)
            agg[(f, rw, dw)]["dage"].append(dage)
            agg[(f, rw, dw)]["dsim"].append(dsim)
            agg[(f, rw, dw)]["changed"].append(float(changed))
            if (f, rw, dw) == SPOTLIGHT:
                spot_rows.append(
                    f"  {q[:22]:<22} distinct={distinct:>2} "
                    f"Δage={dage:+5.0f}d Δsim={dsim:+.4f} changed={changed}/{top_k}")

    print(f"========== GRID (top_k={top_k}, vs pure-sim 1x baseline) ==========")
    print(f"  {'fetch':<5} {'rw':<5} {'dw':<5} | {'distinct':>8} "
          f"{'Δage(d)':>8} {'Δsim':>9} {'changed':>8}   note")
    notes = {
        (1, 0.0, 0.0): "pure similarity (baseline)",
        (1, 0.05, 0.0): "<- CURRENT production",
        (1, 0.05, 0.05): "design default (no fetch bump)",
        (2, 0.05, 0.05): "design default + fetch x2",
    }
    for c in configs:
        d = agg[c]
        print(f"  {c[0]:<5} {c[1]:<5} {c[2]:<5} | "
              f"{statistics.mean(d['distinct']):>8.2f} "
              f"{statistics.mean(d['dage']):>8.0f} "
              f"{statistics.mean(d['dsim']):>+9.4f} "
              f"{statistics.mean(d['changed']):>8.2f}   {notes.get(c, '')}")

    print(f"\n========== SPOTLIGHT per-query: fetch={SPOTLIGHT[0]}x "
          f"rw={SPOTLIGHT[1]} dw={SPOTLIGHT[2]} ==========")
    for r in spot_rows:
        print(r)


if __name__ == "__main__":
    main()
