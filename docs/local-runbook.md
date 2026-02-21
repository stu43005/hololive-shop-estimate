# Estimator King Local Execution Runbook

This runbook explains how to run the Estimator King crawler and Discord bot directly on a local machine (without Kubernetes), using a `.env` file for environment configuration.

For Kubernetes deployment, see [ops-runbook.md](ops-runbook.md).

---

## 1. Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.11+ | `python --version` to verify |
| pip | latest | `pip install --upgrade pip` |
| Git | any | For cloning the repo |
| Dify instance | — | Running and accessible (see [dify-dataset-setup.md](dify-dataset-setup.md)) |
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
python -m pytest -q
```

Expected: `237 passed, 5 failed` (5 pre-existing CLI test failures are known).

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
# ── Crawler (required for crawling) ──────────────────────────
DIFY_BASE_URL=https://dify.long-cod.ts.net/v1
DIFY_API_KEY=dataset-xxxxxxxxxxxxxxxxxxxxxxxx
DIFY_DATASET_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
DATABASE_PATH=./estimator_king.db

# ── Discord Bot (required for bot) ───────────────────────────
DISCORD_BOT_TOKEN=MTIzNDU2Nzg5MDEyMzQ1Njc4OQ.XXXXXX.XXXXXXXX
DIFY_WORKFLOW_API_KEY=app-xxxxxxxxxxxxxxxxxxxxxxxx
```

| Variable | Used By | Description |
|----------|---------|-------------|
| `DIFY_BASE_URL` | Crawler | Dify API endpoint (e.g. `https://dify.long-cod.ts.net/v1`) |
| `DIFY_API_KEY` | Crawler | Dify dataset API key (`dataset-*` format) |
| `DIFY_DATASET_ID` | Crawler | UUID of the target Dify Knowledge Base dataset |
| `DATABASE_PATH` | Crawler | Path to SQLite database file (default: `./estimator_king.db`) |
| `DISCORD_BOT_TOKEN` | Bot | Discord bot authentication token |
| `DIFY_WORKFLOW_API_KEY` | Bot | Dify workflow API key (`app-*` format) |

> **Note**: The bot's workflow base URL is hardcoded to `https://dify.long-cod.ts.net/v1` in `estimator_king/bot/workflow_client.py`. If your Dify instance is at a different address, edit `DEFAULT_BASE_URL` in that file.

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
python -m estimator_king --config stores_config.yaml
```

### 4.2 CLI Options

```
python -m estimator_king [OPTIONS]

Options:
  --config PATH        Stores config YAML (default: stores_config.yaml)
  --db PATH            SQLite database path (env: DATABASE_PATH, default: ./estimator_king.db)
  --dify-base-url URL  Dify API base URL (env: DIFY_BASE_URL, required)
  --dify-api-key KEY   Dify dataset API key (env: DIFY_API_KEY, required)
  --dify-dataset-id ID Dify dataset UUID (env: DIFY_DATASET_ID, required)
```

CLI arguments override environment variables.

### 4.3 Output

- **Logs**: Printed to `stderr` (structured format: `timestamp - LEVEL - message`)
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
python -m estimator_king --config stores_config.yaml > result.json
```

### 4.4 Database Location

The crawler creates/updates an SQLite database (WAL mode) at the path specified by `--db` or `DATABASE_PATH`. Default: `./estimator_king.db`.

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
python -m estimator_king.bot
```

### 5.2 CLI Options

```
python -m estimator_king.bot [OPTIONS]

Options:
  --token TOKEN      Discord bot token (env: DISCORD_BOT_TOKEN, required)
  --guild-id ID      Guild ID for fast command sync (optional)
```

### 5.3 Development vs Production Sync

| Mode | Command | Propagation |
|------|---------|-------------|
| Development | `python -m estimator_king.bot --guild-id 123456789` | Instant (guild-specific) |
| Production | `python -m estimator_king.bot` | Up to 1 hour (global) |

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

python -m estimator_king --config stores_config.yaml "$@"
```

### `run-bot.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
set -a; source "$(dirname "$0")/.env"; set +a

python -m estimator_king.bot "$@"
```

Make them executable:

```bash
chmod +x run-crawler.sh run-bot.sh
```

> These scripts are optional — you don't need to commit them. They're a convenience for local development.

---

## 7. Smoke Tests & Verification

### 7.1 Verify Dify Connectivity (Crawler)

```bash
set -a; source .env; set +a

curl -s -o /dev/null -w "%{http_code}" \
  "${DIFY_BASE_URL}/datasets" \
  -H "Authorization: Bearer ${DIFY_API_KEY}"
```

**Expected**: `200`

### 7.2 Dry-Run Crawler

Run the crawler and check the JSON output:

```bash
python -m estimator_king --config stores_config.yaml 2>crawler.log | python -m json.tool
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
python -m pytest -q --tb=short
```

**Expected**: 237 passed, 5 failed (5 pre-existing CLI test failures).

---

## 8. Troubleshooting

### Crawler: `--dify-api-key or DIFY_API_KEY environment variable required`

Environment variables are not loaded. Ensure you ran:

```bash
set -a; source .env; set +a
```

Verify with: `echo $DIFY_API_KEY`

### Crawler: `Failed to enumerate sitemap for <store_id>`

- Check internet connectivity
- Verify the store URL is accessible: `curl -I https://shop.hololivepro.com/sitemap.xml`
- If behind a proxy, set `proxy.enabled: true` in `stores_config.yaml` and configure `HTTP_PROXY` / `HTTPS_PROXY`

### Crawler: `Sync completed ... +0 created, ~0 updated, =N skipped`

This is normal when no products have changed since the last crawl. The crawler uses content-hash (SHA-256) change detection.

### Bot: `Error: --token required or set DISCORD_BOT_TOKEN`

The `DISCORD_BOT_TOKEN` environment variable is not set. Load `.env` before starting the bot.

### Bot: `Missing DIFY_WORKFLOW_API_KEY`

The bot reads `DIFY_WORKFLOW_API_KEY` from the environment at command invocation time. Ensure it's set in `.env` and loaded.

### Bot: Commands Not Appearing in Discord

- **With `--guild-id`**: Commands sync instantly but only in that guild. Verify the guild ID is correct.
- **Without `--guild-id`**: Global sync takes up to 1 hour. Wait and retry.
- Ensure the bot has the `applications.commands` scope in its OAuth2 URL.

### SQLite: `database is locked`

Another crawler instance may be running. Only one crawler process should access the database at a time. Check with:

```bash
ps aux | grep estimator_king
```

---

## 9. Comparison: Local vs Kubernetes

| Aspect | Local (this runbook) | Kubernetes ([ops-runbook.md](ops-runbook.md)) |
|--------|---------------------|-----------------------------------------------|
| Secrets | `.env` file (gitignored) | K8s Secret (`estimator-king-secrets`) |
| Config | `stores_config.yaml` in project root | ConfigMap (`estimator-king-stores-config`) |
| Scheduling | Manual / cron | CronJob (weekly) |
| Bot lifecycle | Manual start/stop | Deployment with auto-restart |
| Database | Local file (`./estimator_king.db`) | PVC (`estimator-king-state-pvc`) |
| Logs | Terminal stdout/stderr | `kubectl logs` |
