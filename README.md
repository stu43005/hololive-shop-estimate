# Estimator King

Shopify store price estimation system with Discord bot integration.

## Architecture

A single bot process owns both the SQLite database and the ChromaDB vector store:

- **Crawler**: Fetches product data from Shopify stores via web scraping, then embeds product descriptions into ChromaDB for semantic search
- **Discord Bot**: User-facing bot for price estimates; queries ChromaDB and a chat model to generate estimates
- **Database**: SQLite with WAL mode for product state and deduplication tracking
- **Vector Store**: ChromaDB (local) for product-description embeddings used by `/estimate`

## Quick Start

```bash
pip install -r requirements.txt
pytest -q
docker build -t estimator-king .
```

## Project Structure

```
estimator_king/
├── crawler/       # Shopify data fetching + ChromaDB embedding
├── bot/           # Discord bot commands
├── database/      # Data persistence (SQLite)
├── vector/        # ChromaDB client and indexing
└── config.py      # Configuration loading

tests/
├── conftest.py    # Pytest fixtures
├── test_smoke.py  # Smoke tests
└── fixtures/      # Test data
```

## Testing

Run tests with coverage:
```bash
pytest --cov=estimator_king
```

## Configuration

All configuration is via environment variables (copy `.env.example` → `.env`):

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `OPENAI_API_KEY` | *(required)* | OpenAI (or compatible) API key |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Override to point at a local ollama instance |
| `EMBEDDING_MODEL` | `text-embedding-3-large` | Embedding model name |
| `EMBEDDING_DIMENSIONS` | `1024` | Output dimensions for the embedding model |
| `CHAT_MODEL` | `gpt-4o` | Chat / structured-output model |
| `CHAT_STRUCTURED_OUTPUT` | `true` | Set to `false` when using ollama (no JSON schema support) |
| `DATABASE_PATH` | `./estimator_king.db` | SQLite database file path |
| `CHROMA_PATH` | `./chroma` | ChromaDB persistence directory |
| `DISCORD_BOT_TOKEN` | *(required)* | Discord bot token |

### Embedding model recommendation

The default model is `text-embedding-3-large` at 1024 dimensions — a good balance of quality and storage cost for Japanese product names.

To swap to a fully local Japanese embedding model (no API key needed), point the provider at a running [ollama](https://ollama.com) instance:

```dotenv
OPENAI_BASE_URL=http://localhost:11434/v1
OPENAI_API_KEY=ollama
EMBEDDING_MODEL=bge-m3
CHAT_MODEL=<your-local-model>
CHAT_STRUCTURED_OUTPUT=false
```

> **Re-index required when changing the embedding model or `EMBEDDING_DIMENSIONS`:**
> Vectors from different models are incompatible. Delete `chroma/` and re-crawl:
>
> ```bash
> rm -rf chroma/
> python -m estimator_king crawl --force-refetch
> ```

## Daily Crawl Budget

The crawler respects a per-store `max_products_per_run` limit (configured in `stores_config.yaml`).
Each daily run fetches at most that many products per store, rotating through the catalog so every
product is eventually refreshed. This keeps API costs and run time predictable even for large stores.

## Deployment

Single Docker image — the bot and crawler run as one process:

```bash
docker build -t estimator-king .
docker run --env-file .env -v ./data:/data estimator-king
```

For Kubernetes deployment see [docs/ops-runbook.md](docs/ops-runbook.md).
