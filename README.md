# Estimator King

Shopify store price estimation system with Discord bot integration.

## Architecture

- **Crawler**: Fetches product data from Shopify stores via web scraping
- **Sync Engine**: Synchronizes data with Dify workflow engine
- **Discord Bot**: User-facing bot for price estimates and interactions
- **Database**: SQLite with WAL mode for concurrent access

## Quick Start

```bash
pip install -r requirements.txt
pytest -q
docker build -t estimator-king .
```

## Project Structure

```
estimator_king/
├── crawler/       # Shopify data fetching
├── sync/          # Dify integration
├── bot/           # Discord bot commands
├── database/      # Data persistence
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

Set environment variables for:
- `DIFY_API_KEY`: Dify workflow API key
- `DISCORD_TOKEN`: Discord bot token
- `SHOPIFY_STORE_URL`: Target Shopify store URL
- `DATABASE_PATH`: SQLite database file path

## Deployment

Multi-stage Docker builds:
- `crawler`: Product data fetching service
- `bot`: Discord bot service

```bash
docker build --target crawler -t estimator-king-crawler .
docker build --target bot -t estimator-king-bot .
```
