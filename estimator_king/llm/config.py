"""Configuration for the embedding + chat providers."""

from dataclasses import dataclass


@dataclass
class ProviderConfig:
    """Holds all provider settings. Embedding and chat are independent so a
    local-embed + hosted-chat split works without code changes. base_url=None
    means the OpenAI SDK uses its default (api.openai.com)."""

    # Embeddings
    embedding_api_key: str
    chat_api_key: str
    embedding_base_url: str | None = None
    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int | None = 1024
    embedding_max_tokens: int = 8192
    embedding_query_prefix: str = ""
    embedding_doc_prefix: str = ""

    # Chat
    chat_base_url: str | None = None
    chat_model: str = "gpt-4o"
    chat_structured_output: bool = True
