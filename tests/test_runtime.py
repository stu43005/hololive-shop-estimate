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
