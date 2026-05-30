"""Tests for configuration loading and validation."""

import os
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch

from estimator_king.config_schema import CrawlerPolicy, Store, load_config


def test_crawler_policy_budget_defaults():
    p = CrawlerPolicy()
    assert p.max_products_per_run == 32
    assert p.crawl_schedule_hours == 24.0


def test_store_has_no_fetch_interval():
    s = Store(id="a", base_url="b", sitemap_url="c")
    assert not hasattr(s, "fetch_interval_hours")


def _write_yaml(tmp_path):
    """Minimal valid stores config (config.validate() requires >=1 store)."""
    path = tmp_path / "stores.yaml"
    path.write_text(
        "stores:\n"
        "  - id: hololive\n"
        "    base_url: https://x\n"
        "    sitemap_url: https://x/sitemap.xml\n",
        encoding="utf-8",
    )
    return str(path)


@patch.dict(os.environ, {
    "OPENAI_API_KEY": "sk-x", "EMBEDDING_MODEL": "bge-m3",
    "EMBEDDING_DIMENSIONS": "", "CHAT_MODEL": "gpt-4o", "CHROMA_PATH": "/data/chroma",
}, clear=False)
def test_build_provider_config_from_env(tmp_path):
    cfg = load_config(_write_yaml(tmp_path))  # exercises the env → AppConfig path

    pc = cfg.build_provider_config()
    assert pc.embedding_api_key == "sk-x"
    assert pc.chat_api_key == "sk-x"          # falls back to OPENAI_API_KEY
    assert pc.embedding_model == "bge-m3"
    assert pc.embedding_dimensions is None    # EMBEDDING_DIMENSIONS="" → None via _opt_int
    assert pc.chat_model == "gpt-4o"
    assert cfg.chroma_path == "/data/chroma"  # chroma_path lives on AppConfig, not ProviderConfig


class TestStore:
    """Test Store configuration."""

    def test_store_creation(self):
        """Test creating a valid store."""
        store = Store(
            id="hololive",
            base_url="https://shop.hololivepro.com",
            sitemap_url="https://shop.hololivepro.com/sitemap.xml",
        )
        assert store.id == "hololive"
        assert store.base_url == "https://shop.hololivepro.com"
        assert store.sitemap_url == "https://shop.hololivepro.com/sitemap.xml"

    def test_store_validation_invalid_id(self):
        """Test store validation with invalid ID."""
        store = Store(
            id="",
            base_url="https://example.com",
            sitemap_url="https://example.com/sitemap.xml",
        )
        with pytest.raises(ValueError, match="Store 'id' must be a non-empty string"):
            store.validate()

    def test_store_validation_invalid_base_url(self):
        """Test store validation with invalid base_url."""
        store = Store(
            id="test", base_url="", sitemap_url="https://example.com/sitemap.xml"
        )
        with pytest.raises(ValueError, match="must have a valid 'base_url'"):
            store.validate()

    def test_store_validation_invalid_sitemap_url(self):
        """Test store validation with invalid sitemap_url."""
        store = Store(id="test", base_url="https://example.com", sitemap_url="")
        with pytest.raises(ValueError, match="must have a valid 'sitemap_url'"):
            store.validate()


