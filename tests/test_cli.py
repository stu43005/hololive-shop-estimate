"""Unit tests for CLI argument parser and orchestration (run/crawl subcommands)."""

import json
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from estimator_king.__main__ import parse_args, run_service, run_crawl
from estimator_king.runtime import MissingEmbeddingKey, Providers


# ---------------------------------------------------------------------------
# parse_args — crawl subcommand
# ---------------------------------------------------------------------------

def test_parse_args_crawl_flags():
    """crawl subcommand exposes --config / --force-refetch; no --dify-* flag."""
    args = parse_args(["crawl", "--config", "c.yaml", "--force-refetch"])
    assert args.command == "crawl"
    assert args.config == "c.yaml"
    assert args.force_refetch is True
    assert not hasattr(args, "dify_api_key")


def test_parse_args_crawl_defaults():
    """crawl with no extra args uses expected defaults."""
    args = parse_args(["crawl"])
    assert args.command == "crawl"
    assert args.config == "stores_config.yaml"
    assert args.db is None
    assert args.log_level == "INFO"
    assert args.force_refetch is False


def test_parse_args_crawl_db_flag():
    """crawl accepts --db."""
    args = parse_args(["crawl", "--db", "/tmp/test.db"])
    assert args.db == "/tmp/test.db"


def test_parse_args_crawl_log_level():
    """crawl accepts the shared --log-level."""
    args = parse_args(["crawl", "--log-level", "DEBUG"])
    assert args.log_level == "DEBUG"


# ---------------------------------------------------------------------------
# parse_args — run subcommand
# ---------------------------------------------------------------------------

def test_parse_args_run_defaults():
    """run with no extra args uses expected defaults."""
    args = parse_args(["run"])
    assert args.command == "run"
    assert args.config == "stores_config.yaml"
    assert args.token is None
    assert args.guild_id is None
    assert args.log_level == "INFO"


def test_parse_args_run_token_and_guild():
    """run accepts --token and --guild-id."""
    args = parse_args(["run", "--token", "abc", "--guild-id", "123"])
    assert args.command == "run"
    assert args.token == "abc"
    assert args.guild_id == 123


# ---------------------------------------------------------------------------
# parse_args — subcommand required
# ---------------------------------------------------------------------------

def test_parse_args_requires_subcommand():
    """No subcommand is a SystemExit (hard cutover: subcommand required)."""
    with pytest.raises(SystemExit):
        parse_args([])


# ---------------------------------------------------------------------------
# --help (subprocess)
# ---------------------------------------------------------------------------

def test_top_level_help_lists_subcommands():
    """Top-level --help lists the run and crawl subcommands, no --dify-* flag."""
    result = subprocess.run(
        [sys.executable, "-m", "estimator_king", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "Estimator King" in result.stdout
    assert "run" in result.stdout
    assert "crawl" in result.stdout
    assert "--dify" not in result.stdout


def test_crawl_help_lists_crawl_flags():
    """`crawl --help` lists --config / --db / --force-refetch; no --dify-* flag."""
    result = subprocess.run(
        [sys.executable, "-m", "estimator_king", "crawl", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "--config" in result.stdout
    assert "--db" in result.stdout
    assert "--force-refetch" in result.stdout
    assert "--dify" not in result.stdout


# ---------------------------------------------------------------------------
# run_crawl() — exit codes (mocked)
# ---------------------------------------------------------------------------

def _make_crawl_args(**kwargs):
    defaults = dict(config="stores.yaml", db=None, force_refetch=False)
    defaults.update(kwargs)
    return MagicMock(**defaults)


def _make_cfg():
    mock_cfg = MagicMock()
    mock_cfg.database_path = "./estimator_king.db"
    return mock_cfg


def test_run_crawl_success_prints_json_and_exits_0(capsys):
    mock_cfg = _make_cfg()
    counters = {"discovered": 5, "fetched_ok": 5, "created": 2,
                "updated": 1, "skipped": 2, "inactive": 0, "errors": 0}
    providers = Providers(embedder=MagicMock(), vector_store=MagicMock(), chat=None)
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.build_providers", return_value=providers), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock), \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_crawl_args())

    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)["discovered"] == 5


def test_run_crawl_missing_embedding_key_exits_2():
    mock_cfg = _make_cfg()
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.build_providers", side_effect=MissingEmbeddingKey()):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_crawl_args())
    assert exc.value.code == 2


def test_run_crawl_config_load_failure_exits_1():
    with patch("estimator_king.__main__.AppConfig.from_yaml",
               side_effect=FileNotFoundError("Config not found")):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_crawl_args(config="missing.yaml"))
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# run_bot() — routing + token handling (mocked)
# ---------------------------------------------------------------------------

def test_run_service_routes_to_serve_with_token_override():
    """run_service applies --token override and dispatches to runtime.serve."""
    mock_cfg = MagicMock()
    mock_cfg.discord_token = "cfg-token"
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.serve", new_callable=MagicMock) as mock_serve, \
         patch("estimator_king.__main__.asyncio.run") as mock_asyncio_run:
        run_service(MagicMock(config="stores.yaml", db=None, token="cli-token", guild_id=123))

    assert mock_cfg.discord_token == "cli-token"
    mock_serve.assert_called_once_with(mock_cfg, guild_id=123)
    mock_asyncio_run.assert_called_once_with(mock_serve.return_value)


def test_run_service_exits_1_when_token_missing():
    """run_service exits 1 when no token is provided and config has none."""
    mock_cfg = MagicMock()
    mock_cfg.discord_token = None
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg):
        with pytest.raises(SystemExit) as exc:
            run_service(MagicMock(config="stores.yaml", db=None, token=None, guild_id=None))
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# CLI integration (subprocess)
# ---------------------------------------------------------------------------

def test_cli_no_subcommand_exits_nonzero():
    """`python -m estimator_king` with no subcommand prints usage and exits nonzero."""
    result = subprocess.run(
        [sys.executable, "-m", "estimator_king"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "usage" in result.stderr.lower()


def test_cli_crawl_missing_config_file_exits_1():
    """`crawl --config <missing>` exits 1 with a config-load error."""
    result = subprocess.run(
        [sys.executable, "-m", "estimator_king", "crawl",
         "--config", "/nonexistent/path/missing.yaml"],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "Failed to load config" in result.stderr or "No such file" in result.stderr
