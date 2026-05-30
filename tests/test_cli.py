"""Unit tests for CLI argument parser and orchestration."""

import subprocess
import sys
import pytest
from unittest.mock import MagicMock, patch

from estimator_king.__main__ import parse_args


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------

def test_parse_args_has_no_dify_flags():
    """parse_args() must not expose any --dify-* flag."""
    args = parse_args(["--config", "c.yaml", "--force-refetch"])
    assert args.config == "c.yaml"
    assert args.force_refetch is True
    assert not hasattr(args, "dify_api_key")


def test_parse_args_defaults():
    """parse_args() with no arguments uses expected defaults."""
    args = parse_args([])
    assert args.config == "stores_config.yaml"
    assert args.db is None
    assert args.log_level == "INFO"
    assert args.force_refetch is False


def test_parse_args_config_flag():
    """parse_args() accepts --config."""
    args = parse_args(["--config", "custom.yaml"])
    assert args.config == "custom.yaml"


def test_parse_args_db_flag():
    """parse_args() accepts --db."""
    args = parse_args(["--db", "/tmp/test.db"])
    assert args.db == "/tmp/test.db"


def test_parse_args_log_level():
    """parse_args() accepts --log-level."""
    args = parse_args(["--log-level", "DEBUG"])
    assert args.log_level == "DEBUG"


def test_parse_args_force_refetch_default():
    """parse_args() --force-refetch defaults to False."""
    args = parse_args([])
    assert args.force_refetch is False


def test_parse_args_force_refetch_set():
    """parse_args() --force-refetch sets flag to True."""
    args = parse_args(["--force-refetch"])
    assert args.force_refetch is True


# ---------------------------------------------------------------------------
# --help
# ---------------------------------------------------------------------------

