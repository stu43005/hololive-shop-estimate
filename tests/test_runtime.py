"""Tests for runtime.build_providers (shared provider construction)."""

from unittest.mock import MagicMock, patch

import pytest

from estimator_king.runtime import build_providers, MissingEmbeddingKey, Providers


def _make_cfg(*, embedding_api_key="sk-test"):
    mock_cfg = MagicMock()
    mock_cfg.chroma_path = "./chroma"
    provider_cfg = MagicMock()
    provider_cfg.embedding_api_key = embedding_api_key
    mock_cfg.build_provider_config.return_value = provider_cfg
    return mock_cfg


def test_build_providers_without_chat_skips_chat_provider():
    """Default (with_chat=False): embedder + vector_store built, chat stays None."""
    mock_cfg = _make_cfg()
    with patch("estimator_king.runtime.EmbeddingProvider") as mock_ep, \
         patch("estimator_king.runtime.VectorStore") as mock_vs, \
         patch("estimator_king.runtime.ChatProvider") as mock_chat:
        providers = build_providers(mock_cfg)

    assert isinstance(providers, Providers)
    mock_ep.assert_called_once_with(mock_cfg.build_provider_config.return_value)
    mock_vs.assert_called_once_with(mock_cfg.chroma_path)
    mock_chat.assert_not_called()
    assert providers.chat is None
    assert providers.embedder is mock_ep.return_value
    assert providers.vector_store is mock_vs.return_value


def test_build_providers_with_chat_builds_chat_provider():
    """with_chat=True: ChatProvider constructed once, providers.chat non-None."""
    mock_cfg = _make_cfg()
    with patch("estimator_king.runtime.EmbeddingProvider"), \
         patch("estimator_king.runtime.VectorStore"), \
         patch("estimator_king.runtime.ChatProvider") as mock_chat:
        providers = build_providers(mock_cfg, with_chat=True)

    mock_chat.assert_called_once_with(mock_cfg.build_provider_config.return_value)
    assert providers.chat is mock_chat.return_value


def test_build_providers_raises_when_embedding_key_missing():
    """Empty embedding key raises MissingEmbeddingKey (no sys.exit)."""
    for empty_key in (None, ""):
        mock_cfg = _make_cfg(embedding_api_key=empty_key)
        with pytest.raises(MissingEmbeddingKey):
            build_providers(mock_cfg)


def test_serve_shares_one_vector_store_between_scheduler_and_bot():
    """serve injects the SAME vector_store into CrawlScheduler and build_bot."""
    from unittest.mock import AsyncMock
    import asyncio as _asyncio
    from estimator_king import runtime

    providers = Providers(embedder=MagicMock(), vector_store=MagicMock(), chat=MagicMock())
    fake_bot = MagicMock()
    fake_bot.start = AsyncMock()
    cfg = MagicMock()
    cfg.discord_token = "tok"

    with patch("estimator_king.runtime.build_providers", return_value=providers), \
         patch("estimator_king.runtime._background_tasks", set()), \
         patch("estimator_king.runtime.CrawlScheduler") as MockSched, \
         patch("estimator_king.runtime.build_bot", return_value=fake_bot) as mock_build_bot, \
         patch("estimator_king.runtime.asyncio.create_task"), \
         patch("estimator_king.runtime.asyncio.get_running_loop"):
        _asyncio.run(runtime.serve(cfg, guild_id=None))

    sched_vs = MockSched.call_args.args[3]          # CrawlScheduler(config, db, embedder, vector_store)
    bot_vs = mock_build_bot.call_args.kwargs["vector_store"]
    assert sched_vs is bot_vs is providers.vector_store
    fake_bot.start.assert_awaited_once()
