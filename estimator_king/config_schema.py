"""Configuration schema and validation for Estimator King."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, List
import yaml
import os

if TYPE_CHECKING:
    from estimator_king.llm.config import ProviderConfig


@dataclass
class Store:
    """Store configuration."""

    id: str
    base_url: str
    sitemap_url: str
    locale: str = "default"

    def validate(self):
        """Validate store configuration."""
        if not self.id or not isinstance(self.id, str):
            raise ValueError("Store 'id' must be a non-empty string")
        if not self.base_url or not isinstance(self.base_url, str):
            raise ValueError(f"Store '{self.id}' must have a valid 'base_url'")
        if not self.sitemap_url or not isinstance(self.sitemap_url, str):
            raise ValueError(f"Store '{self.id}' must have a valid 'sitemap_url'")
        if not self.locale or not isinstance(self.locale, str):
            raise ValueError(f"Store '{self.id}' must have a valid 'locale'")


@dataclass
class CrawlerPolicy:
    """Crawler policy configuration."""

    rate_limit_rps: float = 1.5
    jitter_max: float = 0.5
    concurrency_per_domain: int = 3
    timeout_connect: int = 10
    timeout_read: int = 30
    max_retries: int = 3
    max_products_per_run: int = 32
    crawl_schedule_hours: float = 24.0
    inactive_failure_threshold: int = 3
    inactive_sitemap_miss_threshold: int = 4

    def validate(self):
        """Validate crawler policy."""
        if self.rate_limit_rps <= 0:
            raise ValueError("'rate_limit_rps' must be greater than 0")
        if self.jitter_max < 0:
            raise ValueError("'jitter_max' must be non-negative")
        if self.concurrency_per_domain <= 0:
            raise ValueError("'concurrency_per_domain' must be greater than 0")
        if self.timeout_connect <= 0:
            raise ValueError("'timeout_connect' must be greater than 0")
        if self.timeout_read <= 0:
            raise ValueError("'timeout_read' must be greater than 0")
        if self.max_retries < 0:
            raise ValueError("'max_retries' must be non-negative")
        if self.max_products_per_run <= 0:
            raise ValueError("'max_products_per_run' must be greater than 0")
        if self.crawl_schedule_hours <= 0:
            raise ValueError("'crawl_schedule_hours' must be greater than 0")
        if self.inactive_failure_threshold <= 0:
            raise ValueError("'inactive_failure_threshold' must be greater than 0")
        if self.inactive_sitemap_miss_threshold <= 0:
            raise ValueError("'inactive_sitemap_miss_threshold' must be greater than 0")


@dataclass
class ProxyConfig:
    """Proxy configuration (optional)."""

    enabled: bool = False
    http_proxy: str = ""
    https_proxy: str = ""

    def validate(self):
        """Validate proxy configuration."""
        if self.enabled and not (self.http_proxy or self.https_proxy):
            raise ValueError(
                "Proxy is enabled but neither 'http_proxy' nor 'https_proxy' is set"
            )


@dataclass
class AppConfig:
    """Complete application configuration.

    Central configuration object that aggregates YAML-based settings
    (stores, crawler, proxy) and environment-based credentials (providers, Discord).

    Each entry point (crawler, bot) is responsible for validating only the
    fields it actually requires.
    """

    stores: List[Store] = field(default_factory=list)
    crawler: CrawlerPolicy = field(default_factory=CrawlerPolicy)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)

    # Providers / vector store
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_base_url: str | None = None
    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int | None = 1024
    embedding_max_tokens: int = 8192
    embedding_query_prefix: str = ""
    embedding_doc_prefix: str = ""
    chat_api_key: str | None = None
    chat_base_url: str | None = None
    chat_model: str = "gpt-4o"
    chat_structured_output: bool = True
    chroma_path: str = "./chroma"

    # Discord (bot)
    discord_token: Optional[str] = None

    # Database
    database_path: str = "./estimator_king.db"

    def validate(self):
        """Validate structural configuration (stores, crawler, proxy).

        This validates YAML-sourced settings only. Credential validation
        is the responsibility of each entry point.
        """
        # Validate stores
        if not self.stores:
            raise ValueError("Configuration must define at least one store")

        for store in self.stores:
            store.validate()

        # Validate crawler policy
        self.crawler.validate()

        # Validate proxy config
        self.proxy.validate()

    def build_provider_config(self) -> "ProviderConfig":
        from estimator_king.llm.config import ProviderConfig
        emb_key = self.embedding_api_key or self.openai_api_key or ""
        chat_key = self.chat_api_key or self.openai_api_key or ""
        return ProviderConfig(
            embedding_api_key=emb_key,
            chat_api_key=chat_key,
            embedding_base_url=self.embedding_base_url or self.openai_base_url,
            embedding_model=self.embedding_model,
            embedding_dimensions=self.embedding_dimensions,
            embedding_max_tokens=self.embedding_max_tokens,
            embedding_query_prefix=self.embedding_query_prefix,
            embedding_doc_prefix=self.embedding_doc_prefix,
            chat_base_url=self.chat_base_url or self.openai_base_url,
            chat_model=self.chat_model,
            chat_structured_output=self.chat_structured_output,
        )

    @staticmethod
    def from_yaml(path: str) -> "AppConfig":
        """Load configuration from YAML file.

        Args:
            path: Path to YAML config file

        Returns:
            AppConfig: Loaded and validated configuration

        Raises:
            FileNotFoundError: If config file doesn't exist
            ValueError: If configuration is invalid
        """
        return load_config(path)


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load configuration from YAML file and environment variables.

    Reads structural settings from YAML and credential/path settings from
    environment variables. Does NOT validate credentials — each entry point
    is responsible for validating the fields it requires.

    Args:
        config_path: Path to YAML config file. If None, uses CONFIG_PATH env var
                    or defaults to './stores_config.yaml'

    Returns:
        AppConfig: Loaded configuration (structurally validated)

    Raises:
        FileNotFoundError: If config file doesn't exist
        ValueError: If structural configuration is invalid
    """
    if config_path is None:
        config_path = os.getenv("CONFIG_PATH", "./stores_config.yaml")

    # Load YAML config
    with open(config_path, "r") as f:
        yaml_data = yaml.safe_load(f) or {}

    # Parse crawler policy
    crawler_data = yaml_data.get("crawler", {})
    crawler = CrawlerPolicy(
        rate_limit_rps=crawler_data.get("rate_limit_rps", 1.5),
        jitter_max=crawler_data.get("jitter_max", 0.5),
        concurrency_per_domain=crawler_data.get("concurrency_per_domain", 3),
        timeout_connect=crawler_data.get("timeout_connect", 10),
        timeout_read=crawler_data.get("timeout_read", 30),
        max_retries=crawler_data.get("max_retries", 3),
        max_products_per_run=crawler_data.get("max_products_per_run", 32),
        crawl_schedule_hours=crawler_data.get("crawl_schedule_hours", 24.0),
        inactive_failure_threshold=crawler_data.get("inactive_failure_threshold", 3),
        inactive_sitemap_miss_threshold=crawler_data.get("inactive_sitemap_miss_threshold", 4),
    )

    # Parse stores
    stores_data = yaml_data.get("stores", [])
    stores = [
        Store(
            id=s["id"],
            base_url=s["base_url"],
            sitemap_url=s["sitemap_url"],
            locale=s.get("locale", "default"),
        )
        for s in stores_data
    ]

    # Parse proxy config
    proxy_data = yaml_data.get("proxy", {})
    proxy = ProxyConfig(
        enabled=proxy_data.get("enabled", False),
        http_proxy=os.getenv("HTTP_PROXY", proxy_data.get("http_proxy", "")),
        https_proxy=os.getenv("HTTPS_PROXY", proxy_data.get("https_proxy", "")),
    )

    def _opt_int(name: str, default: int | None) -> int | None:
        raw = os.getenv(name)
        if raw is None:
            return default
        return int(raw) if raw.strip() != "" else None

    openai_api_key = os.getenv("OPENAI_API_KEY")
    openai_base_url = os.getenv("OPENAI_BASE_URL")
    config = AppConfig(
        stores=stores, crawler=crawler, proxy=proxy,
        openai_api_key=openai_api_key,
        openai_base_url=openai_base_url,
        embedding_api_key=os.getenv("EMBEDDING_API_KEY"),
        embedding_base_url=os.getenv("EMBEDDING_BASE_URL"),
        embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-large"),
        embedding_dimensions=_opt_int("EMBEDDING_DIMENSIONS", 1024),
        embedding_max_tokens=_opt_int("EMBEDDING_MAX_TOKENS", 8192) or 8192,
        embedding_query_prefix=os.getenv("EMBEDDING_QUERY_PREFIX", ""),
        embedding_doc_prefix=os.getenv("EMBEDDING_DOC_PREFIX", ""),
        chat_api_key=os.getenv("CHAT_API_KEY"),
        chat_base_url=os.getenv("CHAT_BASE_URL"),
        chat_model=os.getenv("CHAT_MODEL", "gpt-4o"),
        chat_structured_output=os.getenv("CHAT_STRUCTURED_OUTPUT", "true").lower() != "false",
        chroma_path=os.getenv("CHROMA_PATH", "./chroma"),
        discord_token=os.getenv("DISCORD_TOKEN", os.getenv("DISCORD_BOT_TOKEN")),
        database_path=os.getenv("DATABASE_PATH", "./estimator_king.db"),
    )

    # Validate structural configuration (not credentials)
    config.validate()

    return config