class TestCrawlerPolicy:
    """Test CrawlerPolicy configuration."""

    def test_crawler_policy_defaults(self):
        """Test crawler policy default values."""
        policy = CrawlerPolicy()
        assert policy.rate_limit_rps == 1.5
        assert policy.jitter_max == 0.5
        assert policy.concurrency_per_domain == 3
        assert policy.timeout_connect == 10
        assert policy.timeout_read == 30
        assert policy.max_retries == 3
        assert policy.max_products_per_run == 32
        assert policy.crawl_schedule_hours == 24.0
        assert policy.inactive_failure_threshold == 3
        assert policy.inactive_sitemap_miss_threshold == 4

    def test_crawler_policy_custom_values(self):
        """Test crawler policy with custom values."""
        policy = CrawlerPolicy(
            rate_limit_rps=2.0,
            jitter_max=0.2,
            concurrency_per_domain=5,
            timeout_connect=15,
            timeout_read=60,
            max_retries=5,
            max_products_per_run=64,
            crawl_schedule_hours=48.0,
            inactive_failure_threshold=5,
            inactive_sitemap_miss_threshold=6,
        )
        assert policy.rate_limit_rps == 2.0
        assert policy.jitter_max == 0.2
        assert policy.concurrency_per_domain == 5
        assert policy.timeout_connect == 15
        assert policy.timeout_read == 60
        assert policy.max_retries == 5
        assert policy.max_products_per_run == 64
        assert policy.crawl_schedule_hours == 48.0
        assert policy.inactive_failure_threshold == 5
        assert policy.inactive_sitemap_miss_threshold == 6

    def test_crawler_policy_validation_invalid_rate_limit(self):
        """Test crawler policy validation with invalid rate_limit_rps."""
        policy = CrawlerPolicy(rate_limit_rps=0)
        with pytest.raises(ValueError, match="'rate_limit_rps' must be greater than 0"):
            policy.validate()

    def test_crawler_policy_validation_invalid_concurrency(self):
        """Test crawler policy validation with invalid concurrency."""
        policy = CrawlerPolicy(concurrency_per_domain=0)
        with pytest.raises(
            ValueError, match="'concurrency_per_domain' must be greater than 0"
        ):
            policy.validate()

    def test_crawler_policy_validation_negative_jitter(self):
        """Test crawler policy validation with negative jitter."""
        policy = CrawlerPolicy(jitter_max=-0.1)
        with pytest.raises(ValueError, match="'jitter_max' must be non-negative"):
            policy.validate()

    def test_crawler_policy_validation_invalid_max_products(self):
        """Test crawler policy validation with invalid max_products_per_run."""
        policy = CrawlerPolicy(max_products_per_run=0)
        with pytest.raises(ValueError, match="'max_products_per_run' must be greater than 0"):
            policy.validate()

    def test_crawler_policy_validation_invalid_crawl_schedule(self):
        """Test crawler policy validation with invalid crawl_schedule_hours."""
        policy = CrawlerPolicy(crawl_schedule_hours=0)
        with pytest.raises(ValueError, match="'crawl_schedule_hours' must be greater than 0"):
            policy.validate()

    def test_crawler_policy_validation_invalid_inactive_failure_threshold(self):
        """Test crawler policy validation with invalid inactive_failure_threshold."""
        policy = CrawlerPolicy(inactive_failure_threshold=0)
        with pytest.raises(ValueError, match="'inactive_failure_threshold' must be greater than 0"):
            policy.validate()

    def test_crawler_policy_validation_invalid_inactive_sitemap_miss_threshold(self):
        """Test crawler policy validation with invalid inactive_sitemap_miss_threshold."""
        policy = CrawlerPolicy(inactive_sitemap_miss_threshold=0)
        with pytest.raises(ValueError, match="'inactive_sitemap_miss_threshold' must be greater than 0"):
            policy.validate()


class TestProxyConfig:
    """Test ProxyConfig configuration."""

    def test_proxy_config_disabled_by_default(self):
        """Test proxy config is disabled by default."""
        from estimator_king.config_schema import ProxyConfig
        proxy = ProxyConfig()
        assert proxy.enabled is False
        assert proxy.http_proxy == ""
        assert proxy.https_proxy == ""

    def test_proxy_config_validation_enabled_without_proxy(self):
        """Test proxy validation when enabled but no proxy set."""
        from estimator_king.config_schema import ProxyConfig
        proxy = ProxyConfig(enabled=True, http_proxy="", https_proxy="")
        with pytest.raises(ValueError, match="Proxy is enabled but"):
            proxy.validate()

    def test_proxy_config_validation_enabled_with_http_proxy(self):
        """Test proxy validation when enabled with http_proxy."""
        from estimator_king.config_schema import ProxyConfig
        proxy = ProxyConfig(enabled=True, http_proxy="http://proxy.example.com:8080")
        proxy.validate()  # Should not raise

    def test_proxy_config_validation_enabled_with_https_proxy(self):
        """Test proxy validation when enabled with https_proxy."""
        from estimator_king.config_schema import ProxyConfig
        proxy = ProxyConfig(enabled=True, https_proxy="https://proxy.example.com:8080")
        proxy.validate()  # Should not raise


