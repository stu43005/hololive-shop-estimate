---
name: update-talents
description: >-
  Refresh the talent-gated dedup list in stores_config.yaml by mining each
  store's official collection pages and adding any newly-debuted talents
  (add-only — never removes existing names). Use this whenever the user wants to
  update/refresh/sync the talent list, pull in new debuts, or mentions that a new
  hololive/vspo/holostars member should be recognized — even if they don't name
  the script or the config file explicitly. This is the right tool any time the
  goal is "get the latest talents into the config".
---

# Update talents in stores_config.yaml

`stores_config.yaml` holds a `talents:` list used for talent-gated dedup
(`estimator_king/sync/items.py` matches each name as a whitespace-delimited token
against product variant titles). When new talents debut, their names must be
added so the estimator deduplicates their merch correctly.

This skill runs the collection-page miner and merges its results into the config
**add-only**: it never removes existing entries, so graduated talents (no live
collection but still appearing in product names) and any hand-added names are
preserved across runs.

## How to run

```bash
.venv/bin/python -m scripts.update_talents_config
```

That single command does everything:

1. Mines the authoritative talent display names from each store's official
   collection pages via `mine_from_stores()` (hololive `/pages/talent`; vspo
   `/collections/members` + `/collections/en-members`). HTTP goes through the
   crawler's `AsyncHTTPClient`, so it is rate-limited per `CrawlerPolicy` and
   **takes roughly 2 minutes** — this is expected, not a hang.
2. Computes the union with the current `talents:` (add-only).
3. If there are new names: rewrites only the `talents:` block at the string level
   (surrounding comments / indentation / other keys are left byte-for-byte
   unchanged — it does **not** use `yaml.dump`), then commits **only**
   `stores_config.yaml` with a message like
   `chore(config): add N mined talent(s) to dedup list`.
4. If there are no new names: prints `talents up to date …` and makes no change
   and no commit.

No `.env` is needed — mining only reads the crawler policy from
`stores_config.yaml`.

## After running

- Report the added names to the user (the script prints them as `+ <name>`), or
  that the list was already up to date.
- If stderr shows any `warning: skipping <url>: …` lines, some collections
  exhausted their retries (rate limit / WAF / circuit breaker). The run still
  succeeds, but those talents may be missing — mention it and consider re-running.
- The config change is already committed by the script; do not commit it again.
- The commit captures the working-tree state of `stores_config.yaml`, so make sure
  there are no unrelated pending edits in that file before running — they would be
  swept into the auto-commit. Other files are unaffected (only this path is committed).
