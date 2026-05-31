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

### 3.2 `.env` File Reference

```dotenv
# ── Provider ──────────────────────────────────────────────────
OPENAI_API_KEY=sk-...
# OPENAI_BASE_URL=https://api.openai.com/v1   # override for ollama
# EMBEDDING_MODEL=text-embedding-3-large
# EMBEDDING_DIMENSIONS=1024
# CHAT_MODEL=gpt-4o
# CHAT_STRUCTURED_OUTPUT=true

# ── Storage ───────────────────────────────────────────────────
DATABASE_PATH=./estimator_king.db
CHROMA_PATH=./chroma

# ── Discord Bot ───────────────────────────────────────────────
DISCORD_BOT_TOKEN=MTIzNDU2Nzg5MDEyMzQ1Njc4OQ.XXXXXX.XXXXXXXX
```

| Variable | Used By | Description |
| -------- | ------- | ----------- |
| `OPENAI_API_KEY` | Crawler + Bot | API key for the embedding and chat provider |
| `OPENAI_BASE_URL` | Crawler + Bot | Override to `http://localhost:11434/v1` for ollama |
| `EMBEDDING_MODEL` | Crawler | Model used to embed product descriptions (default `text-embedding-3-large`) |
| `EMBEDDING_DIMENSIONS` | Crawler | Output vector dimensions (default `1024`) |
| `CHAT_MODEL` | Bot | Chat model for `/estimate` (default `gpt-4o`) |
| `CHAT_STRUCTURED_OUTPUT` | Bot | Set to `false` when the model does not support JSON schema |
| `DATABASE_PATH` | Crawler | Path to SQLite database file (default `./estimator_king.db`) |
| `CHROMA_PATH` | Crawler + Bot | ChromaDB persistence directory (default `./chroma`) |
| `DISCORD_BOT_TOKEN` | Bot | Discord bot authentication token |

### 3.3 Loading `.env` into the Shell

The application does **not** auto-load `.env` files. Use one of these approaches:

**Option A — Shell `source` (recommended)**

```bash
set -a            # auto-export all variables
source .env
set +a
```

Run this in every new terminal session before executing the crawler or bot. You can add it to a wrapper script (see Section 6).

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

```bash
# Load env vars first (if not using direnv)
set -a; source .env; set +a

# Run crawler with default config
.venv/bin/python -m estimator_king crawl --config stores_config.yaml
```

### 4.2 CLI Options

```
python -m estimator_king crawl [OPTIONS]

Options:
  --config PATH         Stores config YAML (default: stores_config.yaml)
  --db PATH             SQLite database path (env: DATABASE_PATH, default: ./estimator_king.db)
  --force-refetch       Re-fetch every product regardless of content hash
```

CLI arguments override environment variables.

### 4.3 Daily Budget Crawl

The crawler respects a per-store `max_products_per_run` limit defined in `stores_config.yaml`. Each run fetches at most that many products per store, rotating through the catalog so every product is eventually refreshed. This keeps API and embedding costs predictable even for large stores.

To trigger a full one-cycle backfill (re-fetch all products, regardless of the daily budget):

```bash
.venv/bin/python -m estimator_king crawl --force-refetch
```

### 4.4 Output

- **Logs**: Printed to `stderr` (structured format: `timestamp [LEVEL] message`)
- **Result JSON**: Printed to `stdout` on completion

Example output (stdout):

```json
{
  "discovered": 120,
  "fetched_ok": 118,
  "created": 15,
  "updated": 3,
  "skipped": 100,
  "inactive": 0,
  "errors": 2
}
```

To capture the JSON result while still seeing logs:

```bash
.venv/bin/python -m estimator_king crawl --config stores_config.yaml > result.json
```

### 4.5 Database and Vector Store Location

The crawler creates/updates:

- An SQLite database (WAL mode) at the path specified by `--db` or `DATABASE_PATH`. Default: `./estimator_king.db`.
- A ChromaDB collection at the directory specified by `CHROMA_PATH`. Default: `./chroma`.

To inspect the database:

```bash
sqlite3 estimator_king.db ".tables"
sqlite3 estimator_king.db "SELECT COUNT(*) FROM product_state;"
```

