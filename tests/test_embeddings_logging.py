import logging
from unittest.mock import MagicMock, patch

from estimator_king.llm.config import ProviderConfig
from estimator_king.llm.embeddings import EmbeddingProvider


def _fake_response(vectors):
    resp = MagicMock()
    resp.data = [MagicMock(embedding=v) for v in vectors]
    return resp


@patch("estimator_king.llm.embeddings.OpenAI")
def test_embed_emits_debug_with_count_and_model(mock_openai, caplog):
    client = mock_openai.return_value
    client.embeddings.create.return_value = _fake_response([[0.0]])
    cfg = ProviderConfig(
        embedding_api_key="k", chat_api_key="k",
        embedding_model="text-embedding-3-large", embedding_dimensions=None,
    )

    with caplog.at_level(logging.DEBUG, logger="estimator_king.llm.embeddings"):
        EmbeddingProvider(cfg).embed_query("hello")

    recs = [
        r for r in caplog.records
        if r.name == "estimator_king.llm.embeddings" and r.levelno == logging.DEBUG
    ]
    assert any(
        "embedding request" in r.getMessage()
        and "1 inputs" in r.getMessage()
        and "text-embedding-3-large" in r.getMessage()
        and "ms" in r.getMessage()
        for r in recs
    )


@patch("estimator_king.llm.embeddings.OpenAI")
def test_embed_emits_debug_with_dimensions_branch(mock_openai, caplog):
    client = mock_openai.return_value
    client.embeddings.create.return_value = _fake_response([[0.0]])
    cfg = ProviderConfig(
        embedding_api_key="k", chat_api_key="k",
        embedding_model="text-embedding-3-large", embedding_dimensions=512,
    )

    with caplog.at_level(logging.DEBUG, logger="estimator_king.llm.embeddings"):
        EmbeddingProvider(cfg).embed_query("hello")

    recs = [
        r for r in caplog.records
        if r.name == "estimator_king.llm.embeddings" and r.levelno == logging.DEBUG
    ]
    assert any(
        "embedding request" in r.getMessage()
        and "1 inputs" in r.getMessage()
        and "text-embedding-3-large" in r.getMessage()
        and "ms" in r.getMessage()
        for r in recs
    )