class TestAppConfig:
    """Test AppConfig configuration."""

    def test_app_config_validation_missing_stores(self):
        """Test app config validation with no stores."""
        from estimator_king.config_schema import AppConfig
        config = AppConfig(
            stores=[],
        )
        with pytest.raises(ValueError, match="must define at least one store"):
            config.validate()

    def test_app_config_valid(self):
        """Test valid app config."""
        from estimator_king.config_schema import AppConfig
        config = AppConfig(
            stores=[
                Store(
                    id="test",
                    base_url="https://example.com",
                    sitemap_url="https://example.com/sitemap.xml",
                ),
            ],
        )
        config.validate()  # Should not raise

    def test_app_config_credentials_not_validated(self):
        """Test that validate() does NOT check credentials (entry points do that)."""
        from estimator_king.config_schema import AppConfig
        config = AppConfig(
            stores=[
                Store(
                    id="test",
                    base_url="https://example.com",
                    sitemap_url="https://example.com/sitemap.xml",
                ),
            ],
            openai_api_key=None,
            discord_token=None,
        )
        config.validate()  # Should not raise even without credentials


class TestLoadConfig:
    """Test load_config function."""

    def test_config_load_default_values(self, monkeypatch):
        """Test loading config with default crawler values."""
        yaml_content = """
stores:
  - id: hololive
    base_url: https://shop.hololivepro.com
    sitemap_url: https://shop.hololivepro.com/sitemap.xml
  - id: vspo
    base_url: https://store.vspo.jp
    sitemap_url: https://store.vspo.jp/sitemap.xml

crawler:
  rate_limit_rps: 1.5
  jitter_max: 0.5
  concurrency_per_domain: 3
  timeout_connect: 10
  timeout_read: 30
  max_retries: 3

proxy:
  enabled: false
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            config_path = f.name

        try:
            monkeypatch.delenv("DISCORD_TOKEN", raising=False)
            monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
            monkeypatch.delenv("CONFIG_PATH", raising=False)
            monkeypatch.delenv("HTTP_PROXY", raising=False)
            monkeypatch.delenv("HTTPS_PROXY", raising=False)
            monkeypatch.delenv("DATABASE_PATH", raising=False)
            monkeypatch.delenv("OPENAI_API_KEY", raising=False)
            monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
            monkeypatch.delenv("EMBEDDING_DIMENSIONS", raising=False)
            monkeypatch.delenv("CHAT_MODEL", raising=False)
            monkeypatch.delenv("CHROMA_PATH", raising=False)

            config = load_config(config_path)

            assert len(config.stores) == 2
            assert config.stores[0].id == "hololive"
            assert config.stores[0].base_url == "https://shop.hololivepro.com"
            assert config.stores[1].id == "vspo"
            assert config.stores[1].base_url == "https://store.vspo.jp"

            assert config.crawler.rate_limit_rps == 1.5
            assert config.crawler.jitter_max == 0.5
            assert config.crawler.concurrency_per_domain == 3
            assert config.crawler.timeout_connect == 10
            assert config.crawler.timeout_read == 30
            assert config.crawler.max_retries == 3
            assert config.crawler.max_products_per_run == 32
            assert config.crawler.crawl_schedule_hours == 24.0
            assert config.crawler.inactive_failure_threshold == 3
            assert config.crawler.inactive_sitemap_miss_threshold == 4

            assert config.proxy.enabled is False
            assert config.discord_token is None
            assert config.database_path == "./estimator_king.db"
        finally:
            os.unlink(config_path)

    def test_config_load_with_env_overrides(self, monkeypatch):
        """Test loading config with environment variable overrides."""
        yaml_content = """
stores:
  - id: hololive
    base_url: https://shop.hololivepro.com
    sitemap_url: https://shop.hololivepro.com/sitemap.xml

crawler:
  rate_limit_rps: 1.5

