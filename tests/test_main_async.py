"""Tests for main() wiring: EmbeddingProvider + VectorStore + run_crawl_cycle."""

import json
from unittest.mock import MagicMock, patch

import pytest



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_args(**kwargs):
    defaults = dict(
        config="stores.yaml",
        db=None,
        log_level="INFO",
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
# main() builds EmbeddingProvider and VectorStore, then calls run_crawl_cycle
# ---------------------------------------------------------------------------

def test_main_builds_embedding_provider_and_vector_store():
    """main() constructs EmbeddingProvider(provider_config) and VectorStore(chroma_path)."""
    from estimator_king.__main__ import main

    mock_cfg = _make_cfg()
    counters = {"discovered": 0, "fetched_ok": 0, "created": 0,
                "updated": 0, "skipped": 0, "inactive": 0, "errors": 0}

    with patch("estimator_king.__main__.parse_args", return_value=_make_args()), \
         patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider") as mock_ep, \
         patch("estimator_king.__main__.VectorStore") as mock_vs, \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit) as exc:
            main()

    assert exc.value.code == 0
    # EmbeddingProvider must be constructed with the provider_config
    mock_ep.assert_called_once_with(mock_cfg.build_provider_config.return_value)
    # VectorStore must be constructed with chroma_path
    mock_vs.assert_called_once_with(mock_cfg.chroma_path)


def test_main_passes_embedder_and_vector_store_to_cycle():
    """main() passes EmbeddingProvider and VectorStore instances to asyncio.run(run_crawl_cycle(...))."""
    from estimator_king.__main__ import main

    mock_cfg = _make_cfg()
    counters = {"discovered": 1, "fetched_ok": 1, "created": 0,
                "updated": 0, "skipped": 1, "inactive": 0, "errors": 0}

    captured_coro = []

    def fake_asyncio_run(coro):
        captured_coro.append(coro)
        return counters

    with patch("estimator_king.__main__.parse_args", return_value=_make_args()), \
         patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider") as mock_ep, \
         patch("estimator_king.__main__.VectorStore") as mock_vs, \
         patch("estimator_king.__main__.run_crawl_cycle") as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", side_effect=fake_asyncio_run):
        with pytest.raises(SystemExit):
            main()

    # run_crawl_cycle must have been called with (config, db_path, embedder, vector_store, force_refetch=...)
    assert mock_cycle.called
    call_args = mock_cycle.call_args
    # positional: config, db_path, embedder, vector_store
    assert call_args.args[0] is mock_cfg  # config
    assert call_args.args[2] is mock_ep.return_value  # embedder
    assert call_args.args[3] is mock_vs.return_value  # vector_store


def test_main_passes_force_refetch_to_cycle():
    """main() passes force_refetch=True to run_crawl_cycle when --force-refetch given."""
    from estimator_king.__main__ import main

    mock_cfg = _make_cfg()
    counters = {"discovered": 0, "fetched_ok": 0, "created": 0,
                "updated": 0, "skipped": 0, "inactive": 0, "errors": 0}

    with patch("estimator_king.__main__.parse_args", return_value=_make_args(force_refetch=True)), \
         patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider"), \
         patch("estimator_king.__main__.VectorStore"), \
         patch("estimator_king.__main__.run_crawl_cycle") as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit):
            main()

    call_kwargs = mock_cycle.call_args.kwargs
    assert call_kwargs.get("force_refetch") is True


def test_main_uses_db_path_from_config():
    """main() passes config.database_path to run_crawl_cycle."""
    from estimator_king.__main__ import main

    mock_cfg = _make_cfg(db="/configured/path.db")
    counters = {"discovered": 0, "fetched_ok": 0, "created": 0,
                "updated": 0, "skipped": 0, "inactive": 0, "errors": 0}

    with patch("estimator_king.__main__.parse_args", return_value=_make_args()), \
         patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider"), \
         patch("estimator_king.__main__.VectorStore"), \
         patch("estimator_king.__main__.run_crawl_cycle") as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit):
            main()

    call_args = mock_cycle.call_args
    assert call_args.args[1] == "/configured/path.db"


def test_main_applies_db_override_before_cycle():
    """main() overrides config.database_path with --db value before calling cycle."""
    from estimator_king.__main__ import main

    mock_cfg = _make_cfg(db="./original.db")
    counters = {"discovered": 0, "fetched_ok": 0, "created": 0,
                "updated": 0, "skipped": 0, "inactive": 0, "errors": 0}

    with patch("estimator_king.__main__.parse_args", return_value=_make_args(db="/override.db")), \
         patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider"), \
         patch("estimator_king.__main__.VectorStore"), \
         patch("estimator_king.__main__.run_crawl_cycle") as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit):
            main()

    # The override should be reflected in the db_path positional arg
    call_args = mock_cycle.call_args
    assert call_args.args[1] == "/override.db"


def test_main_prints_json_counters(capsys):
    """main() prints JSON counters from run_crawl_cycle to stdout."""
    from estimator_king.__main__ import main

    mock_cfg = _make_cfg()
    counters = {
        "discovered": 10,
        "fetched_ok": 9,
        "created": 3,
        "updated": 2,
        "skipped": 4,
        "inactive": 1,
        "errors": 1,
    }

    with patch("estimator_king.__main__.parse_args", return_value=_make_args()), \
         patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider"), \
         patch("estimator_king.__main__.VectorStore"), \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit) as exc:
            main()

    assert exc.value.code == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output == counters


def test_main_exits_2_when_embedding_key_missing():
    """main() exits 2 when embedding_api_key is falsy (None or empty string)."""
    from estimator_king.__main__ import main

    for empty_key in (None, ""):
        mock_cfg = _make_cfg(embedding_api_key=empty_key)

        with patch("estimator_king.__main__.parse_args", return_value=_make_args()), \
             patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg):
            with pytest.raises(SystemExit) as exc:
                main()

        assert exc.value.code == 2, f"Expected exit 2 for embedding_api_key={empty_key!r}"


def test_main_exits_1_on_cycle_exception():
    """main() exits 1 when asyncio.run(run_crawl_cycle(...)) raises."""
    from estimator_king.__main__ import main

    mock_cfg = _make_cfg()

    with patch("estimator_king.__main__.parse_args", return_value=_make_args()), \
         patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider"), \
         patch("estimator_king.__main__.VectorStore"), \
         patch("estimator_king.__main__.asyncio.run",
               side_effect=RuntimeError("network error")):
        with pytest.raises(SystemExit) as exc:
            main()

    assert exc.value.code == 1


def test_main_no_dify_client_constructed():
    """main() must NOT construct a DifyKBClient — only EmbeddingProvider + VectorStore."""
    from estimator_king.__main__ import main

    mock_cfg = _make_cfg()
    counters = {"discovered": 0, "fetched_ok": 0, "created": 0,
                "updated": 0, "skipped": 0, "inactive": 0, "errors": 0}

    with patch("estimator_king.__main__.parse_args", return_value=_make_args()), \
         patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider"), \
         patch("estimator_king.__main__.VectorStore"), \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        # If DifyKBClient were still imported and instantiated, the import would
        # fail on the refactored module (the symbol no longer exists). We assert
        # the module attribute is absent.
        import estimator_king.__main__ as m
        assert not hasattr(m, "DifyKBClient"), (
            "DifyKBClient should not be present in the refactored __main__"
        )
        with pytest.raises(SystemExit):
            main()
