"""Unit tests for CLI argument parser and orchestration."""

import os
import subprocess
import sys
import pytest
from unittest.mock import MagicMock, patch

from estimator_king.__main__ import run_crawler
from estimator_king.config_schema import AppConfig, Store, CrawlerPolicy, ProxyConfig
from estimator_king.sync.engine import SyncResult
from estimator_king.sync.inactive import InactiveResult


def test_help_flag():
    """Test --help flag produces help text."""
    result = subprocess.run(
        [sys.executable, "-m", "estimator_king", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Estimator King" in result.stdout
    assert "--config" in result.stdout
    assert "--db" in result.stdout
    assert "--dify-base-url" in result.stdout


def test_missing_dify_api_key(monkeypatch):
    """Test missing DIFY_API_KEY raises error."""
    monkeypatch.delenv("DIFY_API_KEY", raising=False)
    monkeypatch.delenv("DIFY_BASE_URL", raising=False)
    monkeypatch.delenv("DIFY_DATASET_ID", raising=False)

    result = subprocess.run(
        [sys.executable, "-m", "estimator_king"], capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "required" in result.stderr.lower()


def test_missing_dify_base_url(monkeypatch):
    """Test missing DIFY_BASE_URL raises error."""
    monkeypatch.delenv("DIFY_BASE_URL", raising=False)
    monkeypatch.setenv("DIFY_API_KEY", "dataset-test123")
    monkeypatch.setenv("DIFY_DATASET_ID", "uuid-test456")

    result = subprocess.run(
        [sys.executable, "-m", "estimator_king"],
        capture_output=True,
        text=True,
        env=os.environ,
    )
    assert result.returncode == 2
    assert "required" in result.stderr.lower()


def test_config_argument():
    """Test --config argument works."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "estimator_king",
            "--config",
            "/custom/stores.yaml",
            "--dify-api-key",
            "dataset-test",
            "--dify-base-url",
            "https://test.com",
            "--dify-dataset-id",
            "test-uuid",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_db_argument():
    """Test --db argument works."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "estimator_king",
            "--db",
            "/custom/db.sqlite",
            "--dify-api-key",
            "dataset-test",
            "--dify-base-url",
            "https://test.com",
            "--dify-dataset-id",
            "test-uuid",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_all_arguments():
    """Test all arguments provided via CLI."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "estimator_king",
            "--config",
            "/stores.yaml",
            "--db",
            "/data/db.sqlite",
            "--dify-api-key",
            "dataset-abc123",
            "--dify-base-url",
            "https://dify.example.com",
            "--dify-dataset-id",
            "uuid-xyz789",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_env_vars_dify_credentials(monkeypatch):
    """Test Dify credentials via environment variables."""
    monkeypatch.setenv("DIFY_API_KEY", "dataset-env123")
    monkeypatch.setenv("DIFY_BASE_URL", "https://env.example.com")
    monkeypatch.setenv("DIFY_DATASET_ID", "env-uuid-456")

    result = subprocess.run(
        [sys.executable, "-m", "estimator_king"],
        capture_output=True,
        text=True,
        env=os.environ,
    )
    assert result.returncode == 0


def test_database_path_env_var(monkeypatch):
    """Test DATABASE_PATH environment variable works."""
    monkeypatch.setenv("DATABASE_PATH", "/env/path/db.sqlite")
    monkeypatch.setenv("DIFY_API_KEY", "dataset-test")
    monkeypatch.setenv("DIFY_BASE_URL", "https://test.com")
    monkeypatch.setenv("DIFY_DATASET_ID", "test-uuid")

    result = subprocess.run(
        [sys.executable, "-m", "estimator_king"],
        capture_output=True,
        text=True,
        env=os.environ,
    )
    assert result.returncode == 0


@patch("estimator_king.__main__.mark_inactive_products")
@patch("estimator_king.__main__.sync_products")
@patch("estimator_king.__main__.fetch_product")
@patch("estimator_king.__main__.SitemapEnumerator")
@patch("estimator_king.__main__.ProductStateRepository")
def test_run_crawler_success(
    mock_repo_class, mock_enumerate_class, mock_fetch, mock_sync, mock_inactive
):
    """Test run_crawler with successful operations across all stores."""
    mock_repo = MagicMock()
    mock_repo.__enter__ = MagicMock(return_value=mock_repo)
    mock_repo.__exit__ = MagicMock(return_value=None)
    mock_repo_class.return_value = mock_repo

    mock_enumerator = MagicMock()
    mock_enumerator.enumerate_products.return_value = [
        "https://store.com/products/1",
        "https://store.com/products/2",
    ]
    mock_enumerate_class.return_value = mock_enumerator

    mock_fetch.return_value = MagicMock()

    mock_sync.return_value = SyncResult(
        created=1, updated=1, skipped=0, failed=0, failed_ids=[]
    )
    mock_inactive.return_value = InactiveResult(marked_inactive=0, already_inactive=0)

    config = AppConfig(
        stores=[
            Store(
                id="test",
                base_url="https://test.com",
                sitemap_url="https://test.com/sitemap.xml",
            )
        ],
        crawler=CrawlerPolicy(),
        proxy=ProxyConfig(),
    )
    dify_client = MagicMock()

    result = run_crawler(config, ":memory:", dify_client)

    assert result["discovered"] == 2
    assert result["fetched_ok"] == 2
    assert result["created"] == 1
    assert result["updated"] == 1
    assert result["skipped"] == 0
    assert result["errors"] == 0
    assert result["inactive"] == 0


@patch("estimator_king.__main__.mark_inactive_products")
@patch("estimator_king.__main__.sync_products")
@patch("estimator_king.__main__.fetch_product")
@patch("estimator_king.__main__.SitemapEnumerator")
@patch("estimator_king.__main__.ProductStateRepository")
def test_run_crawler_empty_sitemap(
    mock_repo_class, mock_enumerate_class, mock_fetch, mock_sync, mock_inactive
):
    """Test run_crawler with empty sitemap yields zero errors."""
    mock_repo = MagicMock()
    mock_repo.__enter__ = MagicMock(return_value=mock_repo)
    mock_repo.__exit__ = MagicMock(return_value=None)
    mock_repo_class.return_value = mock_repo

    mock_enumerator = MagicMock()
    mock_enumerate_class.return_value = mock_enumerator

    mock_enumerator.enumerate_products.return_value = []
    mock_sync.return_value = SyncResult(
        created=0, updated=0, skipped=0, failed=0, failed_ids=[]
    )
    mock_inactive.return_value = InactiveResult(marked_inactive=0, already_inactive=0)

    config = AppConfig(
        stores=[
            Store(
                id="test",
                base_url="https://test.com",
                sitemap_url="https://test.com/sitemap.xml",
            )
        ],
        crawler=CrawlerPolicy(),
        proxy=ProxyConfig(),
    )
    dify_client = MagicMock()

    result = run_crawler(config, ":memory:", dify_client)

    assert result["discovered"] == 0
    assert result["fetched_ok"] == 0
    assert result["errors"] == 0


@patch("estimator_king.__main__.mark_inactive_products")
@patch("estimator_king.__main__.sync_products")
@patch("estimator_king.__main__.fetch_product")
@patch("estimator_king.__main__.SitemapEnumerator")
@patch("estimator_king.__main__.ProductStateRepository")
def test_run_crawler_fetch_failure(
    mock_repo_class, mock_enumerate_class, mock_fetch, mock_sync, mock_inactive
):
    """Test run_crawler continues after fetch failure."""
    mock_repo = MagicMock()
    mock_repo.__enter__ = MagicMock(return_value=mock_repo)
    mock_repo.__exit__ = MagicMock(return_value=None)
    mock_repo_class.return_value = mock_repo

    mock_enumerator = MagicMock()
    mock_enumerate_class.return_value = mock_enumerator

    mock_enumerator.enumerate_products.return_value = [
        "https://store.com/products/1",
        "https://store.com/products/2",
    ]
    mock_fetch.side_effect = [
        MagicMock(),
        Exception("Network error"),
    ]

    mock_sync.return_value = SyncResult(
        created=1, updated=0, skipped=0, failed=0, failed_ids=[]
    )
    mock_inactive.return_value = InactiveResult(marked_inactive=0, already_inactive=0)

    config = AppConfig(
        stores=[
            Store(
                id="test",
                base_url="https://test.com",
                sitemap_url="https://test.com/sitemap.xml",
            )
        ],
        crawler=CrawlerPolicy(),
        proxy=ProxyConfig(),
    )
    dify_client = MagicMock()

    result = run_crawler(config, ":memory:", dify_client)

    assert result["discovered"] == 2
    assert result["fetched_ok"] == 1
    assert result["errors"] == 1


@patch("estimator_king.__main__.mark_inactive_products")
@patch("estimator_king.__main__.sync_products")
@patch("estimator_king.__main__.fetch_product")
@patch("estimator_king.__main__.SitemapEnumerator")
@patch("estimator_king.__main__.ProductStateRepository")
def test_run_crawler_multiple_stores(
    mock_repo_class, mock_enumerate_class, mock_fetch, mock_sync, mock_inactive
):
    """Test run_crawler aggregates counters across multiple stores."""
    mock_repo = MagicMock()
    mock_repo.__enter__ = MagicMock(return_value=mock_repo)
    mock_repo.__exit__ = MagicMock(return_value=None)
    mock_repo_class.return_value = mock_repo

    mock_enumerator = MagicMock()
    mock_enumerate_class.return_value = mock_enumerator

    mock_enumerator.enumerate_products.side_effect = [
        ["https://store1.com/products/1"],
        ["https://store2.com/products/1", "https://store2.com/products/2"],
    ]
    mock_fetch.return_value = MagicMock()

    mock_sync.side_effect = [
        SyncResult(created=1, updated=0, skipped=0, failed=0, failed_ids=[]),
        SyncResult(created=2, updated=0, skipped=0, failed=0, failed_ids=[]),
    ]
    mock_inactive.return_value = InactiveResult(marked_inactive=0, already_inactive=0)

    config = AppConfig(
        stores=[
            Store(
                id="store1",
                base_url="https://store1.com",
                sitemap_url="https://store1.com/sitemap.xml",
            ),
            Store(
                id="store2",
                base_url="https://store2.com",
                sitemap_url="https://store2.com/sitemap.xml",
            ),
        ],
        crawler=CrawlerPolicy(),
        proxy=ProxyConfig(),
    )
    dify_client = MagicMock()

    result = run_crawler(config, ":memory:", dify_client)

    assert result["discovered"] == 3
    assert result["fetched_ok"] == 3
    assert result["created"] == 3


@patch("estimator_king.__main__.mark_inactive_products")
@patch("estimator_king.__main__.sync_products")
@patch("estimator_king.__main__.fetch_product")
@patch("estimator_king.__main__.SitemapEnumerator")
@patch("estimator_king.__main__.ProductStateRepository")
def test_run_crawler_sync_failure(
    mock_repo_class, mock_enumerate_class, mock_fetch, mock_sync, mock_inactive
):
    """Test run_crawler handles sync failures gracefully."""
    mock_repo = MagicMock()
    mock_repo.__enter__ = MagicMock(return_value=mock_repo)
    mock_repo.__exit__ = MagicMock(return_value=None)
    mock_repo_class.return_value = mock_repo

    mock_enumerator = MagicMock()
    mock_enumerate_class.return_value = mock_enumerator

    mock_enumerator.enumerate_products.return_value = ["https://store.com/products/1"]
    mock_fetch.return_value = MagicMock()

    mock_sync.side_effect = Exception("Dify API error")

    config = AppConfig(
        stores=[
            Store(
                id="test",
                base_url="https://test.com",
                sitemap_url="https://test.com/sitemap.xml",
            )
        ],
        crawler=CrawlerPolicy(),
        proxy=ProxyConfig(),
    )
    dify_client = MagicMock()

    result = run_crawler(config, ":memory:", dify_client)

    assert result["discovered"] == 1
    assert result["fetched_ok"] == 1
    assert result["errors"] == 1


class TestMainSuccess:
    """Test successful main() execution."""

    @patch("estimator_king.__main__.run_crawler")
    @patch("estimator_king.__main__.DifyKBClient")
    @patch("estimator_king.__main__.AppConfig.from_yaml")
    @patch("estimator_king.__main__.parse_args")
    def test_main_success(self, mock_parse, mock_config, mock_dify, mock_run, capsys):
        """Test main() with successful execution outputs JSON."""
        from estimator_king.__main__ import main

        # Setup mocks
        mock_parse.return_value = MagicMock(
            config="stores.yaml",
            db="/tmp/db.sqlite",
            dify_api_key="dataset-test",
            dify_base_url="https://test.com",
            dify_dataset_id="uuid-test",
        )
        mock_config.return_value = MagicMock()
        mock_dify.return_value = MagicMock()
        mock_run.return_value = {
            "discovered": 5,
            "fetched_ok": 5,
            "created": 2,
            "updated": 1,
            "skipped": 2,
            "inactive": 0,
            "errors": 0,
        }

        # Execute (should exit 0)
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 0

        # Verify JSON output
        captured = capsys.readouterr()
        import json

        output = json.loads(captured.out)
        assert output["discovered"] == 5
        assert output["created"] == 2
        assert output["fetched_ok"] == 5


class TestMainConfigLoadFailure:
    """Test main() with config loading failure."""

    @patch("estimator_king.__main__.AppConfig.from_yaml")
    @patch("estimator_king.__main__.parse_args")
    def test_main_config_load_failure(self, mock_parse, mock_config):
        """Test main() exits 1 when config loading fails."""
        from estimator_king.__main__ import main

        mock_parse.return_value = MagicMock(config="missing.yaml")
        mock_config.side_effect = FileNotFoundError("Config not found")

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1


class TestMainCrawlerFailure:
    """Test main() with crawler execution failure."""

    @patch("estimator_king.__main__.run_crawler")
    @patch("estimator_king.__main__.DifyKBClient")
    @patch("estimator_king.__main__.AppConfig.from_yaml")
    @patch("estimator_king.__main__.parse_args")
    def test_main_crawler_failure(self, mock_parse, mock_config, mock_dify, mock_run):
        """Test main() exits 1 when crawler fails."""
        from estimator_king.__main__ import main

        mock_parse.return_value = MagicMock(
            config="stores.yaml",
            db="/tmp/db.sqlite",
            dify_api_key="dataset-test",
            dify_base_url="https://test.com",
            dify_dataset_id="uuid-test",
        )
        mock_config.return_value = MagicMock()
        mock_dify.return_value = MagicMock()
        mock_run.side_effect = Exception("Crawler error")

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1


class TestMainLoggingToStderr:
    """Test main() logging configuration."""

    @patch("estimator_king.__main__.run_crawler")
    @patch("estimator_king.__main__.DifyKBClient")
    @patch("estimator_king.__main__.AppConfig.from_yaml")
    @patch("estimator_king.__main__.parse_args")
    def test_main_logging_to_stderr(
        self, mock_parse, mock_config, mock_dify, mock_run, capsys
    ):
        """Test main() outputs JSON to stdout (not stderr)."""
        from estimator_king.__main__ import main

        mock_parse.return_value = MagicMock(
            config="stores.yaml",
            db="/tmp/db.sqlite",
            dify_api_key="dataset-test",
            dify_base_url="https://test.com",
            dify_dataset_id="uuid-test",
        )
        mock_config.return_value = MagicMock()
        mock_dify.return_value = MagicMock()
        mock_run.return_value = {
            "discovered": 1,
            "fetched_ok": 1,
            "created": 0,
            "updated": 0,
            "skipped": 1,
            "inactive": 0,
            "errors": 0,
        }

        with pytest.raises(SystemExit):
            main()

        captured = capsys.readouterr()
        # JSON should be on stdout (verified separately)
        assert "discovered" in captured.out
        assert "{" in captured.out


class TestMainJsonFormat:
    """Test main() JSON output format."""

    @patch("estimator_king.__main__.run_crawler")
    @patch("estimator_king.__main__.DifyKBClient")
    @patch("estimator_king.__main__.AppConfig.from_yaml")
    @patch("estimator_king.__main__.parse_args")
    def test_main_json_format(
        self, mock_parse, mock_config, mock_dify, mock_run, capsys
    ):
        """Test main() outputs valid JSON with all counter keys."""
        from estimator_king.__main__ import main
        import json

        mock_parse.return_value = MagicMock(
            config="stores.yaml",
            db="/tmp/db.sqlite",
            dify_api_key="dataset-test",
            dify_base_url="https://test.com",
            dify_dataset_id="uuid-test",
        )
        mock_config.return_value = MagicMock()
        mock_dify.return_value = MagicMock()
        mock_run.return_value = {
            "discovered": 150,
            "fetched_ok": 148,
            "created": 5,
            "updated": 12,
            "skipped": 131,
            "inactive": 2,
            "errors": 2,
        }

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 0

        captured = capsys.readouterr()
        output = json.loads(captured.out)

        # Verify all expected keys present
        expected_keys = {
            "discovered",
            "fetched_ok",
            "created",
            "updated",
            "skipped",
            "inactive",
            "errors",
        }
        assert set(output.keys()) == expected_keys

        # Verify values
        assert output["discovered"] == 150
        assert output["fetched_ok"] == 148
        assert output["created"] == 5
        assert output["updated"] == 12
        assert output["skipped"] == 131
        assert output["inactive"] == 2
        assert output["errors"] == 2


class TestCLIIntegration:
    """Integration tests for full CLI execution via subprocess."""

    def test_cli_help_flag_integration(self):
        """Test --help flag via subprocess produces complete help text."""
        result = subprocess.run(
            [sys.executable, "-m", "estimator_king", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "Estimator King" in result.stdout
        assert "--config" in result.stdout
        assert "--db" in result.stdout
        assert "dify" in result.stdout.lower()

    def test_cli_missing_config_file(self, monkeypatch, tmp_path):
        """Test CLI exits with error when config file doesn't exist."""
        monkeypatch.setenv("DIFY_API_KEY", "test-key-12345")
        monkeypatch.setenv("DIFY_BASE_URL", "https://test.example.com")
        monkeypatch.setenv("DIFY_DATASET_ID", "test-uuid-12345")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "estimator_king",
                "--config",
                "/nonexistent/path/missing.yaml",
            ],
            capture_output=True,
            text=True,
            env=os.environ,
        )
        assert result.returncode == 1
        assert (
            "Failed to load config" in result.stderr or "No such file" in result.stderr
        )

    def test_cli_all_env_vars_success(self, monkeypatch):
        """Test CLI succeeds with all required env vars (integration mode)."""
        monkeypatch.setenv("DIFY_API_KEY", "dataset-integration-test")
        monkeypatch.setenv("DIFY_BASE_URL", "https://api.example.com")
        monkeypatch.setenv("DIFY_DATASET_ID", "uuid-integration-test")

        result = subprocess.run(
            [sys.executable, "-m", "estimator_king"],
            capture_output=True,
            text=True,
            env=os.environ,
            timeout=10,
        )
        # Should exit with 0 or 1 depending on actual crawler execution
        # (not testing actual crawl, just CLI arg parsing + startup)
        assert result.returncode in [0, 1]

    def test_cli_custom_db_path(self, monkeypatch, tmp_path):
        """Test CLI accepts custom database path argument."""
        db_path = str(tmp_path / "custom.db")
        monkeypatch.setenv("DIFY_API_KEY", "test-key")
        monkeypatch.setenv("DIFY_BASE_URL", "https://test.com")
        monkeypatch.setenv("DIFY_DATASET_ID", "test-uuid")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "estimator_king",
                "--db",
                db_path,
            ],
            capture_output=True,
            text=True,
            env=os.environ,
            timeout=10,
        )
        # CLI should accept the argument without error
        assert result.returncode in [0, 1]

    def test_cli_combined_cli_and_env_args(self, monkeypatch, tmp_path):
        """Test CLI with combination of CLI args and env vars."""
        config_path = str(tmp_path / "test_config.yaml")
        monkeypatch.setenv("DIFY_API_KEY", "env-key")
        monkeypatch.setenv("DIFY_BASE_URL", "https://env.example.com")
        monkeypatch.setenv("DIFY_DATASET_ID", "env-uuid")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "estimator_king",
                "--config",
                config_path,
                "--db",
                str(tmp_path / "test.db"),
            ],
            capture_output=True,
            text=True,
            env=os.environ,
            timeout=10,
        )
        # CLI args should override env + be accepted
        assert result.returncode in [0, 1]
