---
name: update-item-types
description: >-
  Discover and add recurring product types to the item_types list in
  stores_config.yaml by mining the 'その他' (unclassified) bucket in the live
  DB, semantically clustering the candidates with a small model, and letting the
  user pick what to add. Use this whenever the user wants to update/refresh/grow
  the item-type vocabulary, find item types that show up across many product
  names, reduce how much merch falls into 'その他', or mentions that a product
  category (desk light, amulet, perfume, gaming mouse, etc.) isn't being
  recognized — even if they don't name the script or the config file. This is
  the right tool any time the goal is "find the item types we're missing and get
  them into the config".
---

# Update item_types in stores_config.yaml

`stores_config.yaml` holds an `item_types:` list — the controlled vocabulary the
two-tier item classifier (`estimator_king/sync/typing.py`) uses. Tier 1 does a
deterministic longest-substring match against this list; only names that don't
match fall through to the LLM tier, and anything the LLM can't place lands in
`その他`. Growing this list is how the estimator learns to recognize new product
categories.

The raw `その他` samples are noisy (talent names, event/campaign labels, version
markers all recur), which is why `repository.list_other_typed` alone isn't
enough. This skill does a coarse frequency mine, then leans on a small model to
cluster the survivors into clean candidate types, then puts the decision in the
user's hands before touching the config.

## Why a version bump is part of adding types

The classification cache key includes `item_types_version`
(`typing.py` `_cache_key`), and per the repo's re-index rule, bumping
`item_types_version` forces a full re-index on the next crawl. **Adding names to
`item_types:` without bumping the version is inert for products that are already
indexed and unchanged** — they keep their stale `その他` until their content
changes. So this skill bumps `item_types_version` by 1 whenever it adds names,
so the new vocabulary actually takes effect on the next crawl. (The next crawl
will be a full re-fetch + re-classify; that cost is expected and is the point.)

## Workflow

### 1. Mine candidates from the DB

```bash
.venv/bin/python -m scripts.mine_item_types
```

Read-only — it opens the SQLite DB in `mode=ro` and never writes or migrates.
It prints JSON: `total_other_samples`, the current `known_item_types`, and a
ranked `candidates` list where each entry is a trailing-token `phrase`, its
`frequency` (how many distinct `その他` names end with it), and a few `examples`.

Tune `--min-freq` (default 3) to widen or narrow the list, `--examples` for more
context per candidate. No `.env` is needed — it only reads `database_path` and
`talents`/`item_types` from `stores_config.yaml`.

### 2. Cluster the candidates with a small model

The mined list still mixes surface variants
(`ちびキャライラストアクリルフィギュア`), already-covered types, and residual
noise. Hand the JSON to a **haiku subagent** for semantic clustering. Spawn it
with the Agent tool (`model: haiku`), passing the full candidate JSON and the
`known_item_types` list, and ask it to return a clean, deduped candidate list:

> You are given a JSON list of candidate Japanese product-type tokens mined from
> a store's unclassified merch, plus the list of item types already in the
> config. Produce a clean list of **new** item types worth adding.
>
> Rules:
> - Normalize each candidate to its canonical product-type noun (e.g.
>   `ちびキャライラストアクリルフィギュア` → `アクリルフィギュア`). Merge surface
>   variants that mean the same product into one entry, summing their frequencies.
> - Drop any candidate already covered by an existing item type — Tier 1 matches
>   by substring, so if an existing type is a substring of the candidate
>   (e.g. `キーボード` covers `ゲーミングキーボード`), it's already handled.
>   Use judgment: `リング` does NOT cover `リングライト` (different product).
> - Drop residual noise that slipped the coarse filter: song/event/title
>   fragments, group names, anything that isn't a thing being sold.
> - Keep names at the granularity the config uses (concrete product nouns, not
>   over-general categories).
>
> Return JSON: a list of objects `{name, total_frequency, rationale, example}`,
> sorted by total_frequency descending.

### 3. Let the user pick

Present the clustered list to the user as a table (name, frequency, example,
why). Ask which they want to add — do **not** add anything without explicit
confirmation. The user may pick a subset, rename entries, or skip the run.

### 4. Apply and commit

For the confirmed names:

1. **Read `stores_config.yaml`**, then with the Edit tool append each confirmed
   name as a new `  - <name>` line at the end of the existing `item_types:`
   block (insertion order — the list is not sorted; do not reorder existing
   entries).
2. **Bump `item_types_version` by 1** in the same file (see the section above —
   this is required for the additions to take effect).
3. Commit **only** `stores_config.yaml` via the **git-master** skill, with a
   message like
   `feat(config): add N mined item type(s) and bump item_types_version`.
   Do not `git add -A`; add by path. If nothing was confirmed, make no edit and
   no commit.

## After running

- Tell the user which names were added and the new `item_types_version`.
- Remind them the additions take effect on the **next crawl**, which will be a
  full re-index because of the version bump (`crawl --force-refetch` or the next
  scheduled cycle). No `rm -rf chroma/` is needed — that's only for
  indexing-model / vector-scheme changes, not an item_types change.
- Note that the mined `その他` data reflects past crawls and may lag the current
  config; some candidates may already be partly covered. The clustering step and
  the user's review are what catch that.