proxy:
  enabled: false
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            config_path = f.name

        try:
            monkeypatch.setenv("DISCORD_TOKEN", "env-discord-token")
            monkeypatch.setenv("DATABASE_PATH", "/custom/path.db")
            monkeypatch.setenv("HTTP_PROXY", "http://custom.proxy:3128")
            monkeypatch.delenv("CONFIG_PATH", raising=False)
            monkeypatch.delenv("HTTPS_PROXY", raising=False)

            config = load_config(config_path)

            assert config.discord_token == "env-discord-token"
            assert config.database_path == "/custom/path.db"
            assert config.proxy.http_proxy == "http://custom.proxy:3128"
        finally:
            os.unlink(config_path)

    def test_config_load_missing_file(self, monkeypatch):
        """Test loading config from non-existent file."""
        monkeypatch.delenv("CONFIG_PATH", raising=False)
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.yaml")

    def test_config_load_invalid_config(self, monkeypatch):
        """Test loading config with invalid content."""
        yaml_content = """
stores: []
crawler:
  rate_limit_rps: 0
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            config_path = f.name

        try:
            monkeypatch.setenv("DISCORD_TOKEN", "test-token")
            monkeypatch.delenv("CONFIG_PATH", raising=False)

            with pytest.raises(ValueError):
                load_config(config_path)
        finally:
            os.unlink(config_path)

    def test_config_load_missing_required_env_vars(self):
        """Test loading config without credentials still succeeds (credentials are optional in load_config)."""
        yaml_content = """
stores:
  - id: test
    base_url: https://example.com
    sitemap_url: https://example.com/sitemap.xml
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            config_path = f.name

        try:
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("DISCORD_TOKEN", None)
            os.environ.pop("DISCORD_BOT_TOKEN", None)

            config = load_config(config_path)
            assert config.openai_api_key is None
            assert config.discord_token is None
        finally:
            os.unlink(config_path)

    def test_config_load_with_budget_fields(self, monkeypatch):
        """Test loading config with max_products_per_run and crawl_schedule_hours fields."""
        yaml_content = """
stores:
  - id: hololive
    base_url: https://shop.hololivepro.com
    sitemap_url: https://shop.hololivepro.com/sitemap.xml

crawler:
  rate_limit_rps: 1.5
  max_products_per_run: 64
  crawl_schedule_hours: 48.0
  inactive_failure_threshold: 5
  inactive_sitemap_miss_threshold: 6

proxy:
  enabled: false
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            config_path = f.name

        try:
            monkeypatch.delenv("CONFIG_PATH", raising=False)
            monkeypatch.delenv("HTTP_PROXY", raising=False)
            monkeypatch.delenv("HTTPS_PROXY", raising=False)

            config = load_config(config_path)

            assert config.crawler.max_products_per_run == 64
            assert config.crawler.crawl_schedule_hours == 48.0
            assert config.crawler.inactive_failure_threshold == 5
            assert config.crawler.inactive_sitemap_miss_threshold == 6
        finally:
            os.unlink(config_path)

    def test_config_load_defaults_when_budget_fields_absent(self, monkeypatch):
        """Test that budget fields default correctly when absent from YAML."""
        yaml_content = """
stores:
  - id: test
    base_url: https://example.com
    sitemap_url: https://example.com/sitemap.xml

crawler:
  rate_limit_rps: 1.5
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            config_path = f.name

        try:
            monkeypatch.delenv("CONFIG_PATH", raising=False)
            monkeypatch.delenv("HTTP_PROXY", raising=False)
            monkeypatch.delenv("HTTPS_PROXY", raising=False)

            config = load_config(config_path)

            assert config.crawler.max_products_per_run == 32
            assert config.crawler.crawl_schedule_hours == 24.0
            assert config.crawler.inactive_failure_threshold == 3
            assert config.crawler.inactive_sitemap_miss_threshold == 4
        finally:
            os.unlink(config_path)


class TestConfigIntegration:
    """Integration tests for config loading."""

    def test_stores_config_yaml_exists(self):
        """Test that stores_config.yaml exists in project root."""
        config_path = Path("stores_config.yaml")
        assert config_path.exists(), "stores_config.yaml not found in project root"

    def test_stores_config_yaml_valid(self, monkeypatch):
        """Test that stores_config.yaml is valid and loadable."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("DISCORD_TOKEN", raising=False)
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("CONFIG_PATH", raising=False)
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.delenv("DATABASE_PATH", raising=False)

        config = load_config("stores_config.yaml")

        assert len(config.stores) >= 2
        store_ids = {store.id for store in config.stores}
        assert "hololive" in store_ids
        assert "vspo" in store_ids

        assert config.crawler is not None
        assert config.crawler.rate_limit_rps > 0
        assert config.crawler.max_products_per_run == 32
        assert config.crawler.crawl_schedule_hours == 24.0
        assert config.crawler.inactive_failure_threshold == 3
        assert config.crawler.inactive_sitemap_miss_threshold == 4

        assert config.proxy is not None