---

## 5. Running the Discord Bot

### 5.1 Basic Execution

```bash
# Load env vars first
set -a; source .env; set +a

# Run bot
.venv/bin/python -m estimator_king run
```

### 5.2 CLI Options

```
python -m estimator_king run [OPTIONS]

Options:
  --token TOKEN      Discord bot token (env: DISCORD_BOT_TOKEN, required)
  --guild-id ID      Guild ID for fast command sync (optional)
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

## 6. Convenience Wrapper Scripts

### `run-crawler.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
set -a; source "$(dirname "$0")/.env"; set +a

.venv/bin/python -m estimator_king crawl --config stores_config.yaml "$@"
```

### `run-bot.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
set -a; source "$(dirname "$0")/.env"; set +a

.venv/bin/python -m estimator_king run "$@"
```

Make them executable:

```bash
chmod +x run-crawler.sh run-bot.sh
```

> These scripts are optional — you don't need to commit them. They're a convenience for local development.

---

## 7. Smoke Tests & Verification

### 7.1 Verify Provider Connectivity

```bash
set -a; source .env; set +a

curl -s -o /dev/null -w "%{http_code}" \
  "${OPENAI_BASE_URL:-https://api.openai.com}/v1/models" \
  -H "Authorization: Bearer ${OPENAI_API_KEY}"
```

**Expected**: `200`

### 7.2 Dry-Run Crawler

Run the crawler and check the JSON output:

```bash
.venv/bin/python -m estimator_king crawl --config stores_config.yaml 2>crawler.log | python -m json.tool
```

Verify:
- `discovered > 0` — sitemap enumeration worked
- `errors` is 0 or low — no systemic failures
- Check `crawler.log` for detailed per-store results

### 7.3 Verify Bot Token

```bash
set -a; source .env; set +a

curl -s -o /dev/null -w "%{http_code}" \
  "https://discord.com/api/v10/users/@me" \
  -H "Authorization: Bot ${DISCORD_BOT_TOKEN}"
```

**Expected**: `200`

### 7.4 Unit Tests

```bash
.venv/bin/python -m pytest -q --tb=short
```

---

## 8. Re-index Procedure

Vectors from different embedding models or dimension settings are incompatible with each other.
If you change `EMBEDDING_MODEL` or `EMBEDDING_DIMENSIONS`, you must delete the ChromaDB directory
and re-crawl all products from scratch:

```bash
rm -rf chroma/
.venv/bin/python -m estimator_king crawl --force-refetch
```

This will re-fetch every product and rebuild the vector index. Depending on the number of stores
and products, this may take a while and consume embedding API quota.

### Re-index after the item-level indexing upgrade

The vector ID scheme and document format changed (per-item vectors). After deploying:

```bash
rm -rf chroma/
.venv/bin/python -m estimator_king crawl --force-refetch
```

Changing `EMBEDDING_MODEL`/`EMBEDDING_DIMENSIONS` or bumping `item_types_version` in `stores_config.yaml` also triggers a re-index.

---

## 9. Troubleshooting

### Crawler: `OPENAI_API_KEY environment variable required`

Environment variables are not loaded. Ensure you ran:

```bash
set -a; source .env; set +a
```

Verify with: `echo $OPENAI_API_KEY`

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

The existing ChromaDB collection was created with a different embedding model or dimensions. Run the re-index procedure (Section 8).

---

## 10. Comparison: Local vs Kubernetes

| Aspect | Local (this runbook) | Kubernetes ([ops-runbook.md](ops-runbook.md)) |
| ------ | -------------------- | --------------------------------------------- |
| Secrets | `.env` file (gitignored) | K8s Secret (`estimator-king-secrets`) |
| Config | `stores_config.yaml` in project root | ConfigMap (`estimator-king-stores-config`) |
| Scheduling | Manual / cron | CronJob (daily) |
| Bot lifecycle | Manual start/stop | Deployment with auto-restart |
| Database | Local file (`./estimator_king.db`) | PVC (`estimator-king-state-pvc`) |
| Vector store | Local dir (`./chroma`) | PVC (`estimator-king-state-pvc`) |
| Logs | Terminal stdout/stderr | `kubectl logs` |
