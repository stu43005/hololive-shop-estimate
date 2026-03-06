"""Configuration schema and validation for Estimator King."""

from dataclasses import dataclass, field
from typing import Optional, List
import yaml
import os


@dataclass
class Store:
    """Store configuration."""

    id: str
    base_url: str
    sitemap_url: str

    def validate(self):
        """Validate store configuration."""
        if not self.id or not isinstance(self.id, str):
            raise ValueError("Store 'id' must be a non-empty string")
        if not self.base_url or not isinstance(self.base_url, str):
            raise ValueError(f"Store '{self.id}' must have a valid 'base_url'")
        if not self.sitemap_url or not isinstance(self.sitemap_url, str):
            raise ValueError(f"Store '{self.id}' must have a valid 'sitemap_url'")


@dataclass
class CrawlerPolicy:
    """Crawler policy configuration."""

    rate_limit_rps: float = 1.5
    jitter_max: float = 0.5
    concurrency_per_domain: int = 3
    timeout_connect: int = 10
    timeout_read: int = 30
    max_retries: int = 3

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
    (stores, crawler, proxy) and environment-based credentials (Dify, Discord).

    Each entry point (crawler, bot) is responsible for validating only the
    fields it actually requires.
    """

    stores: List[Store] = field(default_factory=list)
    crawler: CrawlerPolicy = field(default_factory=CrawlerPolicy)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)

    # Dify Knowledge Base (crawler)
    dify_api_key: Optional[str] = None
    dify_base_url: Optional[str] = None
    dify_dataset_id: Optional[str] = None

    # Dify Workflow (bot)
    dify_workflow_api_key: Optional[str] = None
    dify_workflow_base_url: Optional[str] = None

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

    # Parse stores
    stores_data = yaml_data.get("stores", [])
    stores = [
        Store(
            id=s["id"],
            base_url=s["base_url"],
            sitemap_url=s["sitemap_url"],
        )
        for s in stores_data
    ]

    # Parse crawler policy
    crawler_data = yaml_data.get("crawler", {})
    crawler = CrawlerPolicy(
        rate_limit_rps=crawler_data.get("rate_limit_rps", 1.5),
        jitter_max=crawler_data.get("jitter_max", 0.5),
        concurrency_per_domain=crawler_data.get("concurrency_per_domain", 3),
        timeout_connect=crawler_data.get("timeout_connect", 10),
        timeout_read=crawler_data.get("timeout_read", 30),
        max_retries=crawler_data.get("max_retries", 3),
    )

    # Parse proxy config
    proxy_data = yaml_data.get("proxy", {})
    proxy = ProxyConfig(
        enabled=proxy_data.get("enabled", False),
        http_proxy=os.getenv("HTTP_PROXY", proxy_data.get("http_proxy", "")),
        https_proxy=os.getenv("HTTPS_PROXY", proxy_data.get("https_proxy", "")),
    )

    # Load environment variables
    dify_api_key = os.getenv("DIFY_API_KEY")
    dify_base_url = os.getenv("DIFY_BASE_URL")
    dify_dataset_id = os.getenv("DIFY_DATASET_ID")
    dify_workflow_api_key = os.getenv("DIFY_WORKFLOW_API_KEY")
    dify_workflow_base_url = os.getenv("DIFY_WORKFLOW_BASE_URL")
    discord_token = os.getenv("DISCORD_TOKEN", os.getenv("DISCORD_BOT_TOKEN"))
    database_path = os.getenv("DATABASE_PATH", "./estimator_king.db")

    # Create config object
    config = AppConfig(
        stores=stores,
        crawler=crawler,
        proxy=proxy,
        dify_api_key=dify_api_key,
        dify_base_url=dify_base_url,
        dify_dataset_id=dify_dataset_id,
        dify_workflow_api_key=dify_workflow_api_key,
        dify_workflow_base_url=dify_workflow_base_url,
        discord_token=discord_token,
        database_path=database_path,
    )

    # Validate structural configuration (not credentials)
    config.validate()

    return config