def test_help_flag():
    """--help flag produces help text that mentions the four expected flags."""
    result = subprocess.run(
        [sys.executable, "-m", "estimator_king", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Estimator King" in result.stdout
    assert "--config" in result.stdout
    assert "--db" in result.stdout
    assert "--force-refetch" in result.stdout
    # Dify flags must NOT appear
    assert "--dify" not in result.stdout


# ---------------------------------------------------------------------------
# main() unit tests (mocked)
# ---------------------------------------------------------------------------

class TestMainSuccess:
    """Test successful main() execution."""

    @patch("estimator_king.__main__.run_crawl_cycle")
    @patch("estimator_king.__main__.VectorStore")
    @patch("estimator_king.__main__.EmbeddingProvider")
    @patch("estimator_king.__main__.AppConfig.from_yaml")
    @patch("estimator_king.__main__.parse_args")
    def test_main_success(
        self, mock_parse, mock_from_yaml, mock_embedder_cls, mock_vs_cls,
        mock_cycle, capsys
    ):
        """main() prints JSON counters and exits 0 on success."""
        import json
        from estimator_king.__main__ import main

        mock_parse.return_value = MagicMock(
            config="stores.yaml",
            db=None,
            log_level="INFO",
            force_refetch=False,
        )
        mock_cfg = MagicMock()
        mock_cfg.database_path = "./estimator_king.db"
        mock_cfg.chroma_path = "./chroma"
        provider_cfg = MagicMock()
        provider_cfg.embedding_api_key = "sk-test"
        mock_cfg.build_provider_config.return_value = provider_cfg
        mock_from_yaml.return_value = mock_cfg

        mock_embedder_cls.return_value = MagicMock()
        mock_vs_cls.return_value = MagicMock()

        counters = {
            "discovered": 5,
            "fetched_ok": 5,
            "created": 2,
            "updated": 1,
            "skipped": 2,
            "inactive": 0,
            "errors": 0,
        }
        mock_cycle.return_value = counters
        # asyncio.run will call the coroutine — we stub the whole thing
        with patch("estimator_king.__main__.asyncio.run", return_value=counters):
            with pytest.raises(SystemExit) as exc:
                main()

        assert exc.value.code == 0
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["discovered"] == 5
        assert output["created"] == 2
        assert output["fetched_ok"] == 5


class TestMainMissingEmbeddingKey:
    """main() exits 2 when embedding_api_key is falsy."""

    @patch("estimator_king.__main__.AppConfig.from_yaml")
    @patch("estimator_king.__main__.parse_args")
    def test_missing_embedding_key(self, mock_parse, mock_from_yaml):
        from estimator_king.__main__ import main

        mock_parse.return_value = MagicMock(
            config="stores.yaml",
            db=None,
            log_level="INFO",
            force_refetch=False,
        )
        mock_cfg = MagicMock()
        mock_cfg.database_path = "./estimator_king.db"
        mock_cfg.chroma_path = "./chroma"
        provider_cfg = MagicMock()
        provider_cfg.embedding_api_key = ""  # missing
        mock_cfg.build_provider_config.return_value = provider_cfg
        mock_from_yaml.return_value = mock_cfg

        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2


class TestMainConfigLoadFailure:
    """main() exits 1 when config loading fails."""

    @patch("estimator_king.__main__.AppConfig.from_yaml")
    @patch("estimator_king.__main__.parse_args")
    def test_main_config_load_failure(self, mock_parse, mock_from_yaml):
        from estimator_king.__main__ import main

        mock_parse.return_value = MagicMock(config="missing.yaml", log_level="INFO")
        mock_from_yaml.side_effect = FileNotFoundError("Config not found")

        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1


class TestMainCrawlerFailure:
    """main() exits 1 when run_crawl_cycle raises."""

    @patch("estimator_king.__main__.AppConfig.from_yaml")
    @patch("estimator_king.__main__.parse_args")
    def test_main_crawler_failure(self, mock_parse, mock_from_yaml):
        from estimator_king.__main__ import main

        mock_parse.return_value = MagicMock(
            config="stores.yaml",
            db=None,
            log_level="INFO",
            force_refetch=False,
        )
        mock_cfg = MagicMock()
        mock_cfg.database_path = "./estimator_king.db"
        mock_cfg.chroma_path = "./chroma"
        provider_cfg = MagicMock()
        provider_cfg.embedding_api_key = "sk-test"
        mock_cfg.build_provider_config.return_value = provider_cfg
        mock_from_yaml.return_value = mock_cfg

        with patch("estimator_king.__main__.EmbeddingProvider"), \
             patch("estimator_king.__main__.VectorStore"), \
             patch("estimator_king.__main__.asyncio.run",
                   side_effect=RuntimeError("Crawler exploded")):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1


class TestMainJsonOutput:
    """main() writes valid JSON with all counter keys to stdout."""

    @patch("estimator_king.__main__.AppConfig.from_yaml")
    @patch("estimator_king.__main__.parse_args")
    def test_main_json_format(self, mock_parse, mock_from_yaml, capsys):
        import json
        from estimator_king.__main__ import main

        mock_parse.return_value = MagicMock(
            config="stores.yaml",
            db=None,
            log_level="INFO",
            force_refetch=False,
        )
        mock_cfg = MagicMock()
        mock_cfg.database_path = "./estimator_king.db"
        mock_cfg.chroma_path = "./chroma"
        provider_cfg = MagicMock()
        provider_cfg.embedding_api_key = "sk-test"
        mock_cfg.build_provider_config.return_value = provider_cfg
        mock_from_yaml.return_value = mock_cfg

        counters = {
            "discovered": 150,
            "fetched_ok": 148,
            "created": 5,
            "updated": 12,
            "skipped": 131,
            "inactive": 2,
            "errors": 2,
        }
        with patch("estimator_king.__main__.EmbeddingProvider"), \
             patch("estimator_king.__main__.VectorStore"), \
             patch("estimator_king.__main__.asyncio.run", return_value=counters):
            with pytest.raises(SystemExit) as exc:
                main()

        assert exc.value.code == 0
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["discovered"] == 150
        assert output["fetched_ok"] == 148
        assert output["created"] == 5
        assert output["updated"] == 12
        assert output["skipped"] == 131
        assert output["inactive"] == 2
        assert output["errors"] == 2


class TestMainDbOverride:
    """main() applies --db override to config.database_path."""

    @patch("estimator_king.__main__.AppConfig.from_yaml")
    @patch("estimator_king.__main__.parse_args")
    def test_db_override_applied(self, mock_parse, mock_from_yaml, capsys):
        from estimator_king.__main__ import main

        mock_parse.return_value = MagicMock(
            config="stores.yaml",
            db="/custom/override.db",
            log_level="INFO",
            force_refetch=False,
        )
        mock_cfg = MagicMock()
        mock_cfg.database_path = "./estimator_king.db"
        mock_cfg.chroma_path = "./chroma"
        provider_cfg = MagicMock()
        provider_cfg.embedding_api_key = "sk-test"
        mock_cfg.build_provider_config.return_value = provider_cfg
        mock_from_yaml.return_value = mock_cfg

        counters = {"discovered": 0, "fetched_ok": 0, "created": 0,
                    "updated": 0, "skipped": 0, "inactive": 0, "errors": 0}
        with patch("estimator_king.__main__.EmbeddingProvider"), \
             patch("estimator_king.__main__.VectorStore"), \
             patch("estimator_king.__main__.asyncio.run", return_value=counters):
            with pytest.raises(SystemExit):
                main()

        # database_path should have been overridden
        assert mock_cfg.database_path == "/custom/override.db"


# ---------------------------------------------------------------------------
# CLI integration (subprocess)
# ---------------------------------------------------------------------------

class TestCLIIntegration:
    """Integration tests via subprocess."""

    def test_cli_help_flag(self):
        """--help lists --config, --db, --force-refetch; no --dify- flags."""
        result = subprocess.run(
            [sys.executable, "-m", "estimator_king", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--config" in result.stdout
        assert "--db" in result.stdout
        assert "--force-refetch" in result.stdout
        assert "--dify" not in result.stdout

    def test_cli_missing_config_file(self, tmp_path):
        """CLI exits 1 when config file doesn't exist (no env key needed now)."""
        result = subprocess.run(
            [
                sys.executable, "-m", "estimator_king",
                "--config", "/nonexistent/path/missing.yaml",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "Failed to load config" in result.stderr or "No such file" in result.stderr
