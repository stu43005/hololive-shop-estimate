from unittest.mock import MagicMock, patch

from estimator_king.llm.config import ProviderConfig
from estimator_king.llm.embeddings import EmbeddingProvider


def _fake_response(vectors):
    resp = MagicMock()
    resp.data = [MagicMock(embedding=v) for v in vectors]
    return resp


@patch("estimator_king.llm.embeddings.OpenAI")
def test_embed_documents_sends_model_dimensions_and_prefix(mock_openai):
    client = mock_openai.return_value
    client.embeddings.create.return_value = _fake_response([[0.1, 0.2], [0.3, 0.4]])
    cfg = ProviderConfig(
        embedding_api_key="k", chat_api_key="k",
        embedding_model="text-embedding-3-large", embedding_dimensions=1024,
        embedding_doc_prefix="passage: ",
    )

    provider = EmbeddingProvider(cfg)
    out = provider.embed_documents(["alpha", "beta"])

    assert out == [[0.1, 0.2], [0.3, 0.4]]
    kwargs = client.embeddings.create.call_args.kwargs
    assert kwargs["model"] == "text-embedding-3-large"
    assert kwargs["dimensions"] == 1024
    assert kwargs["input"] == ["passage: alpha", "passage: beta"]


@patch("estimator_king.llm.embeddings.OpenAI")
def test_embed_query_applies_query_prefix_and_returns_single_vector(mock_openai):
    client = mock_openai.return_value
    client.embeddings.create.return_value = _fake_response([[1.0, 2.0]])
    cfg = ProviderConfig(
        embedding_api_key="k", chat_api_key="k", embedding_query_prefix="query: "
    )

    provider = EmbeddingProvider(cfg)
    out = provider.embed_query("hello")

    assert out == [1.0, 2.0]
    assert client.embeddings.create.call_args.kwargs["input"] == ["query: hello"]


@patch("estimator_king.llm.embeddings.OpenAI")
def test_dimensions_omitted_when_none(mock_openai):
    client = mock_openai.return_value
    client.embeddings.create.return_value = _fake_response([[0.0]])
    cfg = ProviderConfig(embedding_api_key="k", chat_api_key="k", embedding_dimensions=None)

    EmbeddingProvider(cfg).embed_query("x")

    assert "dimensions" not in client.embeddings.create.call_args.kwargs


@patch("estimator_king.llm.embeddings.OpenAI")
def test_embed_documents_batches(mock_openai):
    client = mock_openai.return_value
    client.embeddings.create.side_effect = [
        _fake_response([[1.0]]), _fake_response([[2.0]]),
    ]
    cfg = ProviderConfig(embedding_api_key="k", chat_api_key="k", embedding_dimensions=None)

    out = EmbeddingProvider(cfg, batch_size=1).embed_documents(["a", "b"])

    assert out == [[1.0], [2.0]]
    assert client.embeddings.create.call_count == 2
