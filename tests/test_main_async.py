"""Tests for run_crawl() wiring: EmbeddingProvider + VectorStore + run_crawl_cycle."""

import json
from unittest.mock import MagicMock, patch

import pytest

import estimator_king.__main__ as m
from estimator_king.__main__ import run_crawl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_args(**kwargs):
    defaults = dict(
        config="stores.yaml",
        db=None,
        force_refetch=False,
    )
    defaults.update(kwargs)
    return MagicMock(**defaults)


def _make_cfg(*, embedding_api_key: str = "sk-test", db: str = "./estimator_king.db"):
    mock_cfg = MagicMock()
    mock_cfg.database_path = db
    mock_cfg.chroma_path = "./chroma"
    provider_cfg = MagicMock()
    provider_cfg.embedding_api_key = embedding_api_key
    mock_cfg.build_provider_config.return_value = provider_cfg
    return mock_cfg


# ---------------------------------------------------------------------------
# run_crawl() builds EmbeddingProvider and VectorStore, then calls run_crawl_cycle
# ---------------------------------------------------------------------------

def test_run_crawl_builds_embedding_provider_and_vector_store():
    """run_crawl() constructs EmbeddingProvider(provider_config) and VectorStore(chroma_path)."""
    mock_cfg = _make_cfg()
    counters = {"discovered": 0, "fetched_ok": 0, "created": 0,
                "updated": 0, "skipped": 0, "inactive": 0, "errors": 0}

    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider") as mock_ep, \
         patch("estimator_king.__main__.VectorStore") as mock_vs, \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock), \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_args())

    assert exc.value.code == 0
    mock_ep.assert_called_once_with(mock_cfg.build_provider_config.return_value)
    mock_vs.assert_called_once_with(mock_cfg.chroma_path)


def test_run_crawl_passes_embedder_and_vector_store_to_cycle():
    """run_crawl() passes the embedder and vector store to run_crawl_cycle(...)."""
    mock_cfg = _make_cfg()
    counters = {"discovered": 1, "fetched_ok": 1, "created": 0,
                "updated": 0, "skipped": 1, "inactive": 0, "errors": 0}

    captured_coro = []

    def fake_asyncio_run(coro):
        captured_coro.append(coro)
        return counters

    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider") as mock_ep, \
         patch("estimator_king.__main__.VectorStore") as mock_vs, \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock) as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", side_effect=fake_asyncio_run):
        with pytest.raises(SystemExit):
            run_crawl(_make_args())

    assert mock_cycle.called
    call_args = mock_cycle.call_args
    assert call_args.args[0] is mock_cfg  # config
    assert call_args.args[2] is mock_ep.return_value  # embedder
    assert call_args.args[3] is mock_vs.return_value  # vector_store


def test_run_crawl_passes_force_refetch_to_cycle():
    """run_crawl() passes force_refetch=True to run_crawl_cycle when --force-refetch given."""
    mock_cfg = _make_cfg()
    counters = {"discovered": 0, "fetched_ok": 0, "created": 0,
                "updated": 0, "skipped": 0, "inactive": 0, "errors": 0}

    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider"), \
         patch("estimator_king.__main__.VectorStore"), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock) as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit):
            run_crawl(_make_args(force_refetch=True))

    assert mock_cycle.call_args.kwargs.get("force_refetch") is True


def test_run_crawl_uses_db_path_from_config():
    """run_crawl() passes config.database_path to run_crawl_cycle."""
    mock_cfg = _make_cfg(db="/configured/path.db")
    counters = {"discovered": 0, "fetched_ok": 0, "created": 0,
                "updated": 0, "skipped": 0, "inactive": 0, "errors": 0}

    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider"), \
         patch("estimator_king.__main__.VectorStore"), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock) as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit):
            run_crawl(_make_args())

    assert mock_cycle.call_args.args[1] == "/configured/path.db"


def test_run_crawl_applies_db_override_before_cycle():
    """run_crawl() overrides config.database_path with --db value before calling cycle."""
    mock_cfg = _make_cfg(db="./original.db")
    counters = {"discovered": 0, "fetched_ok": 0, "created": 0,
                "updated": 0, "skipped": 0, "inactive": 0, "errors": 0}

    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider"), \
         patch("estimator_king.__main__.VectorStore"), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock) as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit):
            run_crawl(_make_args(db="/override.db"))

    assert mock_cycle.call_args.args[1] == "/override.db"


def test_run_crawl_prints_json_counters(capsys):
    """run_crawl() prints JSON counters from run_crawl_cycle to stdout."""
    mock_cfg = _make_cfg()
    counters = {"discovered": 10, "fetched_ok": 9, "created": 3,
                "updated": 2, "skipped": 4, "inactive": 1, "errors": 1}

    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider"), \
         patch("estimator_king.__main__.VectorStore"), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock), \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_args())

    assert exc.value.code == 0
    output = json.loads(capsys.readouterr().out)
    assert output == counters


def test_run_crawl_exits_2_when_embedding_key_missing():
    """run_crawl() exits 2 when embedding_api_key is falsy (None or empty string)."""
    for empty_key in (None, ""):
        mock_cfg = _make_cfg(embedding_api_key=empty_key)
        with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg):
            with pytest.raises(SystemExit) as exc:
                run_crawl(_make_args())
        assert exc.value.code == 2, f"Expected exit 2 for embedding_api_key={empty_key!r}"


def test_run_crawl_exits_1_on_cycle_exception():
    """run_crawl() exits 1 when asyncio.run(run_crawl_cycle(...)) raises."""
    mock_cfg = _make_cfg()
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider"), \
         patch("estimator_king.__main__.VectorStore"), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock), \
         patch("estimator_king.__main__.asyncio.run",
               side_effect=RuntimeError("network error")):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_args())

    assert exc.value.code == 1


def test_run_crawl_no_dify_client_constructed():
    """The refactored __main__ must NOT carry a DifyKBClient symbol."""
    assert not hasattr(m, "DifyKBClient"), (
        "DifyKBClient should not be present in the refactored __main__"
    )
