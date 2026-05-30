"""Embedding provider over the OpenAI-compatible embeddings API."""

import tiktoken
from openai import OpenAI

from estimator_king.llm.config import ProviderConfig


class EmbeddingProvider:
    """Computes embeddings via the OpenAI SDK. base_url-swappable to ollama.

    embed_documents applies embedding_doc_prefix; embed_query applies
    embedding_query_prefix (both empty by default, which OpenAI needs).

    Each input is truncated to embedding_max_tokens before the API call so a
    long product document never exceeds the model's context window (e.g.
    text-embedding-3-large rejects inputs over 8192 tokens). The price-relevant
    content (title, description, variants) is formatted first, so truncating the
    tail loses little retrieval signal.
    """

    _config: ProviderConfig
    _batch_size: int
    _client: OpenAI
    _encoding: "tiktoken.Encoding | None"

    def __init__(self, config: ProviderConfig, *, batch_size: int = 100) -> None:
        self._config = config
        self._batch_size = batch_size
        self._client = OpenAI(
            api_key=config.embedding_api_key,
            base_url=config.embedding_base_url,
        )
        self._encoding = self._resolve_encoding(config.embedding_model)

    @staticmethod
    def _resolve_encoding(model: str) -> "tiktoken.Encoding | None":
        """Return a tiktoken encoding for `model`, falling back to cl100k_base
        for non-OpenAI models (e.g. ollama). None only if even that is missing."""
        try:
            return tiktoken.encoding_for_model(model)
        except KeyError:
            try:
                return tiktoken.get_encoding("cl100k_base")
            except Exception:
                return None

    def _truncate(self, text: str) -> str:
        max_tokens = self._config.embedding_max_tokens
        if self._encoding is not None:
            tokens = self._encoding.encode(text)
            if len(tokens) <= max_tokens:
                return text
            return self._encoding.decode(tokens[:max_tokens])
        # No tokenizer available: conservative character cap (CJK is token-dense,
        # so ~1 char per token keeps us safely under the limit).
        return text[:max_tokens]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        inputs = [self._config.embedding_doc_prefix + t for t in texts]
        out: list[list[float]] = []
        for start in range(0, len(inputs), self._batch_size):
            out.extend(self._embed(inputs[start : start + self._batch_size]))
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self._config.embedding_query_prefix + text])[0]

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        inputs = [self._truncate(text) for text in inputs]
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
