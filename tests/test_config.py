"""Tests for configuration loading and validation."""

import os
import tempfile
import pytest
from pathlib import Path

from estimator_king.config_schema import (
    AppConfig,
    Store,
    CrawlerPolicy,
    ProxyConfig,
    load_config,
)


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

    def test_crawler_policy_custom_values(self):
        """Test crawler policy with custom values."""
        policy = CrawlerPolicy(
            rate_limit_rps=2.0,
            jitter_max=0.2,
            concurrency_per_domain=5,
            timeout_connect=15,
            timeout_read=60,
            max_retries=5,
        )
        assert policy.rate_limit_rps == 2.0
        assert policy.jitter_max == 0.2
        assert policy.concurrency_per_domain == 5
        assert policy.timeout_connect == 15
        assert policy.timeout_read == 60
        assert policy.max_retries == 5

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


class TestProxyConfig:
    """Test ProxyConfig configuration."""

    def test_proxy_config_disabled_by_default(self):
        """Test proxy config is disabled by default."""
        proxy = ProxyConfig()
        assert proxy.enabled is False
        assert proxy.http_proxy == ""
        assert proxy.https_proxy == ""

    def test_proxy_config_validation_enabled_without_proxy(self):
        """Test proxy validation when enabled but no proxy set."""
        proxy = ProxyConfig(enabled=True, http_proxy="", https_proxy="")
        with pytest.raises(ValueError, match="Proxy is enabled but"):
            proxy.validate()

    def test_proxy_config_validation_enabled_with_http_proxy(self):
        """Test proxy validation when enabled with http_proxy."""
        proxy = ProxyConfig(enabled=True, http_proxy="http://proxy.example.com:8080")
        proxy.validate()  # Should not raise

    def test_proxy_config_validation_enabled_with_https_proxy(self):
        """Test proxy validation when enabled with https_proxy."""
        proxy = ProxyConfig(enabled=True, https_proxy="https://proxy.example.com:8080")
        proxy.validate()  # Should not raise


class TestAppConfig:
    """Test AppConfig configuration."""

    def test_app_config_validation_missing_stores(self):
        """Test app config validation with no stores."""
        config = AppConfig(
            stores=[],
        )
        with pytest.raises(ValueError, match="must define at least one store"):
            config.validate()

    def test_app_config_valid(self):
        """Test valid app config."""
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
        config = AppConfig(
            stores=[
                Store(
                    id="test",
                    base_url="https://example.com",
                    sitemap_url="https://example.com/sitemap.xml",
                ),
            ],
            dify_api_key=None,
            discord_token=None,
        )
        config.validate()  # Should not raise even without credentials

class TestLoadConfig:
    """Test load_config function."""

    def test_config_load_default_values(self, monkeypatch):
        """Test loading config with default crawler values."""
        # Create temporary YAML config
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
            # Set environment variables
            monkeypatch.setenv("DIFY_API_KEY", "test-dify-key")
            monkeypatch.delenv("DISCORD_TOKEN", raising=False)
            monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
            monkeypatch.delenv("CONFIG_PATH", raising=False)
            monkeypatch.delenv("HTTP_PROXY", raising=False)
            monkeypatch.delenv("HTTPS_PROXY", raising=False)
            monkeypatch.delenv("DATABASE_PATH", raising=False)
            monkeypatch.delenv("DIFY_BASE_URL", raising=False)
            monkeypatch.delenv("DIFY_DATASET_ID", raising=False)
            monkeypatch.delenv("DIFY_WORKFLOW_API_KEY", raising=False)
            monkeypatch.delenv("DIFY_WORKFLOW_BASE_URL", raising=False)

            # Load config
            config = load_config(config_path)

            # Verify stores
            assert len(config.stores) == 2
            assert config.stores[0].id == "hololive"
            assert config.stores[0].base_url == "https://shop.hololivepro.com"
            assert config.stores[1].id == "vspo"
            assert config.stores[1].base_url == "https://store.vspo.jp"

            # Verify crawler defaults
            assert config.crawler.rate_limit_rps == 1.5
            assert config.crawler.jitter_max == 0.5
            assert config.crawler.concurrency_per_domain == 3
            assert config.crawler.timeout_connect == 10
            assert config.crawler.timeout_read == 30
            assert config.crawler.max_retries == 3

            # Verify proxy
            assert config.proxy.enabled is False

            # Verify env vars
            assert config.dify_api_key == "test-dify-key"
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
            # Set environment variables
            monkeypatch.setenv("DIFY_API_KEY", "env-dify-key")
            monkeypatch.setenv("DISCORD_TOKEN", "env-discord-token")
            monkeypatch.setenv("DATABASE_PATH", "/custom/path.db")
            monkeypatch.setenv("HTTP_PROXY", "http://custom.proxy:3128")
            monkeypatch.delenv("CONFIG_PATH", raising=False)
            monkeypatch.delenv("HTTPS_PROXY", raising=False)

            # Load config
            config = load_config(config_path)

            # Verify env var overrides
            assert config.dify_api_key == "env-dify-key"
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
            monkeypatch.setenv("DIFY_API_KEY", "test-key")
            monkeypatch.setenv("DISCORD_TOKEN", "test-token")
            monkeypatch.delenv("CONFIG_PATH", raising=False)

            # Should raise ValueError for invalid config
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
            # Remove environment variables
            os.environ.pop("DIFY_API_KEY", None)
            os.environ.pop("DISCORD_TOKEN", None)
            os.environ.pop("DISCORD_BOT_TOKEN", None)

            # load_config no longer validates credentials — this should succeed
            config = load_config(config_path)
            assert config.dify_api_key is None
            assert config.discord_token is None
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
        # Clean env to avoid interference
        monkeypatch.delenv("DIFY_API_KEY", raising=False)
        monkeypatch.delenv("DISCORD_TOKEN", raising=False)
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("CONFIG_PATH", raising=False)
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.delenv("DATABASE_PATH", raising=False)

        # Load the actual config file
        config = load_config("stores_config.yaml")

        # Verify it has the expected stores
        assert len(config.stores) >= 2
        store_ids = {store.id for store in config.stores}
        assert "hololive" in store_ids
        assert "vspo" in store_ids

        # Verify crawler policy exists
        assert config.crawler is not None
        assert config.crawler.rate_limit_rps > 0

        # Verify proxy config exists
        assert config.proxy is not None
