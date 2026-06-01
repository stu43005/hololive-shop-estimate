# Estimator King Local Execution Runbook

This runbook explains how to run the Estimator King crawler and Discord bot directly on a local machine (without Kubernetes), using a `.env` file for environment configuration.

For Kubernetes deployment, see [ops-runbook.md](ops-runbook.md).

---

## 1. Prerequisites

| Requirement | Version | Notes |
| ----------- | ------- | ----- |
| Python | 3.11+ | `python --version` to verify |
| pip | latest | `pip install --upgrade pip` |
| Git | any | For cloning the repo |
| OpenAI API key | — | Or a local [ollama](https://ollama.com) instance for fully offline operation |
| Discord bot token | — | From [Discord Developer Portal](https://discord.com/developers/applications) (bot only) |

---

## 2. Initial Setup

### 2.1 Clone and Create Virtual Environment

```bash
git clone <repo-url> && cd hololive-shop-estimate

python -m venv .venv
source .venv/bin/activate      # macOS / Linux
# .venv\Scripts\activate       # Windows

pip install -r requirements.txt
```

### 2.2 Verify Installation

```bash
.venv/bin/python -m pytest -q
```

---

## 3. Environment Variables via `.env`

### 3.1 Create `.env` File

Copy the template from the project root:

```bash
cp .env.example .env
```

Then edit `.env` with your actual values. The file is already in `.gitignore` — it will never be committed.

### 3.2 `.env` Reference

[`.env.example`](../.env.example) is the authoritative reference — every variable, its default, and the provider fallback rules are documented inline there. Read those comments rather than a copy here. In short: `OPENAI_API_KEY` is required; the `EMBEDDING_*` / `CHAT_*` / `TYPING_*` keys and base URLs fall back to the `OPENAI_*` values when unset (point `OPENAI_BASE_URL` at ollama's `/v1` to swap providers), and `DATABASE_PATH` / `CHROMA_PATH` / `DISCORD_BOT_TOKEN` configure storage and the bot.

### 3.3 Loading `.env` into the Shell

The application does **not** auto-load `.env` files. Use one of these approaches once per shell session — later commands in this runbook assume `.env` is already loaded.

**Option A — Shell `source` (recommended)**

```bash
set -a            # auto-export all variables
source .env
set +a
```

Run this in every new terminal session before executing the crawler or bot.

**Option B — `direnv` (automatic)**

If you use [direnv](https://direnv.net/):

```bash
echo 'dotenv' > .envrc
direnv allow
```

Variables load automatically when you `cd` into the project directory.

---

## 4. Running the Crawler

### 4.1 Basic Execution

After loading `.env` (see §3.3):

```bash
.venv/bin/python -m estimator_king crawl --config stores_config.yaml
```

### 4.2 CLI Options

```
python -m estimator_king crawl [OPTIONS]

Options:
  --config PATH     Stores config YAML (default: stores_config.yaml)
  --log-level LVL   DEBUG | INFO | WARNING | ERROR | CRITICAL (default: INFO)
  --db PATH         Override the database path from config / DATABASE_PATH
  --force-refetch   Re-fetch every active product regardless of content hash
```

CLI arguments override environment variables.

### 4.3 Daily Budget Crawl

The crawler respects a per-store `max_products_per_run` limit defined in `stores_config.yaml`. Each run fetches at most that many products per store, rotating through the catalog so every product is eventually refreshed. This keeps API and embedding costs predictable even for large stores.

To trigger a full one-cycle backfill (re-fetch all products, regardless of the daily budget):

```bash
.venv/bin/python -m estimator_king crawl --force-refetch
```

### 4.4 Database and Vector Store Location

The crawler creates/updates:

- An SQLite database (WAL mode) at the path specified by `--db` or `DATABASE_PATH`. Default: `./estimator_king.db`.
- A ChromaDB collection at the directory specified by `CHROMA_PATH`. Default: `./chroma`.

To inspect the database:

```bash
sqlite3 estimator_king.db ".tables"
sqlite3 estimator_king.db "SELECT COUNT(*) FROM products;"
```

---

## 5. Running the Discord Bot

### 5.1 Basic Execution

After loading `.env` (see §3.3):

```bash
.venv/bin/python -m estimator_king run
```

### 5.2 CLI Options

```
python -m estimator_king run [OPTIONS]

Options:
  --config PATH    Stores config YAML (default: stores_config.yaml)
  --log-level LVL  DEBUG | INFO | WARNING | ERROR | CRITICAL (default: INFO)
  --db PATH        Override the database path from config / DATABASE_PATH
  --token TOKEN    Discord bot token (env: DISCORD_BOT_TOKEN / DISCORD_TOKEN)
  --guild-id ID    Guild ID for fast command sync (optional, omit for global sync)
```

### 5.3 Development vs Production Sync

| Mode | Command | Propagation |
| ---- | ------- | ----------- |
| Development | `python -m estimator_king run --guild-id 123456789` | Instant (guild-specific) |
| Production | `python -m estimator_king run` | Up to 1 hour (global) |

Use `--guild-id` during development for instant slash command updates. Omit it in production for global availability.

### 5.4 Verifying the Bot

1. Check logs for: `Logged in as EstimatorKing#XXXX`
2. Check logs for: `Bot ready and commands synchronized`
3. In Discord, type `/estimate` in a channel where the bot is present
4. A modal should appear for entering product names

### 5.5 Stopping the Bot

Press `Ctrl+C` in the terminal. The bot handles `SIGINT` gracefully.

---

## 6. Smoke Tests & Verification

### 6.1 Verify Provider Connectivity

With `.env` loaded (§3.3):

```bash
curl -s -o /dev/null -w "%{http_code}" \
  "${OPENAI_BASE_URL:-https://api.openai.com}/v1/models" \
  -H "Authorization: Bearer ${OPENAI_API_KEY}"
```

**Expected**: `200`

### 6.2 Verify Bot Token

With `.env` loaded (§3.3):

```bash
curl -s -o /dev/null -w "%{http_code}" \
  "https://discord.com/api/v10/users/@me" \
  -H "Authorization: Bot ${DISCORD_BOT_TOKEN}"
```

**Expected**: `200`

### 6.3 Unit Tests

```bash
.venv/bin/python -m pytest -q --tb=short
```

---

## 7. Re-index Procedure

A re-index re-fetches every product and rebuilds the vector index (this consumes embedding API quota):

```bash
rm -rf chroma/
.venv/bin/python -m estimator_king crawl --force-refetch
```

Delete `chroma/` first whenever the stored vectors are incompatible: changing `EMBEDDING_MODEL` or `EMBEDDING_DIMENSIONS`, or the item-level indexing upgrade (the vector ID scheme and document format changed to per-item vectors). Bumping `item_types_version` in `stores_config.yaml` also forces a full re-index on the next crawl.

To only repair prices crawled in the wrong currency (before the `?currency=JPY` enforcement in `crawler/shopify.py`), run the same `crawl --force-refetch` **without** deleting `chroma/` — the corrected JPY price changes each product's content hash and re-indexes it. Natural daily crawls heal the catalog over time anyway.

---

## 8. Troubleshooting

### Crawler: `OPENAI_API_KEY environment variable required`

Environment variables are not loaded. Load `.env` as shown in §3.3, then verify with `echo $OPENAI_API_KEY`.

### Crawler: `Failed to enumerate sitemap for <store_id>`

- Check internet connectivity
- Verify the store URL is accessible: `curl -I https://shop.hololivepro.com/sitemap.xml`
- If behind a proxy, set `proxy.enabled: true` in `stores_config.yaml` and configure `HTTP_PROXY` / `HTTPS_PROXY`

### Crawler: `Sync completed ... +0 created, ~0 updated, =N skipped`

This is normal when no products have changed since the last crawl. The crawler uses content-hash (SHA-256) change detection.

### Bot: `Error: --token required or set DISCORD_BOT_TOKEN`

The `DISCORD_BOT_TOKEN` environment variable is not set. Load `.env` before starting the bot.

### Bot: Commands Not Appearing in Discord

- **With `--guild-id`**: Commands sync instantly but only in that guild. Verify the guild ID is correct.
- **Without `--guild-id`**: Global sync takes up to 1 hour. Wait and retry.
- Ensure the bot has the `applications.commands` scope in its OAuth2 URL.

### SQLite: `database is locked`

Another crawler instance may be running. Only one crawler process should access the database at a time. Check with:

```bash
ps aux | grep estimator_king
```

### ChromaDB: `dimension mismatch` or `collection already exists with different metadata`

The existing ChromaDB collection was created with a different embedding model or dimensions. Run the re-index procedure (Section 7).

---

## 9. Comparison: Local vs Kubernetes

| Aspect | Local (this runbook) | Kubernetes ([ops-runbook.md](ops-runbook.md)) |
| ------ | -------------------- | --------------------------------------------- |
| Secrets | `.env` file (gitignored) | K8s Secret (`estimator-king-secrets`) |
| Config | `stores_config.yaml` in project root | ConfigMap (`estimator-king-stores-config`) |
| Scheduling | Manual / cron | CronJob (daily) |
| Bot lifecycle | Manual start/stop | Deployment with auto-restart |
| Database | Local file (`./estimator_king.db`) | PVC (`estimator-king-state-pvc`) |
| Vector store | Local dir (`./chroma`) | PVC (`estimator-king-state-pvc`) |
| Logs | Terminal stdout/stderr | `kubectl logs` |
