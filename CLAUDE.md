# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Verification toolchain (non-obvious entrypoints)

These tools are **not** listed in `requirements.txt` and the entrypoints are easy to get wrong:

- **Type check**: `.venv/bin/basedpyright <paths>` (reads `pyrightconfig.json`). The venv has **no** `pyright` — `python -m pyright` fails with `No module named pyright`. Type gate is **0 errors in production code (`estimator_king/`)**; test files use duck-typed fakes (`FakeEnumerator`, `FakeEmbedder`, etc.) that produce known `reportArgumentType` noise — that is existing convention, not a regression.
- **Lint**: `uvx ruff check <paths>` (ruff is not in the venv; run it via `uvx`).
- **Single / per-file test**: `.venv/bin/python -m pytest <path> -v -o addopts=""`. `pytest.ini` sets `addopts = --cov=...`; do **not** use `-p no:cov` (the leftover `--cov` then becomes an unrecognized arg). Override the whole thing with `-o addopts=""`.
- **Full suite (with coverage)**: `.venv/bin/python -m pytest`.

Run all three (type check, lint, relevant tests) after any change before claiming completion.

## Running the app

Both subcommands share one process and the `stores_config.yaml` + `.env` config. The app does **not** auto-load `.env` — `set -a; source .env; set +a` first.

```bash
.venv/bin/python -m estimator_king run            # Discord bot + in-process crawl scheduler
.venv/bin/python -m estimator_king crawl          # one crawl cycle, prints JSON counters to stdout, exits
.venv/bin/python -m estimator_king crawl --force-refetch   # re-fetch every product (ignores daily budget)
```

CLI args override env vars. Logs go to **stderr**; the `crawl` result JSON goes to **stdout**. See [docs/local-runbook.md](docs/local-runbook.md) for the full local workflow and [docs/ops-runbook.md](docs/ops-runbook.md) for Kubernetes.

## Architecture

A single bot process owns both the SQLite database (product state / dedup) and the ChromaDB vector store (product-description embeddings). The crawler and the `/estimate` bot command share these stores.

**Crawl cycle** ([crawler/cycle.py](estimator_king/crawler/cycle.py)) — `run_crawl_cycle()` is the shared core, called by both the CLI `crawl` command and the bot's in-process scheduler. Per store: enumerate sitemap → enqueue (new products always; remaining daily budget spent on oldest existing products, unless `--force-refetch` enqueues everything) → drain queue (async fetch → HTML extract → embed → upsert). After all stores, one cross-store inactive sweep marks products inactive after N consecutive fetch failures or sitemap misses (thresholds in `stores_config.yaml`). Each store/queue/sweep stage is wrapped so one store's failure increments `errors` but does not abort the cycle.

**Sync engine** ([sync/engine.py](estimator_king/sync/engine.py)) — `sync_products()` is the **single writer of product rows on the success path**. Change detection uses a SHA-256 content hash (`crawler/snapshot.py`): unchanged + already-indexed products are skipped (no re-embed). On embed/vector failure it logs and continues fire-and-forget — `last_indexed_at` is **not** advanced, but the DB row is still upserted with `consecutive_failures=0` and sitemap-tracking fields carried forward so a fetch never clobbers them.

**Estimator** ([bot/estimator.py](estimator_king/bot/estimator.py)) — `/estimate` embeds each product-name query, retrieves top-K references from ChromaDB, builds a context block, and asks the chat model for structured price estimates. Requests are chunked (`CHUNK_SIZE = 10` product lines per chat call).

**Config** ([config_schema.py](estimator_king/config_schema.py)) — `AppConfig.from_yaml()` reads structural settings (stores, crawler policy, proxy) from YAML and credentials/paths from env vars. `config.validate()` validates **structure only**; each entry point validates the credentials it needs (e.g. `build_provider_config()` then checking `embedding_api_key`). Provider keys cascade: `embedding_api_key`/`chat_api_key` fall back to `openai_api_key`, and base URLs fall back to `openai_base_url` (so pointing `OPENAI_BASE_URL` at ollama's `/v1` swaps the whole provider).

**Bot lifecycle** ([bot/runner.py](estimator_king/bot/runner.py)) — `run_bot()` builds providers, registers slash commands, and starts the `CrawlScheduler` as a background task (kept in a strong-ref set so it isn't GC'd). Two-stage shutdown: first SIGINT/SIGTERM cancels the scheduler and closes the bot gracefully; a second signal forces `os._exit(130)`. The scheduler ([bot/scheduler.py](estimator_king/bot/scheduler.py)) is a guarded asyncio loop (`_running` flag skips overlapping triggers), running every `crawl_schedule_hours`.

## Gotchas

- **Re-index on indexing-model change**: vectors from different models/dimensions are incompatible, AND this build changed the vector ID scheme (one vector per *item*, not per product) and the embedding document format. Any of these requires `rm -rf chroma/` then `crawl --force-refetch`. The SQLite `products` table migrates additively (new `item_types_version` column via idempotent ALTER); bumping `item_types_version` in `stores_config.yaml` forces a full re-index on the next crawl.
- **Single DB writer**: only one crawler process should touch the SQLite DB at a time (WAL mode, but concurrent writers cause `database is locked`).
- **`stores_config.yaml` crawler policy** controls rate limiting, per-domain concurrency, retry, daily budget (`max_products_per_run`), schedule interval, and inactive thresholds — change crawl behavior there, not in code.
- **Fetching live store data (scripts, one-off verification, ad-hoc checks)**: always go through `AsyncHTTPClient` ([crawler/async_http_client.py](estimator_king/crawler/async_http_client.py)) driven by the configured `CrawlerPolicy` — **never** raw `urllib`/`requests`/`aiohttp`. Build it exactly like the crawler ([crawler/cycle.py](estimator_king/crawler/cycle.py)): `config = AppConfig.from_yaml("stores_config.yaml")` then `async with AsyncHTTPClient(config.crawler, proxy=config.proxy) as client: text = await client.get(url)`. This applies per-domain rate limiting + jitter, retry/backoff, the circuit breaker, and the crawler User-Agent. `AppConfig.from_yaml` needs no `.env` (it loads structural YAML and doesn't validate credentials), and `config.crawler`/`config.proxy`/`config.talents` are populated from `stores_config.yaml`. Bare urllib ignores the rate limit and invites a WAF `403`.
- **Prices are pinned to JPY at fetch time**: the crawler appends `?currency=JPY` to the Shopify `.json` request (`crawler/shopify.py` `_FORCE_CURRENCY`), because Shopify Markets otherwise returns geo/locale-converted prices.
