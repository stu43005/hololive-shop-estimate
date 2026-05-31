"""Composition root: shared provider construction and the long-lived service.

``build_providers`` is the single place that constructs the embedding / chat /
vector-store providers, shared by both the ``run`` and ``crawl`` commands. The
``serve`` composition root (added later) wires the bot and crawl scheduler as
two independent components over one shared VectorStore.
"""

from dataclasses import dataclass
from typing import Optional

from estimator_king.config_schema import AppConfig
from estimator_king.llm.embeddings import EmbeddingProvider
from estimator_king.llm.chat import ChatProvider
from estimator_king.vectorstore.store import VectorStore


class MissingEmbeddingKey(Exception):
    """Raised by build_providers when no embedding API key is configured.

    The caller maps this to its own exit code (crawl -> 2, run -> 1) so the
    validation lives in one place while CLI exit semantics stay per-command.
    """


@dataclass
class Providers:
    embedder: EmbeddingProvider
    vector_store: VectorStore
    chat: Optional[ChatProvider] = None


def build_providers(config: AppConfig, *, with_chat: bool = False) -> Providers:
    """Construct the shared providers; raise MissingEmbeddingKey if no key.

    chat is only built when with_chat=True (the bot needs it; crawl does not).
    Building ChatProvider with an empty chat_api_key raises OpenAIError under
    openai>=2, so crawl must never request it.
    """
    provider_config = config.build_provider_config()
    if not provider_config.embedding_api_key:
        raise MissingEmbeddingKey()
    embedder = EmbeddingProvider(provider_config)
    vector_store = VectorStore(config.chroma_path)
    chat = ChatProvider(provider_config) if with_chat else None
    return Providers(embedder=embedder, vector_store=vector_store, chat=chat)
