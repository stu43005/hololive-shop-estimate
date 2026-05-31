"""Tests for run_crawl() wiring: build_providers -> run_crawl_cycle."""

import json
from unittest.mock import MagicMock, patch

import pytest

import estimator_king.__main__ as m
from estimator_king.__main__ import run_crawl
from estimator_king.runtime import Providers, MissingEmbeddingKey


def _make_args(**kwargs):
    defaults = dict(config="stores.yaml", db=None, force_refetch=False)
    defaults.update(kwargs)
    return MagicMock(**defaults)


def _make_cfg(*, db="./estimator_king.db"):
    mock_cfg = MagicMock()
    mock_cfg.database_path = db
    return mock_cfg


def _make_providers():
    return Providers(embedder=MagicMock(), vector_store=MagicMock(), typing_provider=MagicMock(), chat=None)


def test_run_crawl_passes_providers_to_cycle():
    """run_crawl passes providers.embedder / providers.vector_store to the cycle."""
    mock_cfg = _make_cfg()
    providers = _make_providers()
    counters = {"errors": 0}
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.build_providers", return_value=providers), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock) as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_args())

    assert exc.value.code == 0
    call_args = mock_cycle.call_args
    assert call_args.args[0] is mock_cfg
    assert call_args.args[2] is providers.embedder
    assert call_args.args[3] is providers.vector_store


def test_run_crawl_passes_force_refetch_to_cycle():
    mock_cfg = _make_cfg()
    providers = _make_providers()
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.build_providers", return_value=providers), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock) as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", return_value={"errors": 0}):
        with pytest.raises(SystemExit):
            run_crawl(_make_args(force_refetch=True))

    assert mock_cycle.call_args.kwargs.get("force_refetch") is True


def test_run_crawl_uses_db_path_from_config():
    mock_cfg = _make_cfg(db="/configured/path.db")
    providers = _make_providers()
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.build_providers", return_value=providers), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock) as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", return_value={"errors": 0}):
        with pytest.raises(SystemExit):
            run_crawl(_make_args())

    assert mock_cycle.call_args.args[1] == "/configured/path.db"


def test_run_crawl_applies_db_override_before_cycle():
    mock_cfg = _make_cfg(db="./original.db")
    providers = _make_providers()
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.build_providers", return_value=providers), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock) as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", return_value={"errors": 0}):
        with pytest.raises(SystemExit):
            run_crawl(_make_args(db="/override.db"))

    assert mock_cycle.call_args.args[1] == "/override.db"


def test_run_crawl_prints_json_counters(capsys):
    mock_cfg = _make_cfg()
    providers = _make_providers()
    counters = {"discovered": 10, "fetched_ok": 9, "created": 3,
                "updated": 2, "skipped": 4, "inactive": 1, "errors": 1}
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.build_providers", return_value=providers), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock), \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_args())

    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out) == counters


def test_run_crawl_exits_2_when_embedding_key_missing():
    """build_providers raising MissingEmbeddingKey maps to exit 2."""
    mock_cfg = _make_cfg()
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.build_providers",
               side_effect=MissingEmbeddingKey()):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_args())
    assert exc.value.code == 2


def test_run_crawl_exits_1_on_cycle_exception():
    mock_cfg = _make_cfg()
    providers = _make_providers()
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.build_providers", return_value=providers), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock), \
         patch("estimator_king.__main__.asyncio.run",
               side_effect=RuntimeError("network error")):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_args())
    assert exc.value.code == 1


def test_run_crawl_no_dify_client_constructed():
    """The refactored __main__ must NOT carry DifyKBClient nor the renamed run_bot."""
    assert not hasattr(m, "DifyKBClient")
    assert not hasattr(m, "run_bot")  # renamed to run_service in this task
