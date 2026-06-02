"""One-off experiment: does doubling the per-where-query fetch (top_k*2) while
still sending only top_k to the LLM change the result?

Hypothesis: a bigger fetch only adds LOWER-similarity candidates (fetch-rank
11..20), so the final top_k changes ONLY through the rerank bonus:
  - rw=0 (pure similarity): top_k by sim is unchanged -> identical output.
  - rw>0: deep newer candidates can now be promoted -> recency gains set-level
    leverage (the `entered`/Δage that was ~0 at fetch=top_k).

Per query we build the pool at fetch=top_k and fetch=top_k*2, rerank both at
rw in {0.0, 0.05}, and report how many of the final top_k differ + median-date
shift, plus how many final picks came from the deep (rank>=top_k) region.

Run: set -a; source .env; set +a; PYTHONPATH=. .venv/bin/python scripts/analysis/experiment_fetch_mult.py
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any

from estimator_king.config_schema import load_config
from estimator_king.runtime import build_providers
from estimator_king.sync.typing import classify_query

RWS = [0.0, 0.05]

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


def build_pool(vector_store: Any, emb: Any, wheres: list[Any],
               fetch: int) -> list[Any]:
    merged: dict[str, Any] = {}
    for where in wheres:
        for hit in vector_store.query(emb, fetch, where=where):
            prev = merged.get(hit.id)
            if prev is None or hit.distance < prev.distance:
                merged[hit.id] = hit
    return list(merged.values())


def rerank(pool: list[Any], rw: float, top_k: int) -> list[Any]:
    pubs = [int(h.metadata.get("published_at", 0) or 0) for h in pool]
    positive = [p for p in pubs if p > 0]
    min_pub = min(positive) if positive else 0
    max_pub = max(positive) if positive else 0
    span = max_pub - min_pub

    def base(h: Any) -> float:
        sim = 1.0 - h.distance
        pub = int(h.metadata.get("published_at", 0) or 0)
        rec = (pub - min_pub) / span if (span > 0 and pub > 0) else 0.0
        return sim + rw * rec

    return sorted(pool, key=base, reverse=True)[:top_k]


def med_date(hits: list[Any]) -> int:
    pubs = [int(h.metadata.get("published_at", 0) or 0) for h in hits
            if int(h.metadata.get("published_at", 0) or 0) > 0]
    return int(statistics.median(pubs)) if pubs else 0


def main() -> None:
    config = load_config()
    providers = build_providers(config, with_chat=False)
    embedder = providers.embedder
    vector_store = providers.vector_store
    typing_provider = providers.typing_provider

    top_k = getattr(config, "estimator_top_k", 10)
    item_types = config.item_types
    item_types_version = getattr(config, "item_types_version", 0)

    agg: dict[float, dict[str, list[float]]] = {
        rw: {"changed": [], "dage": [], "deep": []} for rw in RWS}

    for q in QUERIES:
        emb = embedder.embed_query(q)
        types = classify_query(
            q, item_types=item_types, item_types_version=item_types_version,
            typing_provider=typing_provider, repository=None,
        )
        wheres: list[dict[str, Any] | None] = [{"item_type": t} for t in types]
        wheres.append(None)

        pool_1x = build_pool(vector_store, emb, wheres, top_k)
        pool_2x = build_pool(vector_store, emb, wheres, top_k * 2)
        ids_1x = {h.id for h in pool_1x}
        print(f"\n=== {q}")
        print(f"  pool: 1x={len(pool_1x)}  2x={len(pool_2x)}  "
              f"(+{len(pool_2x) - len(pool_1x)} deeper candidates)")
        for rw in RWS:
            sel_1x = rerank(pool_1x, rw, top_k)
            sel_2x = rerank(pool_2x, rw, top_k)
            set_1x = {h.id for h in sel_1x}
            set_2x = {h.id for h in sel_2x}
            changed = len(set_2x - set_1x)
            deep = sum(1 for h in sel_2x if h.id not in ids_1x)
            d1, d2 = med_date(sel_1x), med_date(sel_2x)
            dage = (d2 - d1) / 86400.0 if (d1 and d2) else 0.0
            agg[rw]["changed"].append(changed)
            agg[rw]["dage"].append(dage)
            agg[rw]["deep"].append(deep)
            print(f"  rw={rw:<5} final top_k changed={changed}/{top_k}  "
                  f"from_deep(rank>{top_k})={deep}  "
                  f"medDate {ymd(d1)}->{ymd(d2)}  Δage={dage:+.0f}d")

    print("\n========== AGGREGATE (fetch top_k*2 vs top_k) ==========")
    for rw in RWS:
        ch = statistics.mean(agg[rw]["changed"])
        da = statistics.mean(agg[rw]["dage"])
        dp = statistics.mean(agg[rw]["deep"])
        print(f"  rw={rw:<5} mean changed={ch:.2f}/{top_k}  "
              f"mean from_deep={dp:.2f}  mean Δage={da:+.0f}d")


if __name__ == "__main__":
    main()
