"""Embedding provider over the OpenAI-compatible embeddings API."""

from openai import OpenAI

from estimator_king.llm.config import ProviderConfig


class EmbeddingProvider:
    """Computes embeddings via the OpenAI SDK. base_url-swappable to ollama.

    embed_documents applies embedding_doc_prefix; embed_query applies
    embedding_query_prefix (both empty by default, which OpenAI needs).
    """

    _config: ProviderConfig
    _batch_size: int
    _client: OpenAI

    def __init__(self, config: ProviderConfig, *, batch_size: int = 100) -> None:
        self._config = config
        self._batch_size = batch_size
        self._client = OpenAI(
            api_key=config.embedding_api_key,
            base_url=config.embedding_base_url,
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        inputs = [self._config.embedding_doc_prefix + t for t in texts]
        out: list[list[float]] = []
        for start in range(0, len(inputs), self._batch_size):
            out.extend(self._embed(inputs[start : start + self._batch_size]))
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self._config.embedding_query_prefix + text])[0]

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        if self._config.embedding_dimensions is not None:
            response = self._client.embeddings.create(
                model=self._config.embedding_model,
                input=inputs,
                dimensions=self._config.embedding_dimensions,
            )
        else:
            response = self._client.embeddings.create(
                model=self._config.embedding_model,
                input=inputs,
            )
        return [item.embedding for item in response.data]
