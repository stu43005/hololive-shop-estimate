"""
Estimator King: Shopify store price estimation system.

Consists of three main components:
- Crawler: Fetches product data from Shopify stores
- Sync: Embeds product data into the local ChromaDB vector store
- Bot: Discord bot for price estimates, with an in-process daily crawl scheduler
"""

__version__ = "0.1.0"
