# Remove Dify → Embedded ChromaDB + OpenAI Providers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all Dify dependencies (Knowledge Base + Workflow) with an in-process ChromaDB vector store and an OpenAI-compatible provider layer, merge the crawler into the bot as a daily budgeted scheduler, and rebuild the SQLite schema greenfield.

**Architecture:** A single bot process owns SQLite + an embedded ChromaDB `PersistentClient` on one PVC. A provider layer (`estimator_king/llm/`) wraps the OpenAI SDK for embeddings and chat (swappable to ollama via `base_url`). The crawler runs as an in-process asyncio scheduler that, per store per daily cycle, always fetches new products and tops up with the oldest existing products within a budget (`max_products_per_run`, default 32). `/estimate` retrieves references from ChromaDB and asks the chat model for structured estimates.

**Tech Stack:** Python 3, `chromadb` (1.5.x), `openai` (2.x), `pydantic` (2.x), `discord.py`, `aiohttp`, SQLite, pytest.

**Reference spec:** `docs/superpowers/specs/2026-05-30-remove-dify-chromadb-design.md`

---

## File structure

**New files**
- `estimator_king/llm/__init__.py` — package marker.
- `estimator_king/llm/config.py` — `ProviderConfig` dataclass.
- `estimator_king/llm/embeddings.py` — `EmbeddingProvider`.
- `estimator_king/llm/chat.py` — Pydantic estimate models + `ChatProvider` + `EstimationError`.
- `estimator_king/vectorstore/__init__.py` — package marker.
- `estimator_king/vectorstore/store.py` — `VectorStore`, `QueryHit`.
- `estimator_king/crawler/cycle.py` — `run_crawl_cycle()` shared by CLI + scheduler.
- `estimator_king/bot/estimator.py` — `Estimator` (replaces `workflow_client.py`).
- `estimator_king/bot/scheduler.py` — `CrawlScheduler`.
- Test files mirroring the above under `tests/`.

**Modified files**
- `requirements.txt`, `estimator_king/database/schema.sql`, `estimator_king/database/repository.py`,
  `estimator_king/sync/engine.py`, `estimator_king/sync/inactive.py`,
  `estimator_king/crawler/pipeline.py`, `estimator_king/crawler/async_pipeline.py`,
  `estimator_king/config_schema.py`, `estimator_king/__main__.py`, `estimator_king/bot/commands.py`,
  `estimator_king/bot/__main__.py`, `deploy/*`, `README.md`, `.env.example`, `docs/*runbook.md`.

**Deleted files**
- `estimator_king/sync/dify_client.py`, `estimator_king/sync/async_dify.py`, `estimator_king/bot/workflow_client.py`.
- Tests: `test_dify_client.py`, `test_async_dify_wrapper.py`, `test_poll_indexing_status.py`,
  `test_bot_workflow_client.py`, `test_sync_products_docid.py`, `test_sync_fire_and_forget.py`, `test_migration.py`.
- `dify-deploy/` (whole dir), `deploy/crawler-cronjob.yaml`, `docs/dify-dataset-setup.md`,
  `docs/dify-workflow-contract.md`, `dify_python_sdk_research_report.md`, `estimator-dify-plan.md`, `dify/` (if present).

**Validation per task:** `basedpyright` (type check), `ruff check` (lint), and the task's `pytest` selection. The user's rules require all three to pass before a task is "done".

---

## Task 1: Dependencies

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Replace `dify-client` with the new dependencies**

Set `requirements.txt` to exactly:

```text
requests
aiohttp
beautifulsoup4
markdownify
lxml
discord.py
tenacity
pyyaml
chromadb
openai
pydantic
pytest
pytest-cov
pytest-asyncio
responses
```

- [ ] **Step 2: Install**

Run: `pip install -r requirements.txt`
Expected: chromadb, openai, pydantic install successfully; `dify-client` no longer installed.

- [ ] **Step 3: Verify imports resolve**

Run: `python -c "import chromadb, openai, pydantic; print(chromadb.__version__, openai.__version__, pydantic.VERSION)"`
Expected: prints three version strings (chromadb ~1.5.x, openai ~2.x, pydantic ~2.x), no ImportError.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "build: swap dify-client for chromadb, openai, pydantic"
```

---

## Task 2: ProviderConfig

**Files:**
- Create: `estimator_king/llm/__init__.py`
- Create: `estimator_king/llm/config.py`
- Test: `tests/test_provider_config.py`

- [ ] **Step 1: Create the package marker**

Create `estimator_king/llm/__init__.py`:

```python
"""LLM provider abstraction (embeddings + chat) over OpenAI-compatible APIs."""
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_provider_config.py`:

```python
from estimator_king.llm.config import ProviderConfig


def test_defaults_match_spec():
    cfg = ProviderConfig(embedding_api_key="k", chat_api_key="k")
    assert cfg.embedding_model == "text-embedding-3-large"
    assert cfg.embedding_dimensions == 1024
    assert cfg.chat_model == "gpt-4o"
    assert cfg.chat_structured_output is True
    assert cfg.embedding_base_url is None
    assert cfg.embedding_query_prefix == ""
    assert cfg.embedding_doc_prefix == ""


def test_overrides_apply():
    cfg = ProviderConfig(
        embedding_api_key="e",
        chat_api_key="c",
        embedding_base_url="http://ollama:11434/v1",
        embedding_model="bge-m3",
        embedding_dimensions=None,
        chat_model="qwen2",
        chat_structured_output=False,
    )
    assert cfg.embedding_base_url == "http://ollama:11434/v1"
    assert cfg.embedding_dimensions is None
    assert cfg.chat_structured_output is False
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_provider_config.py -v`
Expected: FAIL — `ModuleNotFoundError: estimator_king.llm.config`.

- [ ] **Step 4: Implement `ProviderConfig`**

Create `estimator_king/llm/config.py`:

```python
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
    embedding_query_prefix: str = ""
    embedding_doc_prefix: str = ""

    # Chat
    chat_base_url: str | None = None
    chat_model: str = "gpt-4o"
    chat_structured_output: bool = True
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_provider_config.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Type-check, lint, commit**

```bash
basedpyright estimator_king/llm/config.py
ruff check estimator_king/llm/ tests/test_provider_config.py
git add estimator_king/llm/__init__.py estimator_king/llm/config.py tests/test_provider_config.py
git commit -m "feat(llm): add ProviderConfig dataclass"
```

---

## Task 2 covers config only; Task 3 wires the OpenAI SDK.

## Task 3: EmbeddingProvider

**Files:**
- Create: `estimator_king/llm/embeddings.py`
- Test: `tests/test_embeddings.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_embeddings.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_embeddings.py -v`
Expected: FAIL — `ModuleNotFoundError: estimator_king.llm.embeddings`.

- [ ] **Step 3: Implement `EmbeddingProvider`**

Create `estimator_king/llm/embeddings.py`:

```python
"""Embedding provider over the OpenAI-compatible embeddings API."""

from openai import OpenAI

from estimator_king.llm.config import ProviderConfig


class EmbeddingProvider:
    """Computes embeddings via the OpenAI SDK. base_url-swappable to ollama.

    embed_documents applies embedding_doc_prefix; embed_query applies
    embedding_query_prefix (both empty by default, which OpenAI needs).
    """

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
        kwargs: dict[str, object] = {
            "model": self._config.embedding_model,
            "input": inputs,
        }
        if self._config.embedding_dimensions is not None:
            kwargs["dimensions"] = self._config.embedding_dimensions
        response = self._client.embeddings.create(**kwargs)  # type: ignore[arg-type]
        return [item.embedding for item in response.data]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_embeddings.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Type-check, lint, commit**

```bash
basedpyright estimator_king/llm/embeddings.py
ruff check estimator_king/llm/embeddings.py tests/test_embeddings.py
git add estimator_king/llm/embeddings.py tests/test_embeddings.py
git commit -m "feat(llm): add EmbeddingProvider with batching and prefixes"
```

---

## Task 4: ChatProvider + estimate models

**Files:**
- Create: `estimator_king/llm/chat.py`
- Test: `tests/test_chat_provider.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_chat_provider.py`:

```python
import json
from unittest.mock import MagicMock, patch

import pytest

from estimator_king.llm.chat import ChatProvider, EstimateBatch, EstimationError
from estimator_king.llm.config import ProviderConfig

VALID = {
    "estimates": [
        {
            "product_name": "p1",
            "suggested_price_jpy": 2000,
            "price_range_jpy": {"min": 1800, "max": 2200},
            "confidence": "high",
            "rationale": "because",
            "reference_products": [
                {"name": "ref", "price_jpy": 2000, "store": "hololive"}
            ],
        }
    ]
}


@patch("estimator_king.llm.chat.OpenAI")
def test_structured_output_uses_parse_and_returns_batch(mock_openai):
    client = mock_openai.return_value
    parsed = EstimateBatch.model_validate(VALID)
    msg = MagicMock(parsed=parsed, refusal=None)
    client.chat.completions.parse.return_value = MagicMock(choices=[MagicMock(message=msg)])
    cfg = ProviderConfig(embedding_api_key="k", chat_api_key="k", chat_structured_output=True)

    out = ChatProvider(cfg).estimate("sys", "user")

    assert isinstance(out, EstimateBatch)
    assert out.estimates[0].suggested_price_jpy == 2000
    kwargs = client.chat.completions.parse.call_args.kwargs
    assert kwargs["model"] == "gpt-4o"
    assert kwargs["response_format"] is EstimateBatch


@patch("estimator_king.llm.chat.OpenAI")
def test_structured_output_refusal_raises(mock_openai):
    client = mock_openai.return_value
    msg = MagicMock(parsed=None, refusal="no")
    client.chat.completions.parse.return_value = MagicMock(choices=[MagicMock(message=msg)])
    cfg = ProviderConfig(embedding_api_key="k", chat_api_key="k", chat_structured_output=True)

    with pytest.raises(EstimationError):
        ChatProvider(cfg).estimate("sys", "user")


@patch("estimator_king.llm.chat.OpenAI")
def test_json_object_mode_parses_content(mock_openai):
    client = mock_openai.return_value
    msg = MagicMock(content=json.dumps(VALID))
    client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=msg)])
    cfg = ProviderConfig(embedding_api_key="k", chat_api_key="k", chat_structured_output=False)

    out = ChatProvider(cfg).estimate("sys", "user")

    assert out.estimates[0].confidence == "high"
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}


@patch("estimator_king.llm.chat.OpenAI")
def test_json_object_mode_invalid_json_raises(mock_openai):
    client = mock_openai.return_value
    msg = MagicMock(content="not json")
    client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=msg)])
    cfg = ProviderConfig(embedding_api_key="k", chat_api_key="k", chat_structured_output=False)

    with pytest.raises(EstimationError):
        ChatProvider(cfg).estimate("sys", "user")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_chat_provider.py -v`
Expected: FAIL — `ModuleNotFoundError: estimator_king.llm.chat`.

- [ ] **Step 3: Implement chat models + provider**

Create `estimator_king/llm/chat.py`:

```python
"""Chat provider that returns structured price estimates."""

import json

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from estimator_king.llm.config import ProviderConfig


class PriceRange(BaseModel):
    min: int
    max: int


class ReferenceProduct(BaseModel):
    name: str
    price_jpy: int
    store: str


class ProductEstimate(BaseModel):
    product_name: str
    suggested_price_jpy: int
    price_range_jpy: PriceRange
    confidence: str
    rationale: str
    reference_products: list[ReferenceProduct]


class EstimateBatch(BaseModel):
    estimates: list[ProductEstimate]


class EstimationError(Exception):
    """Raised when the chat model refuses or returns unparseable output."""


class ChatProvider:
    """Calls the chat model and returns a validated EstimateBatch.

    When chat_structured_output is True, uses chat.completions.parse with the
    EstimateBatch schema. Otherwise uses json_object mode and validates manually
    (for endpoints without strict schema support, e.g. ollama).
    """

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self._client = OpenAI(api_key=config.chat_api_key, base_url=config.chat_base_url)

    def estimate(self, system_prompt: str, user_prompt: str) -> EstimateBatch:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        if self._config.chat_structured_output:
            return self._estimate_structured(messages)
        return self._estimate_json_object(messages)

    def _estimate_structured(self, messages: list[dict[str, str]]) -> EstimateBatch:
        response = self._client.chat.completions.parse(
            model=self._config.chat_model,
            messages=messages,  # type: ignore[arg-type]
            response_format=EstimateBatch,
        )
        message = response.choices[0].message
        if message.refusal:
            raise EstimationError(f"model refused: {message.refusal}")
        if message.parsed is None:
            raise EstimationError("structured output returned no parsed value")
        return message.parsed

    def _estimate_json_object(self, messages: list[dict[str, str]]) -> EstimateBatch:
        response = self._client.chat.completions.create(
            model=self._config.chat_model,
            messages=messages,  # type: ignore[arg-type]
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or ""
        try:
            data = json.loads(content)
            return EstimateBatch.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise EstimationError(f"could not parse estimates: {exc}") from exc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_chat_provider.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Type-check, lint, commit**

```bash
basedpyright estimator_king/llm/chat.py
ruff check estimator_king/llm/chat.py tests/test_chat_provider.py
git add estimator_king/llm/chat.py tests/test_chat_provider.py
git commit -m "feat(llm): add ChatProvider with structured + json_object output"
```

---

## Task 5: VectorStore

**Files:**
- Create: `estimator_king/vectorstore/__init__.py`
- Create: `estimator_king/vectorstore/store.py`
- Test: `tests/test_vector_store.py`

- [ ] **Step 1: Create the package marker**

Create `estimator_king/vectorstore/__init__.py`:

```python
"""Embedded ChromaDB vector store wrapper."""
```

- [ ] **Step 2: Write the failing test** (real ChromaDB against a tmp dir)

Create `tests/test_vector_store.py`:

```python
import pytest

from estimator_king.vectorstore.store import QueryHit, VectorStore


@pytest.fixture
def store(tmp_path):
    return VectorStore(str(tmp_path / "chroma"))


def test_upsert_then_query_returns_nearest_first(store):
    store.upsert("hololive:1", "red shirt", [1.0, 0.0], {"store_id": "hololive", "price_jpy": 100})
    store.upsert("hololive:2", "blue shirt", [0.0, 1.0], {"store_id": "hololive", "price_jpy": 200})

    hits = store.query([0.9, 0.1], n_results=2)

    assert [h.id for h in hits] == ["hololive:1", "hololive:2"]
    assert isinstance(hits[0], QueryHit)
    assert hits[0].metadata["price_jpy"] == 100
    assert hits[0].document == "red shirt"


def test_upsert_updates_existing_id(store):
    store.upsert("hololive:1", "v1", [1.0, 0.0], {"store_id": "hololive"})
    store.upsert("hololive:1", "v2", [1.0, 0.0], {"store_id": "hololive"})

    hits = store.query([1.0, 0.0], n_results=5)

    assert len(hits) == 1
    assert hits[0].document == "v2"


def test_where_filters_by_metadata(store):
    store.upsert("hololive:1", "a", [1.0, 0.0], {"store_id": "hololive"})
    store.upsert("vspo:1", "b", [1.0, 0.0], {"store_id": "vspo"})

    hits = store.query([1.0, 0.0], n_results=5, where={"store_id": "vspo"})

    assert [h.id for h in hits] == ["vspo:1"]


def test_delete_removes_ids(store):
    store.upsert("hololive:1", "a", [1.0, 0.0], {"store_id": "hololive"})
    store.delete(["hololive:1"])

    assert store.query([1.0, 0.0], n_results=5) == []


def test_delete_empty_list_is_noop(store):
    store.delete([])  # must not raise


def test_persistence_across_instances(tmp_path):
    path = str(tmp_path / "chroma")
    VectorStore(path).upsert("hololive:1", "a", [1.0, 0.0], {"store_id": "hololive"})

    hits = VectorStore(path).query([1.0, 0.0], n_results=5)

    assert [h.id for h in hits] == ["hololive:1"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_vector_store.py -v`
Expected: FAIL — `ModuleNotFoundError: estimator_king.vectorstore.store`.

- [ ] **Step 4: Implement `VectorStore`**

Create `estimator_king/vectorstore/store.py`:

```python
"""Embedded ChromaDB PersistentClient wrapper using precomputed embeddings."""

from dataclasses import dataclass
from typing import Any

import chromadb


@dataclass
class QueryHit:
    id: str
    document: str
    metadata: dict[str, Any]
    distance: float


class VectorStore:
    """One persistent ChromaDB collection 'products' with cosine distance.

    Embeddings are computed by EmbeddingProvider and passed in; this class does
    not configure a Chroma embedding function. The doc id is the product
    external_key, so upsert handles both create and update.
    """

    COLLECTION = "products"

    def __init__(self, path: str) -> None:
        self._client = chromadb.PersistentClient(path=path)
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )

    def upsert(
        self,
        id: str,
        document: str,
        embedding: list[float],
        metadata: dict[str, Any],
    ) -> None:
        self._collection.upsert(
            ids=[id],
            documents=[document],
            embeddings=[embedding],
            metadatas=[metadata],
        )

    def delete(self, ids: list[str]) -> None:
        if ids:
            self._collection.delete(ids=ids)

    def query(
        self,
        embedding: list[float],
        n_results: int,
        where: dict[str, Any] | None = None,
    ) -> list[QueryHit]:
        result = self._collection.query(
            query_embeddings=[embedding],
            n_results=n_results,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        return [
            QueryHit(
                id=ids[i],
                document=documents[i] or "",
                metadata=dict(metadatas[i] or {}),
                distance=float(distances[i]),
            )
            for i in range(len(ids))
        ]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_vector_store.py -v`
Expected: PASS (6 passed).

- [ ] **Step 6: Type-check, lint, commit**

```bash
basedpyright estimator_king/vectorstore/store.py
ruff check estimator_king/vectorstore/ tests/test_vector_store.py
git add estimator_king/vectorstore/ tests/test_vector_store.py
git commit -m "feat(vectorstore): add embedded ChromaDB VectorStore wrapper"
```

---

## Task 6: Greenfield schema + repository rework

**Files:**
- Modify: `estimator_king/database/schema.sql` (full rewrite)
- Modify: `estimator_king/database/repository.py`
- Test: `tests/test_repository.py` (adapt)

- [ ] **Step 1: Rewrite `schema.sql`**

Replace the entire contents of `estimator_king/database/schema.sql` with:

```sql
-- Estimator King state database (SQLite). Greenfield — created fresh, no migrations.
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS products (
    external_key   TEXT PRIMARY KEY,            -- "{store_id}:{product_id}" — also the ChromaDB id
    store_id       TEXT NOT NULL,
    product_id     TEXT NOT NULL,
    product_url    TEXT NOT NULL,

    content_hash   TEXT NOT NULL,
    normalizer_version INTEGER NOT NULL,

    last_seen_in_sitemap_at TEXT,
    last_fetch_success_at   TEXT,
    last_indexed_at         TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,

    consecutive_failures       INTEGER NOT NULL DEFAULT 0,
    consecutive_sitemap_misses INTEGER NOT NULL DEFAULT 0,

    inactive        INTEGER NOT NULL DEFAULT 0 CHECK (inactive IN (0,1)),
    inactive_reason TEXT,
    inactive_since  TEXT
);

CREATE INDEX IF NOT EXISTS idx_products_store_active_fetch
    ON products(store_id, inactive, last_fetch_success_at);

CREATE TABLE IF NOT EXISTS crawl_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id    TEXT NOT NULL,
    product_url TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE(store_id, product_url)
);
CREATE INDEX IF NOT EXISTS idx_crawl_queue_store_id ON crawl_queue(store_id, id);
```

- [ ] **Step 2: Write/adapt the failing tests** in `tests/test_repository.py`

Replace the file's product-state helper and remove the deleted-machinery cases. Set the helper and key new tests to:

```python
from datetime import datetime, timedelta, timezone

import pytest

from estimator_king.database.repository import ProductState, ProductStateRepository


@pytest.fixture
def repo():
    with ProductStateRepository(":memory:") as r:
        yield r


def _state(external_key, *, store_id, product_id, product_url="https://x/p",
           content_hash="h", last_fetch_success_at=None, last_indexed_at=None,
           inactive=False):
    return ProductState(
        external_key=external_key,
        store_id=store_id,
        product_id=product_id,
        product_url=product_url,
        content_hash=content_hash,
        normalizer_version=2,
        last_fetch_success_at=last_fetch_success_at,
        last_indexed_at=last_indexed_at,
        inactive=inactive,
    )


def test_upsert_roundtrip_new_columns(repo):
    repo.upsert(_state("hololive:1", store_id="hololive", product_id="1",
                        last_indexed_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    got = repo.get_by_external_key("hololive:1")
    assert got is not None
    assert got.store_id == "hololive"
    assert got.product_id == "1"
    assert got.last_indexed_at == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_upsert_coalesces_last_indexed_at_when_none(repo):
    repo.upsert(_state("hololive:1", store_id="hololive", product_id="1",
                        last_indexed_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    # second write omits last_indexed_at (None) -> must be preserved
    repo.upsert(_state("hololive:1", store_id="hololive", product_id="1",
                        content_hash="h2", last_indexed_at=None))
    got = repo.get_by_external_key("hololive:1")
    assert got is not None
    assert got.content_hash == "h2"
    assert got.last_indexed_at == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_get_oldest_active_products_orders_nulls_first_then_oldest(repo):
    now = datetime.now(tz=timezone.utc)
    repo.upsert(_state("s:newer", store_id="s", product_id="newer",
                       last_fetch_success_at=now))
    repo.upsert(_state("s:older", store_id="s", product_id="older",
                       last_fetch_success_at=now - timedelta(days=5)))
    repo.upsert(_state("s:never", store_id="s", product_id="never",
                       last_fetch_success_at=None))
    repo.upsert(_state("s:inactive", store_id="s", product_id="inactive",
                       last_fetch_success_at=None, inactive=True))

    keys = [p.external_key for p in repo.get_oldest_active_products("s", limit=10)]

    assert keys == ["s:never", "s:older", "s:newer"]  # inactive excluded, NULL first


def test_get_oldest_active_products_respects_limit(repo):
    repo.upsert(_state("s:a", store_id="s", product_id="a"))
    repo.upsert(_state("s:b", store_id="s", product_id="b"))
    assert len(repo.get_oldest_active_products("s", limit=1)) == 1


def test_list_active_filters_by_store_id_column(repo):
    repo.upsert(_state("hololive:1", store_id="hololive", product_id="1"))
    repo.upsert(_state("vspo:1", store_id="vspo", product_id="1"))
    keys = [p.external_key for p in repo.list_active("hololive")]
    assert keys == ["hololive:1"]
```

Remove from `tests/test_repository.py`: `test_state_db_schema_version_initialized`,
`test_state_db_schema_newer_than_supported_is_rejected`,
`test_state_db_upsert_updates_dify_document_id_when_provided`, the `get_stale_products` cases, the
`get_products_needing_fetch` cases, and any remaining `dify_document_id` references.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_repository.py -v`
Expected: FAIL — `ProductState.__init__` rejects `store_id`/`product_id`/`last_indexed_at`, and `get_oldest_active_products` does not exist.

- [ ] **Step 4: Rework `repository.py`**

Apply these changes to `estimator_king/database/repository.py`:

(a) `ProductState` dataclass — replace the field block so it reads (remove `dify_document_id`, add `store_id`, `product_id`, `last_indexed_at`):

```python
@dataclass(frozen=True)
class ProductState:
    external_key: str
    store_id: str
    product_id: str
    product_url: str
    content_hash: str
    normalizer_version: int
    last_seen_in_sitemap_at: datetime | None = None
    last_fetch_success_at: datetime | None = None
    last_indexed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    consecutive_failures: int = 0
    consecutive_sitemap_misses: int = 0
    inactive: bool = False
    inactive_reason: str | None = None
    inactive_since: datetime | None = None

    def with_updated_timestamps(self, *, created_at, updated_at):
        return replace(self, created_at=created_at, updated_at=updated_at)
```

Add `from dataclasses import dataclass, replace` at the top.

(b) Remove migration machinery: delete `_SCHEMA_VERSION`, `_migrate`, and rewrite `_ensure_schema` to:

```python
    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_read_schema_sql())
```

(c) Replace the **entire** `upsert` method body — keep the original `now`/`created_at`/`with_updated_timestamps` stamping preamble and the `refreshed` return postamble (do NOT delete them; they set the `NOT NULL` `created_at`/`updated_at` and return the persisted row), swapping in the new columns. `last_seen_in_sitemap_at`, `last_fetch_success_at`, `last_indexed_at`, and `product_url` are `COALESCE`d so a `None` never wipes a stored value; counters are written directly. The full method:

```python
    def upsert(self, state: ProductState) -> ProductState:
        now = _utc_now()
        existing = self.get_by_external_key(state.external_key)
        created_at = existing.created_at if existing and existing.created_at else now
        state = state.with_updated_timestamps(created_at=created_at, updated_at=now)
        _ = self.connection.execute(
            """
            INSERT INTO products (
                external_key, store_id, product_id, product_url,
                content_hash, normalizer_version,
                last_seen_in_sitemap_at, last_fetch_success_at, last_indexed_at,
                created_at, updated_at,
                consecutive_failures, consecutive_sitemap_misses,
                inactive, inactive_reason, inactive_since
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(external_key) DO UPDATE SET
                store_id=excluded.store_id,
                product_id=excluded.product_id,
                product_url=COALESCE(excluded.product_url, products.product_url),
                content_hash=excluded.content_hash,
                normalizer_version=excluded.normalizer_version,
                last_seen_in_sitemap_at=COALESCE(excluded.last_seen_in_sitemap_at, products.last_seen_in_sitemap_at),
                last_fetch_success_at=COALESCE(excluded.last_fetch_success_at, products.last_fetch_success_at),
                last_indexed_at=COALESCE(excluded.last_indexed_at, products.last_indexed_at),
                updated_at=excluded.updated_at,
                consecutive_failures=excluded.consecutive_failures,
                consecutive_sitemap_misses=excluded.consecutive_sitemap_misses,
                inactive=excluded.inactive,
                inactive_reason=excluded.inactive_reason,
                inactive_since=excluded.inactive_since
            """,
            (
                state.external_key, state.store_id, state.product_id, state.product_url,
                state.content_hash, state.normalizer_version,
                _dt_to_iso(state.last_seen_in_sitemap_at),
                _dt_to_iso(state.last_fetch_success_at),
                _dt_to_iso(state.last_indexed_at),
                _dt_to_iso(state.created_at), _dt_to_iso(state.updated_at),
                int(state.consecutive_failures), int(state.consecutive_sitemap_misses),
                1 if state.inactive else 0, state.inactive_reason,
                _dt_to_iso(state.inactive_since),
            ),
        )
        refreshed = self.get_by_external_key(state.external_key)
        if refreshed is None:
            raise RuntimeError("upsert failed to persist record")
        return refreshed
```

> Note on COALESCE semantics: `last_seen_in_sitemap_at` is COALESCE-preserved so a fetch write that
> carries the value forward (sync engine, Task 7) cannot wipe a fresh `record_sitemap_seen` value.

(d) Replace every `external_key LIKE ?` / `f"{store_id}:%"` filter with a `store_id = ?` filter.
`list_active`:

```python
    def list_active(self, store_id: str) -> list[ProductState]:
        rows = self.connection.execute(
            "SELECT * FROM products WHERE store_id = ? AND inactive = 0 ORDER BY external_key",
            (store_id,),
        ).fetchall()
        return [_row_to_state(cast(sqlite3.Row, r)) for r in rows]
```

`get_by_product_url`:

```python
    def get_by_product_url(self, store_id: str, product_url: str) -> ProductState | None:
        row = self.connection.execute(
            "SELECT * FROM products WHERE store_id = ? AND product_url = ?",
            (store_id, product_url),
        ).fetchone()
        if row is None:
            return None
        return _row_to_state(cast(sqlite3.Row, row))
```

(e) **Delete** `get_products_needing_fetch` and `get_stale_products`. **Add**:

```python
    def get_oldest_active_products(self, store_id: str, limit: int) -> list[ProductState]:
        if limit <= 0:
            return []
        rows = self.connection.execute(
            """
            SELECT * FROM products
            WHERE store_id = ? AND inactive = 0
            ORDER BY last_fetch_success_at ASC
            LIMIT ?
            """,
            (store_id, limit),
        ).fetchall()
        return [_row_to_state(cast(sqlite3.Row, r)) for r in rows]
```

(f) Rewrite `_row_to_state` to map the new columns (drop `dify_document_id`, add `store_id`,
`product_id`, `last_indexed_at`; `product_url` is now always present and `NOT NULL`):

```python
def _row_to_state(row: sqlite3.Row) -> ProductState:
    return ProductState(
        external_key=str(row["external_key"]),
        store_id=str(row["store_id"]),
        product_id=str(row["product_id"]),
        product_url=str(row["product_url"]),
        content_hash=str(row["content_hash"]),
        normalizer_version=int(cast(int, row["normalizer_version"])),
        last_seen_in_sitemap_at=_iso_to_dt(cast("str | None", row["last_seen_in_sitemap_at"])),
        last_fetch_success_at=_iso_to_dt(cast("str | None", row["last_fetch_success_at"])),
        last_indexed_at=_iso_to_dt(cast("str | None", row["last_indexed_at"])),
        created_at=_iso_to_dt(cast("str | None", row["created_at"])),
        updated_at=_iso_to_dt(cast("str | None", row["updated_at"])),
        consecutive_failures=int(cast(int, row["consecutive_failures"])),
        consecutive_sitemap_misses=int(cast(int, row["consecutive_sitemap_misses"])),
        inactive=bool(int(cast(int, row["inactive"]))),
        inactive_reason=cast("str | None", row["inactive_reason"]),
        inactive_since=_iso_to_dt(cast("str | None", row["inactive_since"])),
    )
```

Keep `_apply_pragmas`, `_utc_now`, `_dt_to_iso`, `_iso_to_dt`, the crawl_queue helpers, and
`check_same_thread=False` unchanged.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_repository.py -v`
Expected: PASS (all repository tests green).

- [ ] **Step 6: Type-check, lint, commit**

```bash
basedpyright estimator_king/database/repository.py
ruff check estimator_king/database/ tests/test_repository.py
git add estimator_king/database/schema.sql estimator_king/database/repository.py tests/test_repository.py
git commit -m "refactor(db): greenfield schema + store_id queries, drop dify_document_id & migrations"
```

---

## Task 7: Sync engine — single-writer embed + upsert

**Files:**
- Modify: `estimator_king/sync/engine.py`
- Test: `tests/test_sync_engine.py` (rewrite)

- [ ] **Step 1: Rewrite the failing tests** in `tests/test_sync_engine.py`

Replace the whole file with:

```python
from datetime import datetime, timezone

import pytest

from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.database.repository import ProductState, ProductStateRepository
from estimator_king.sync.engine import SyncResult, _format_product_document, sync_products


class FakeEmbedder:
    def __init__(self):
        self.calls = []

    def embed_documents(self, texts):
        self.calls.append(list(texts))
        return [[0.1, 0.2] for _ in texts]


class FakeVectorStore:
    def __init__(self):
        self.upserts = []
        self.deletes = []

    def upsert(self, id, document, embedding, metadata):
        self.upserts.append((id, document, embedding, metadata))

    def delete(self, ids):
        self.deletes.append(list(ids))


@pytest.fixture
def repo():
    with ProductStateRepository(":memory:") as r:
        yield r


def _snapshot(pid=1):
    return ProductSnapshot(
        product_id=pid, title="Voice Pack", description="desc",
        variants=[ProductVariant(1, "Standard", "2000", "SKU")],
        html_details={"Features": "five tracks"},
    )


def test_format_product_document_includes_title_and_price_metadata():
    name, text, meta = _format_product_document(_snapshot(), "hololive", "https://x/p/1")
    assert name.startswith("hololive:1 - ")
    assert "Voice Pack" in text
    assert meta["store_id"] == "hololive"
    assert meta["product_id"] == "1"
    assert meta["title"] == "Voice Pack"
    assert meta["price_jpy"] == 2000


def test_create_embeds_upserts_and_persists_state(repo):
    emb, vs = FakeEmbedder(), FakeVectorStore()
    result = sync_products([_snapshot()], "hololive", "https://x", repo, emb, vs)

    assert result.created == 1
    assert vs.upserts[0][0] == "hololive:1"
    state = repo.get_by_external_key("hololive:1")
    assert state is not None
    assert state.last_indexed_at is not None
    assert state.last_fetch_success_at is not None
    assert state.consecutive_failures == 0


def test_unchanged_content_skips_reindex_but_stamps_fetch(repo):
    emb, vs = FakeEmbedder(), FakeVectorStore()
    sync_products([_snapshot()], "hololive", "https://x", repo, emb, vs)
    before = repo.get_by_external_key("hololive:1")

    emb2, vs2 = FakeEmbedder(), FakeVectorStore()
    result = sync_products([_snapshot()], "hololive", "https://x", repo, emb2, vs2)

    assert result.skipped == 1
    assert vs2.upserts == []  # not re-indexed
    after = repo.get_by_external_key("hololive:1")
    assert after.last_indexed_at == before.last_indexed_at  # preserved


def test_changed_content_updates_and_reindexes(repo):
    emb, vs = FakeEmbedder(), FakeVectorStore()
    sync_products([_snapshot()], "hololive", "https://x", repo, emb, vs)

    changed = ProductSnapshot(
        product_id=1, title="Voice Pack v2", description="new",
        variants=[ProductVariant(1, "Standard", "2500", "SKU")], html_details={},
    )
    emb2, vs2 = FakeEmbedder(), FakeVectorStore()
    result = sync_products([changed], "hololive", "https://x", repo, emb2, vs2)

    assert result.updated == 1
    assert vs2.upserts[0][0] == "hololive:1"


def test_carries_sitemap_state_forward(repo):
    emb, vs = FakeEmbedder(), FakeVectorStore()
    sync_products([_snapshot()], "hololive", "https://x", repo, emb, vs)
    repo.record_sitemap_seen("hololive:1")
    seen_before = repo.get_by_external_key("hololive:1").last_seen_in_sitemap_at
    assert seen_before is not None

    changed = ProductSnapshot(product_id=1, title="T2", description="d",
                              variants=[ProductVariant(1, "S", "2000")], html_details={})
    sync_products([changed], "hololive", "https://x", repo, FakeEmbedder(), FakeVectorStore())

    after = repo.get_by_external_key("hololive:1")
    assert after.last_seen_in_sitemap_at == seen_before  # not wiped


def test_embedding_error_counts_failed_and_does_not_advance_index(repo):
    class Boom:
        def embed_documents(self, texts):
            raise RuntimeError("embed down")

    result = sync_products([_snapshot()], "hololive", "https://x", repo, Boom(), FakeVectorStore())

    assert result.failed == 1
    state = repo.get_by_external_key("hololive:1")
    assert state is not None
    assert state.last_indexed_at is None
    assert state.last_fetch_success_at is not None  # fetch succeeded
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sync_engine.py -v`
Expected: FAIL — old `sync_products(dify_client=...)` signature / `_poll_indexing_status` import errors.

- [ ] **Step 3: Rewrite `estimator_king/sync/engine.py`**

Replace the whole file with:

```python
"""Sync engine: format products, embed, and upsert into the vector store.

sync_products is the single writer of product rows on the success path.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Protocol

from estimator_king.crawler.snapshot import (
    NORMALIZER_VERSION,
    ProductSnapshot,
    compute_content_hash,
)
from estimator_king.database.repository import ProductState, ProductStateRepository


class _Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


class _VectorStore(Protocol):
    def upsert(self, id: str, document: str, embedding: list[float],
               metadata: dict[str, object]) -> None: ...


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    failed_ids: list[str] = field(default_factory=list)


def _min_variant_price(snapshot: ProductSnapshot) -> int:
    prices: list[int] = []
    for variant in snapshot.variants:
        try:
            prices.append(int(float(variant.price)))
        except (TypeError, ValueError):
            continue
    return min(prices) if prices else 0


def _format_product_document(
    snapshot: ProductSnapshot, store_id: str, product_url: str
) -> tuple[str, str, dict[str, object]]:
    document_name = f"{store_id}:{snapshot.product_id} - {snapshot.title}"
    parts: list[str] = [f"# {snapshot.title}", ""]
    if snapshot.description.strip():
        parts.extend([snapshot.description, ""])
    if snapshot.variants:
        parts.extend(["## Variants", "", "| Title | Price |", "|-------|-------|"])
        for variant in snapshot.variants:
            parts.append(f"| {variant.title} | {variant.price} |")
        parts.append("")
    for section_name, section_content in snapshot.html_details.items():
        if section_content.strip():
            parts.extend([f"## {section_name}", "", section_content, ""])
    text_content = "\n".join(parts).rstrip()

    metadata: dict[str, object] = {
        "store_id": store_id,
        "product_id": str(snapshot.product_id),
        "product_url": product_url,
        "content_hash": compute_content_hash(snapshot),
        "title": snapshot.title,
        "price_jpy": _min_variant_price(snapshot),
    }
    return document_name, text_content, metadata


def sync_products(
    snapshots: Iterable[ProductSnapshot],
    store_id: str,
    base_url: str,
    repository: ProductStateRepository,
    embedder: _Embedder,
    vector_store: _VectorStore,
) -> SyncResult:
    result = SyncResult()
    for snapshot in snapshots:
        now = datetime.now(tz=timezone.utc)
        external_key = f"{store_id}:{snapshot.product_id}"
        product_url = f"{base_url}/products/{snapshot.product_id}"
        content_hash = compute_content_hash(snapshot)
        state = repository.get_by_external_key(external_key)

        # Sitemap-tracking fields carried forward so the fetch never clobbers them.
        seen_at = state.last_seen_in_sitemap_at if state else now
        sitemap_misses = state.consecutive_sitemap_misses if state else 0

        unchanged = (
            state is not None
            and state.content_hash == content_hash
            and state.last_indexed_at is not None
        )

        last_indexed_at = state.last_indexed_at if state else None
        try:
            if unchanged:
                result.skipped += 1
            else:
                _name, text, metadata = _format_product_document(
                    snapshot, store_id, product_url
                )
                embedding = embedder.embed_documents([text])[0]
                vector_store.upsert(external_key, text, embedding, metadata)
                last_indexed_at = now
                if state is None:
                    result.created += 1
                else:
                    result.updated += 1
        except Exception as exc:  # embedding/vector failure: fire-and-forget
            logging.exception("Sync failed for %s", external_key)
            result.failed += 1
            result.failed_ids.append(external_key)
            # last_indexed_at stays at the previous value (not advanced)

        repository.upsert(
            ProductState(
                external_key=external_key,
                store_id=store_id,
                product_id=str(snapshot.product_id),
                product_url=product_url,
                content_hash=content_hash,
                normalizer_version=NORMALIZER_VERSION,
                last_seen_in_sitemap_at=seen_at,
                last_fetch_success_at=now,
                last_indexed_at=last_indexed_at,
                consecutive_failures=0,
                consecutive_sitemap_misses=sitemap_misses,
            )
        )
    return result
```

> Note: on the failure path `content_hash` is still written as the (new) snapshot hash but
> `last_indexed_at` is not advanced, so on the next run `state.last_indexed_at is None` (or stale)
> keeps `unchanged` False and the product is retried. On a brand-new product that fails, the row is
> created with `last_indexed_at = None`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sync_engine.py -v`
Expected: PASS (all sync-engine tests green).

- [ ] **Step 5: Type-check, lint, commit**

```bash
basedpyright estimator_king/sync/engine.py
ruff check estimator_king/sync/engine.py tests/test_sync_engine.py
git add estimator_king/sync/engine.py tests/test_sync_engine.py
git commit -m "refactor(sync): embed+upsert single-writer engine, drop Dify client"
```

---

## Task 8: Pipeline — oldest-products budget enqueue

**Files:**
- Modify: `estimator_king/crawler/pipeline.py`
- Test: `tests/test_pipeline.py` (rewrite)

- [ ] **Step 1: Rewrite the failing tests** in `tests/test_pipeline.py`

Replace the whole file with:

```python
from datetime import datetime, timedelta, timezone

import pytest

from estimator_king.config_schema import Store
from estimator_king.crawler.pipeline import enqueue_oldest_products, populate_queue_from_sitemap
from estimator_king.database.repository import ProductState, ProductStateRepository


@pytest.fixture
def repo():
    with ProductStateRepository(":memory:") as r:
        yield r


def _store():
    return Store(id="hololive", base_url="https://x", sitemap_url="https://x/sitemap.xml")


def _state(pid, fetched):
    return ProductState(
        external_key=f"hololive:{pid}", store_id="hololive", product_id=str(pid),
        product_url=f"https://x/products/{pid}", content_hash="h", normalizer_version=2,
        last_fetch_success_at=fetched,
    )


def test_enqueue_oldest_products_picks_oldest_within_limit(repo):
    now = datetime.now(tz=timezone.utc)
    repo.upsert(_state(1, now))
    repo.upsert(_state(2, now - timedelta(days=3)))
    repo.upsert(_state(3, None))

    enqueued = enqueue_oldest_products(_store(), repo, limit=2)

    assert enqueued == 2
    queued = {e["product_url"] for e in repo.peek_all("hololive")}
    assert queued == {"https://x/products/3", "https://x/products/2"}  # NULL + oldest


def test_enqueue_oldest_products_limit_zero_is_noop(repo):
    repo.upsert(_state(1, None))
    assert enqueue_oldest_products(_store(), repo, limit=0) == 0
    assert repo.peek_all("hololive") == []


class FakeEnumerator:
    def __init__(self, urls):
        self._urls = urls

    def enumerate_products(self, base_url):
        return self._urls


def test_populate_enqueues_only_new_urls(repo):
    repo.upsert(_state(1, None))  # existing
    enum = FakeEnumerator(["https://x/products/1", "https://x/products/2"])

    new_count = populate_queue_from_sitemap(_store(), repo, enum)

    assert new_count == 1
    assert [e["product_url"] for e in repo.peek_all("hololive")] == ["https://x/products/2"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_pipeline.py -v`
Expected: FAIL — `enqueue_oldest_products` does not exist.

- [ ] **Step 3: Edit `estimator_king/crawler/pipeline.py`**

(a) Keep `populate_queue_from_sitemap` exactly as-is.

(b) **Delete** `enqueue_stale_products` and **delete** `process_queue` (the sync Dify path is removed; the async pipeline is the only path). Remove the now-unused `DifyKBClient` import in the `TYPE_CHECKING` block.

(c) Add:

```python
def enqueue_oldest_products(store: Store, repo: ProductStateRepository, *, limit: int) -> int:
    """Enqueue up to `limit` existing active products, oldest last_fetch first."""
    if limit <= 0:
        return 0
    enqueued = 0
    for state in repo.get_oldest_active_products(store.id, limit):
        if repo.enqueue_url(store.id, state.product_url):
            enqueued += 1
    return enqueued
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_pipeline.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Type-check, lint, commit**

```bash
basedpyright estimator_king/crawler/pipeline.py
ruff check estimator_king/crawler/pipeline.py tests/test_pipeline.py
git add estimator_king/crawler/pipeline.py tests/test_pipeline.py
git commit -m "refactor(crawler): budget-based enqueue_oldest_products, drop stale/sync-process paths"
```

---

## Task 9: Async pipeline — VectorStore + failure bookkeeping

**Files:**
- Modify: `estimator_king/crawler/async_pipeline.py`
- Test: `tests/test_async_pipeline.py` (rewrite)

- [ ] **Step 1: Rewrite the failing tests** in `tests/test_async_pipeline.py`

Replace the whole file with:

```python
import asyncio
from unittest.mock import patch

import pytest

from estimator_king.config_schema import CrawlerPolicy
from estimator_king.crawler.async_pipeline import async_process_queue
from estimator_king.crawler.shopify import ShopifyHTTPError
from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.database.repository import ProductStateRepository


class FakeEmbedder:
    def embed_documents(self, texts):
        return [[0.1, 0.2] for _ in texts]


class FakeVectorStore:
    def __init__(self):
        self.upserts = []

    def upsert(self, id, document, embedding, metadata):
        self.upserts.append(id)

    def delete(self, ids):
        pass


@pytest.fixture
def repo():
    with ProductStateRepository(":memory:") as r:
        yield r


def _snap(pid):
    return ProductSnapshot(product_id=pid, title=f"T{pid}", description="d",
                           variants=[ProductVariant(1, "S", "2000")], html_details={})


def test_success_indexes_and_clears_queue(repo):
    repo.enqueue_url("hololive", "https://x/products/1")
    vs = FakeVectorStore()
    policy = CrawlerPolicy()

    with patch("estimator_king.crawler.async_pipeline.fetch_product", return_value=_snap(1)):
        result = asyncio.run(async_process_queue(
            "hololive", "https://x", policy, repo, FakeEmbedder(), vs))

    assert result.processed == 1
    assert vs.upserts == ["hololive:1"]
    assert repo.peek_all("hololive") == []  # queue drained
    state = repo.get_by_external_key("hololive:1")
    assert state is not None and state.last_fetch_success_at is not None


def test_fetch_failure_increments_failures_and_keeps_queue(repo):
    # Pre-existing product row so the failure can be recorded against it.
    repo.enqueue_url("hololive", "https://x/products/1")
    with patch("estimator_king.crawler.async_pipeline.fetch_product", return_value=_snap(1)):
        asyncio.run(async_process_queue("hololive", "https://x", CrawlerPolicy(), repo,
                                        FakeEmbedder(), FakeVectorStore()))
    repo.enqueue_url("hololive", "https://x/products/1")  # re-queue for the failing run

    def boom(url, client):
        raise ShopifyHTTPError(url, status_code=500)

    with patch("estimator_king.crawler.async_pipeline.fetch_product", side_effect=boom):
        result = asyncio.run(async_process_queue("hololive", "https://x", CrawlerPolicy(), repo,
                                                 FakeEmbedder(), FakeVectorStore()))

    assert result.failed == 1
    assert repo.peek_all("hololive") != []  # entry kept for retry
    assert repo.get_by_external_key("hololive:1").consecutive_failures == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_async_pipeline.py -v`
Expected: FAIL — `async_process_queue` still expects `normalizer`/`dify_client`.

- [ ] **Step 3: Rewrite `estimator_king/crawler/async_pipeline.py`**

Replace the whole file with:

```python
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, cast

from estimator_king.crawler.async_http_client import AsyncHTTPClient
from estimator_king.crawler.shopify import fetch_product
from estimator_king.sync.engine import sync_products

if TYPE_CHECKING:
    from estimator_king.config_schema import CrawlerPolicy
    from estimator_king.database.repository import ProductStateRepository
    from estimator_king.llm.embeddings import EmbeddingProvider
    from estimator_king.vectorstore.store import VectorStore

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    created: int = 0
    updated: int = 0
    sync_skipped: int = 0


class _AsyncToSyncHTTPAdapter:
    def __init__(self, client: AsyncHTTPClient, loop: asyncio.AbstractEventLoop):
        self._client = client
        self._loop = loop

    def get(self, url: str):
        text = asyncio.run_coroutine_threadsafe(self._client.get(url), self._loop).result()
        return type("_Resp", (), {"status_code": 200, "text": text})()


async def async_process_queue(
    store_id: str,
    store_base_url: str,
    policy: CrawlerPolicy,
    state_repo: ProductStateRepository,
    embedder: EmbeddingProvider,
    vector_store: VectorStore,
) -> PipelineResult:
    entries = state_repo.peek_all(store_id)
    if not entries:
        return PipelineResult()

    loop = asyncio.get_running_loop()
    result = PipelineResult()
    lock = asyncio.Lock()

    async with AsyncHTTPClient(policy) as client:
        adapter = _AsyncToSyncHTTPAdapter(client, loop)
        fetch_with_adapter = cast(Callable[[str, Any], Any], fetch_product)

        async def _handle(entry: dict[str, int | str]) -> None:
            entry_id = int(entry["id"])
            product_url = str(entry["product_url"])
            try:
                snapshot = await asyncio.to_thread(fetch_with_adapter, product_url, adapter)
                sync_result = await asyncio.to_thread(
                    sync_products, [snapshot], store_id, store_base_url,
                    state_repo, embedder, vector_store,
                )
                state_repo.delete_queue_entry(entry_id)
                async with lock:
                    result.created += sync_result.created
                    result.updated += sync_result.updated
                    result.sync_skipped += sync_result.skipped
                    result.processed += 1
            except Exception:
                logger.exception("Error processing %s (url=%s)", entry_id, product_url)
                existing = state_repo.get_by_product_url(store_id, product_url)
                if existing is not None:
                    state_repo.increment_consecutive_failures(existing.external_key)
                async with lock:
                    result.failed += 1
                # queue entry intentionally kept for retry

        sem = asyncio.Semaphore(policy.concurrency_per_domain)

        async def _bounded(entry: dict[str, int | str]) -> None:
            async with sem:
                await _handle(entry)

        await asyncio.gather(*[_bounded(entry) for entry in entries])

    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_async_pipeline.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Type-check, lint, commit**

```bash
basedpyright estimator_king/crawler/async_pipeline.py
ruff check estimator_king/crawler/async_pipeline.py tests/test_async_pipeline.py
git add estimator_king/crawler/async_pipeline.py tests/test_async_pipeline.py
git commit -m "refactor(crawler): async pipeline uses VectorStore + embedder, records fetch failures"
```

---

## Task 10: Inactive products delete vectors

**Files:**
- Modify: `estimator_king/sync/inactive.py`
- Test: `tests/test_inactive.py` (adapt)

- [ ] **Step 1: Adapt the failing tests** in `tests/test_inactive.py`

The existing `tests/test_inactive.py` `_state(...)` helper builds a `ProductState` with the old
fields (`dify_document_id`, no `store_id`/`product_id`/`product_url`), which Task 6 removed/made
required. **Migrate the helper**, add a `FakeVectorStore`, pass it to every
`mark_inactive_products(...)` call, and add a concrete deletion test. Replace the helper and add the
test as:

```python
from datetime import datetime

from estimator_king.database.repository import ProductState


def _state(
    external_key,
    *,
    content_hash="a" * 64,
    normalizer_version=2,
    consecutive_failures=0,
    consecutive_sitemap_misses=0,
    inactive=False,
    inactive_reason=None,
    inactive_since=None,
):
    store_id, _, product_id = external_key.partition(":")
    return ProductState(
        external_key=external_key,
        store_id=store_id,
        product_id=product_id,
        product_url=f"https://x/products/{product_id}",
        content_hash=content_hash,
        normalizer_version=normalizer_version,
        consecutive_failures=consecutive_failures,
        consecutive_sitemap_misses=consecutive_sitemap_misses,
        inactive=inactive,
        inactive_reason=inactive_reason,
        inactive_since=inactive_since,
    )


class FakeVectorStore:
    def __init__(self):
        self.deleted = []

    def delete(self, ids):
        self.deleted.append(list(ids))


def test_marks_inactive_and_deletes_vectors(repo):  # `repo` = existing fixture
    repo.upsert(_state("hololive:1", consecutive_failures=3))
    vs = FakeVectorStore()

    result = mark_inactive_products(repo, vs, failure_threshold=3, miss_threshold=4)

    assert result.marked_inactive == 1
    assert vs.deleted == [["hololive:1"]]
```

Update every existing `mark_inactive_products(repo, ...)` call in the file to
`mark_inactive_products(repo, FakeVectorStore(), ...)`, and update any other call sites in the file
that build a `ProductState` directly to use the migrated `_state(...)` helper (no
`dify_document_id`; `store_id`/`product_id`/`product_url` populated).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_inactive.py -v`
Expected: FAIL — `mark_inactive_products()` takes no `vector_store` argument.

- [ ] **Step 3: Edit `estimator_king/sync/inactive.py`**

Add a `_VectorStore` Protocol and a `vector_store` parameter; collect deactivated keys and delete:

```python
from typing import Protocol


class _VectorStore(Protocol):
    def delete(self, ids: list[str]) -> None: ...
```

Change the signature to:

```python
def mark_inactive_products(
    repository: ProductStateRepository,
    vector_store: _VectorStore,
    failure_threshold: int = 3,
    miss_threshold: int = 4,
) -> InactiveResult:
```

Inside the loop, after each `repository.upsert(updated_state)` that marks a product inactive,
append `product.external_key` to a local `deactivated: list[str] = []`. After the loop (before the
`already_inactive` count block), add:

```python
    if deactivated:
        vector_store.delete(deactivated)
```

When constructing `updated_state`, drop the removed `dify_document_id` argument and add
`store_id=product.store_id, product_id=product.product_id, product_url=product.product_url,
last_indexed_at=product.last_indexed_at` so the rebuilt `ProductState` is valid under the new
schema.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_inactive.py -v`
Expected: PASS.

- [ ] **Step 5: Type-check, lint, commit**

```bash
basedpyright estimator_king/sync/inactive.py
ruff check estimator_king/sync/inactive.py tests/test_inactive.py
git add estimator_king/sync/inactive.py tests/test_inactive.py
git commit -m "feat(sync): delete vectors for products marked inactive"
```

---

## Task 11: Estimator (retrieve → prompt → estimate)

**Files:**
- Create: `estimator_king/bot/estimator.py`
- Test: `tests/test_estimator.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_estimator.py`:

```python
from estimator_king.bot.estimator import Estimator
from estimator_king.llm.chat import EstimateBatch, PriceRange, ProductEstimate, ReferenceProduct
from estimator_king.vectorstore.store import QueryHit


class FakeEmbedder:
    def embed_query(self, text):
        return [0.1, 0.2]


class FakeVectorStore:
    def __init__(self, hits):
        self._hits = hits
        self.queries = []

    def query(self, embedding, n_results, where=None):
        self.queries.append((embedding, n_results, where))
        return self._hits


class FakeChat:
    def __init__(self):
        self.calls = []

    def estimate(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        return EstimateBatch(estimates=[
            ProductEstimate(
                product_name="p", suggested_price_jpy=2000,
                price_range_jpy=PriceRange(min=1800, max=2200),
                confidence="high", rationale="r", reference_products=[],
            )
        ])


def _hit():
    return QueryHit(id="hololive:1", document="doc",
                    metadata={"title": "ref", "price_jpy": 2000, "store_id": "hololive"},
                    distance=0.1)


def test_estimate_products_queries_and_calls_chat():
    vs = FakeVectorStore([_hit()])
    chat = FakeChat()
    est = Estimator(FakeEmbedder(), chat, vs, top_k=5)

    batch = est.estimate_products(["voice pack"], "discord-1")

    assert len(batch.estimates) == 1
    assert vs.queries[0][1] == 5  # n_results == top_k
    assert "ref" in chat.calls[0][1]  # retrieved reference text in the user prompt


def test_chunking_aggregates_across_calls():
    vs = FakeVectorStore([_hit()])
    chat = FakeChat()
    est = Estimator(FakeEmbedder(), chat, vs)
    est.CHUNK_SIZE = 1  # force two chunks

    batch = est.estimate_products(["a", "b"], "discord-1")

    assert len(chat.calls) == 2
    assert len(batch.estimates) == 2


def test_empty_input_returns_empty_batch():
    est = Estimator(FakeEmbedder(), FakeChat(), FakeVectorStore([]))
    assert est.estimate_products([], "discord-1").estimates == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_estimator.py -v`
Expected: FAIL — `ModuleNotFoundError: estimator_king.bot.estimator`.

- [ ] **Step 3: Implement `Estimator`**

Create `estimator_king/bot/estimator.py`:

```python
"""Price estimation pipeline: retrieve references from the vector store and ask
the chat model for structured estimates (replaces the Dify workflow)."""

import logging
from typing import Protocol

from estimator_king.llm.chat import EstimateBatch

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are the Estimator King, a price estimation assistant for Japanese "
    "merchandise (hololive / vspo goods). For each product line in the user "
    "message, find the closest matches in the provided reference context and "
    "produce a price estimate. Confidence: 'high' = direct/very close match, "
    "'medium' = similar product types, 'low' = no strong match. Include up to 3 "
    "reference_products drawn from the context. Prices are integer JPY. Return "
    "estimates for every product line, in order."
)


class _Embedder(Protocol):
    def embed_query(self, text: str) -> list[float]: ...


class _Chat(Protocol):
    def estimate(self, system_prompt: str, user_prompt: str) -> EstimateBatch: ...


class _Hit(Protocol):
    metadata: dict[str, object]


class _VectorStore(Protocol):
    def query(self, embedding: list[float], n_results: int,
              where: dict[str, object] | None = None) -> list[_Hit]: ...


class Estimator:
    CHUNK_SIZE = 10

    def __init__(self, embedder: _Embedder, chat: _Chat, vector_store: _VectorStore,
                 *, top_k: int = 10) -> None:
        self._embedder = embedder
        self._chat = chat
        self._vector_store = vector_store
        self._top_k = top_k

    def estimate_products(self, product_names: list[str], user_id: str) -> EstimateBatch:
        if not product_names:
            return EstimateBatch(estimates=[])
        logger.info("estimate request from %s for %d products", user_id, len(product_names))
        all_estimates = []
        for start in range(0, len(product_names), self.CHUNK_SIZE):
            chunk = product_names[start : start + self.CHUNK_SIZE]
            batch = self._estimate_chunk(chunk)
            all_estimates.extend(batch.estimates)
        return EstimateBatch(estimates=all_estimates)

    def _estimate_chunk(self, chunk: list[str]) -> EstimateBatch:
        context_blocks: list[str] = []
        for name in chunk:
            embedding = self._embedder.embed_query(name)
            hits = self._vector_store.query(embedding, self._top_k)
            refs = "\n".join(
                f"- {h.metadata.get('title')} | ¥{h.metadata.get('price_jpy')} "
                f"| {h.metadata.get('store_id')}"
                for h in hits
            )
            context_blocks.append(f"### Query: {name}\n{refs or '(no matches)'}")
        user_prompt = (
            "Products to estimate (one per line):\n"
            + "\n".join(chunk)
            + "\n\nReference context:\n"
            + "\n\n".join(context_blocks)
        )
        return self._chat.estimate(SYSTEM_PROMPT, user_prompt)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_estimator.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Type-check, lint, commit**

```bash
basedpyright estimator_king/bot/estimator.py
ruff check estimator_king/bot/estimator.py tests/test_estimator.py
git add estimator_king/bot/estimator.py tests/test_estimator.py
git commit -m "feat(bot): add Estimator (retrieve + chat) replacing Dify workflow"
```

---

## Task 12: Bot commands wired to Estimator

**Files:**
- Modify: `estimator_king/bot/commands.py`
- Test: `tests/test_bot_commands.py` (adapt)

- [ ] **Step 1: Adapt the failing tests** in `tests/test_bot_commands.py`

Replace the rendering tests to build an `EstimateBatch` and call the new `format_estimates`:

```python
import discord

from estimator_king.bot.commands import format_estimates, parse_product_lines
from estimator_king.llm.chat import EstimateBatch, PriceRange, ProductEstimate, ReferenceProduct


def _batch(n=1):
    return EstimateBatch(estimates=[
        ProductEstimate(
            product_name=f"p{i}", suggested_price_jpy=2000,
            price_range_jpy=PriceRange(min=1800, max=2200), confidence="high",
            rationale="r", reference_products=[ReferenceProduct(name="ref", price_jpy=2000, store="hololive")],
        ) for i in range(n)
    ])


def test_parse_product_lines_strips_and_filters():
    assert parse_product_lines(" a \n\n b \n") == ["a", "b"]


def test_format_estimates_returns_embeds_with_prices():
    embeds = format_estimates(_batch(1))
    assert embeds and isinstance(embeds[0], discord.Embed)
    assert "¥2,000" in embeds[0].description


def test_format_estimates_empty_batch():
    embeds = format_estimates(EstimateBatch(estimates=[]))
    assert len(embeds) == 1
    assert "0 products" in embeds[0].title
```

Remove any tests referencing `WorkflowClient` / `WorkflowResult` / `format_workflow_result`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_bot_commands.py -v`
Expected: FAIL — `format_estimates` does not exist / `WorkflowClient` import gone.

- [ ] **Step 3: Edit `estimator_king/bot/commands.py`**

(a) Replace the import `from estimator_king.bot.workflow_client import WorkflowClient, WorkflowResult`
with `from estimator_king.bot.estimator import Estimator` and
`from estimator_king.llm.chat import EstimateBatch, EstimationError`.

(b) Rename `format_workflow_result(result: WorkflowResult, ...)` to
`format_estimates(batch: EstimateBatch, max_length: int = 2000)`; iterate `batch.estimates`
instead of `result.estimates`; drop the `workflow_run_id` / `elapsed_time` footer logic. Empty case
title stays `"Price Estimates (0 products)"`. The per-product block formatting (price, range,
confidence, rationale, references) is unchanged.

(c) `ProductInputModal.on_submit`: build the estimator from `self._config` instead of `WorkflowClient`:

```python
        try:
            embedder = EmbeddingProvider(self._config.build_provider_config())
            chat = ChatProvider(self._config.build_provider_config())
            store = VectorStore(self._config.chroma_path)
            estimator = Estimator(embedder, chat, store)
            user_id = f"discord-{interaction.user.id}"
            batch = estimator.estimate_products(product_list, user_id)
            for embed in format_estimates(batch):
                await interaction.followup.send(embed=embed)
        except EstimationError as e:
            await interaction.followup.send(f"❌ Estimation failed: {e}")
        except Exception as e:
            await interaction.followup.send(f"❌ Unexpected error: {e}")
```

Add the imports `from estimator_king.llm.embeddings import EmbeddingProvider`,
`from estimator_king.llm.chat import ChatProvider`, and
`from estimator_king.vectorstore.store import VectorStore`. Remove the
`dify_workflow_api_key` config checks and the `requests.Timeout`/`requests.HTTPError` branches and
the `import requests`.

> The shared singletons built in `bot/__main__.py` (Task 17) will later be injected; for now the
> modal constructs them from config. (Task 17 refines this to pass a shared `Estimator` via the
> modal constructor and removes this per-request construction — see that task.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_bot_commands.py -v`
Expected: PASS.

- [ ] **Step 5: Type-check, lint, commit**

```bash
basedpyright estimator_king/bot/commands.py
ruff check estimator_king/bot/commands.py tests/test_bot_commands.py
git add estimator_king/bot/commands.py tests/test_bot_commands.py
git commit -m "feat(bot): wire /estimate to local Estimator, render EstimateBatch"
```

---

## Task 13: Config — drop Dify/interval, add provider + budget

**Files:**
- Modify: `estimator_king/config_schema.py`
- Test: `tests/test_config.py` (adapt)

- [ ] **Step 1: Adapt the failing tests** in `tests/test_config.py`

Replace Dify/interval assertions with provider + budget ones:

```python
import os
from unittest.mock import patch

from estimator_king.config_schema import AppConfig, CrawlerPolicy, Store, load_config


def test_crawler_policy_budget_defaults():
    p = CrawlerPolicy()
    assert p.max_products_per_run == 32
    assert p.crawl_schedule_hours == 24.0


def test_store_has_no_fetch_interval():
    s = Store(id="a", base_url="b", sitemap_url="c")
    assert not hasattr(s, "fetch_interval_hours")


def _write_yaml(tmp_path):
    """Minimal valid stores config (config.validate() requires >=1 store)."""
    path = tmp_path / "stores.yaml"
    path.write_text(
        "stores:\n"
        "  - id: hololive\n"
        "    base_url: https://x\n"
        "    sitemap_url: https://x/sitemap.xml\n",
        encoding="utf-8",
    )
    return str(path)


@patch.dict(os.environ, {
    "OPENAI_API_KEY": "sk-x", "EMBEDDING_MODEL": "bge-m3",
    "EMBEDDING_DIMENSIONS": "", "CHAT_MODEL": "gpt-4o", "CHROMA_PATH": "/data/chroma",
}, clear=False)
def test_build_provider_config_from_env(tmp_path):
    cfg = load_config(_write_yaml(tmp_path))  # exercises the env → AppConfig path

    pc = cfg.build_provider_config()
    assert pc.embedding_api_key == "sk-x"
    assert pc.chat_api_key == "sk-x"          # falls back to OPENAI_API_KEY
    assert pc.embedding_model == "bge-m3"
    assert pc.embedding_dimensions is None    # EMBEDDING_DIMENSIONS="" → None via _opt_int
    assert pc.chat_model == "gpt-4o"
    assert cfg.chroma_path == "/data/chroma"  # chroma_path lives on AppConfig, not ProviderConfig
```

Remove tests referencing `dify_api_key`, `dify_base_url`, `dify_dataset_id`,
`dify_workflow_api_key`, `fetch_interval_hours`, `default_fetch_interval_hours`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — new fields / `build_provider_config` absent.

- [ ] **Step 3: Edit `estimator_king/config_schema.py`**

(a) `Store`: remove the `fetch_interval_hours` field and its validation line. `load_config` stops
reading it (drop the `s.get("fetch_interval_hours", ...)` arg).

(b) `CrawlerPolicy`: remove `default_fetch_interval_hours` (field + validation + the `load_config`
read). Add fields `max_products_per_run: int = 32` and `crawl_schedule_hours: float = 24.0`; in
`validate()` add `> 0` checks for both; in `load_config` read them via
`crawler_data.get("max_products_per_run", 32)` and `crawler_data.get("crawl_schedule_hours", 24.0)`.

(c) `AppConfig`: remove the five `dify_*` fields. Add:

```python
    # Providers / vector store
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_base_url: str | None = None
    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int | None = 1024
    embedding_query_prefix: str = ""
    embedding_doc_prefix: str = ""
    chat_api_key: str | None = None
    chat_base_url: str | None = None
    chat_model: str = "gpt-4o"
    chat_structured_output: bool = True
    chroma_path: str = "./chroma"
```

Add the builder:

```python
    def build_provider_config(self) -> "ProviderConfig":
        from estimator_king.llm.config import ProviderConfig
        emb_key = self.embedding_api_key or self.openai_api_key or ""
        chat_key = self.chat_api_key or self.openai_api_key or ""
        return ProviderConfig(
            embedding_api_key=emb_key,
            chat_api_key=chat_key,
            embedding_base_url=self.embedding_base_url or self.openai_base_url,
            embedding_model=self.embedding_model,
            embedding_dimensions=self.embedding_dimensions,
            embedding_query_prefix=self.embedding_query_prefix,
            embedding_doc_prefix=self.embedding_doc_prefix,
            chat_base_url=self.chat_base_url or self.openai_base_url,
            chat_model=self.chat_model,
            chat_structured_output=self.chat_structured_output,
        )
```

(d) In `load_config`, replace the `DIFY_*` env reads with:

```python
    def _opt_int(name: str, default: int | None) -> int | None:
        raw = os.getenv(name)
        if raw is None:
            return default
        return int(raw) if raw.strip() != "" else None

    openai_api_key = os.getenv("OPENAI_API_KEY")
    openai_base_url = os.getenv("OPENAI_BASE_URL")
    config = AppConfig(
        stores=stores, crawler=crawler, proxy=proxy,
        openai_api_key=openai_api_key,
        openai_base_url=openai_base_url,
        embedding_api_key=os.getenv("EMBEDDING_API_KEY"),
        embedding_base_url=os.getenv("EMBEDDING_BASE_URL"),
        embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-large"),
        embedding_dimensions=_opt_int("EMBEDDING_DIMENSIONS", 1024),
        embedding_query_prefix=os.getenv("EMBEDDING_QUERY_PREFIX", ""),
        embedding_doc_prefix=os.getenv("EMBEDDING_DOC_PREFIX", ""),
        chat_api_key=os.getenv("CHAT_API_KEY"),
        chat_base_url=os.getenv("CHAT_BASE_URL"),
        chat_model=os.getenv("CHAT_MODEL", "gpt-4o"),
        chat_structured_output=os.getenv("CHAT_STRUCTURED_OUTPUT", "true").lower() != "false",
        chroma_path=os.getenv("CHROMA_PATH", "./chroma"),
        discord_token=os.getenv("DISCORD_TOKEN", os.getenv("DISCORD_BOT_TOKEN")),
        database_path=os.getenv("DATABASE_PATH", "./estimator_king.db"),
    )
```

Add `from typing import TYPE_CHECKING` and, under it, `from estimator_king.llm.config import ProviderConfig` for the return type hint.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Type-check, lint, commit**

```bash
basedpyright estimator_king/config_schema.py
ruff check estimator_king/config_schema.py tests/test_config.py
git add estimator_king/config_schema.py tests/test_config.py
git commit -m "feat(config): drop Dify/interval settings, add provider + crawl budget config"
```

---

## Task 14: Crawl cycle (shared by CLI + scheduler)

**Files:**
- Create: `estimator_king/crawler/cycle.py`
- Test: `tests/test_crawl_cycle.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_crawl_cycle.py`:

```python
import asyncio
from unittest.mock import patch

import pytest

from estimator_king.config_schema import AppConfig, CrawlerPolicy, Store
from estimator_king.crawler.cycle import run_crawl_cycle
from estimator_king.database.repository import ProductStateRepository


class FakeEmbedder:
    def embed_documents(self, texts):
        return [[0.0] for _ in texts]


class FakeVectorStore:
    def upsert(self, *a, **k):
        pass

    def delete(self, ids):
        pass


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "state.db")


def _config():
    return AppConfig(
        stores=[Store(id="hololive", base_url="https://x", sitemap_url="https://x/sm.xml")],
        crawler=CrawlerPolicy(max_products_per_run=32),
    )


def test_run_cycle_invokes_inactive_once_after_stores(db_path):
    cfg = _config()
    with patch("estimator_king.crawler.cycle.populate_queue_from_sitemap", return_value=0), \
         patch("estimator_king.crawler.cycle.enqueue_oldest_products", return_value=0) as enq, \
         patch("estimator_king.crawler.cycle.async_process_queue") as proc, \
         patch("estimator_king.crawler.cycle.mark_inactive_products") as inactive:
        async def fake_proc(*a, **k):
            from estimator_king.crawler.async_pipeline import PipelineResult
            return PipelineResult()
        proc.side_effect = fake_proc

        counters = asyncio.run(run_crawl_cycle(cfg, db_path, FakeEmbedder(), FakeVectorStore()))

    assert inactive.call_count == 1  # once per cycle, cross-store
    assert enq.call_args.kwargs["limit"] == 32  # budget = 32 - new_count(0)
    assert "errors" in counters


def test_force_refetch_skips_budget_enqueue(db_path):
    cfg = _config()
    with patch("estimator_king.crawler.cycle.populate_queue_from_sitemap", return_value=0), \
         patch("estimator_king.crawler.cycle.enqueue_oldest_products") as enq, \
         patch("estimator_king.crawler.cycle.async_process_queue") as proc, \
         patch("estimator_king.crawler.cycle.mark_inactive_products"):
        async def fake_proc(*a, **k):
            from estimator_king.crawler.async_pipeline import PipelineResult
            return PipelineResult()
        proc.side_effect = fake_proc

        asyncio.run(run_crawl_cycle(cfg, db_path, FakeEmbedder(), FakeVectorStore(), force_refetch=True))

    enq.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_crawl_cycle.py -v`
Expected: FAIL — `estimator_king.crawler.cycle` does not exist.

- [ ] **Step 3: Implement `run_crawl_cycle`**

Create `estimator_king/crawler/cycle.py`:

```python
"""One full crawl cycle: per-store sitemap + budget enqueue + drain, then a
single cross-store inactive sweep. Shared by the CLI and the bot scheduler."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from estimator_king.crawler.async_pipeline import async_process_queue
from estimator_king.crawler.http_client import HTTPClient
from estimator_king.crawler.pipeline import enqueue_oldest_products, populate_queue_from_sitemap
from estimator_king.crawler.sitemap import SitemapEnumerator
from estimator_king.database.repository import ProductStateRepository
from estimator_king.sync.inactive import mark_inactive_products

if TYPE_CHECKING:
    from estimator_king.config_schema import AppConfig
    from estimator_king.llm.embeddings import EmbeddingProvider
    from estimator_king.vectorstore.store import VectorStore

logger = logging.getLogger(__name__)


async def run_crawl_cycle(
    config: AppConfig,
    db_path: str,
    embedder: EmbeddingProvider,
    vector_store: VectorStore,
    *,
    force_refetch: bool = False,
) -> dict[str, int]:
    counters = {"discovered": 0, "fetched_ok": 0, "created": 0, "updated": 0,
                "skipped": 0, "inactive": 0, "errors": 0}

    with ProductStateRepository(db_path) as repo:
        http_client = HTTPClient(crawler_policy=config.crawler, proxy=config.proxy)
        enumerator = SitemapEnumerator(http_client=http_client)

        for store in config.stores:
            logger.info("Processing store %s", store.id)
            try:
                new_count = populate_queue_from_sitemap(store, repo, enumerator)
                counters["discovered"] += new_count
            except Exception:
                logger.exception("Sitemap failed for %s", store.id)
                counters["errors"] += 1
                continue

            if force_refetch:
                for state in repo.list_active(store.id):
                    repo.enqueue_url(store.id, state.product_url)
            else:
                remaining = max(0, config.crawler.max_products_per_run - new_count)
                enqueue_oldest_products(store, repo, limit=remaining)

            try:
                result = await async_process_queue(
                    store.id, store.base_url, config.crawler, repo, embedder, vector_store)
                counters["fetched_ok"] += result.processed
                counters["created"] += result.created
                counters["updated"] += result.updated
                counters["skipped"] += result.sync_skipped
                counters["errors"] += result.failed
            except Exception:
                logger.exception("Queue processing failed for %s", store.id)
                counters["errors"] += 1

        try:
            inactive_result = mark_inactive_products(
                repo, vector_store,
                failure_threshold=config.crawler.inactive_failure_threshold,
                miss_threshold=config.crawler.inactive_sitemap_miss_threshold,
            )
            counters["inactive"] += inactive_result.marked_inactive
        except Exception:
            logger.exception("Inactive sweep failed")
            counters["errors"] += 1

    return counters
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_crawl_cycle.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Type-check, lint, commit**

```bash
basedpyright estimator_king/crawler/cycle.py
ruff check estimator_king/crawler/cycle.py tests/test_crawl_cycle.py
git add estimator_king/crawler/cycle.py tests/test_crawl_cycle.py
git commit -m "feat(crawler): add run_crawl_cycle shared by CLI and scheduler"
```

---

## Task 15: Scheduler

**Files:**
- Create: `estimator_king/bot/scheduler.py`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_scheduler.py`:

```python
import asyncio

import pytest

from estimator_king.bot.scheduler import CrawlScheduler


@pytest.mark.asyncio
async def test_run_once_calls_cycle(monkeypatch):
    calls = []

    async def fake_cycle(config, db_path, embedder, vector_store, *, force_refetch=False):
        calls.append(db_path)
        return {"errors": 0}

    monkeypatch.setattr("estimator_king.bot.scheduler.run_crawl_cycle", fake_cycle)
    sched = CrawlScheduler(config=object(), db_path="db", embedder=object(), vector_store=object())

    await sched.run_once()

    assert calls == ["db"]


@pytest.mark.asyncio
async def test_run_once_is_reentrancy_guarded(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    count = 0

    async def fake_cycle(*a, **k):
        nonlocal count
        count += 1
        started.set()
        await release.wait()
        return {"errors": 0}

    monkeypatch.setattr("estimator_king.bot.scheduler.run_crawl_cycle", fake_cycle)
    sched = CrawlScheduler(config=object(), db_path="db", embedder=object(), vector_store=object())

    first = asyncio.create_task(sched.run_once())
    await started.wait()
    await sched.run_once()  # should be skipped (already running)
    release.set()
    await first

    assert count == 1


@pytest.mark.asyncio
async def test_run_once_swallows_cycle_errors(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("cycle failed")

    monkeypatch.setattr("estimator_king.bot.scheduler.run_crawl_cycle", boom)
    sched = CrawlScheduler(config=object(), db_path="db", embedder=object(), vector_store=object())

    await sched.run_once()  # must not raise
```

Note: `pytest-asyncio` is installed; ensure `pytest.ini` enables asyncio mode (Task already covered
by existing async tests — if `asyncio_mode` is not `auto`, add `@pytest.mark.asyncio` as shown).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_scheduler.py -v`
Expected: FAIL — `estimator_king.bot.scheduler` does not exist.

- [ ] **Step 3: Implement `CrawlScheduler`**

Create `estimator_king/bot/scheduler.py`:

```python
"""In-process daily crawl scheduler. No external dependency — a guarded asyncio loop."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from estimator_king.crawler.cycle import run_crawl_cycle

if TYPE_CHECKING:
    from estimator_king.config_schema import AppConfig
    from estimator_king.llm.embeddings import EmbeddingProvider
    from estimator_king.vectorstore.store import VectorStore

logger = logging.getLogger(__name__)


class CrawlScheduler:
    def __init__(self, config: AppConfig, db_path: str,
                 embedder: EmbeddingProvider, vector_store: VectorStore) -> None:
        self._config = config
        self._db_path = db_path
        self._embedder = embedder
        self._vector_store = vector_store
        self._running = False

    async def run_once(self) -> None:
        if self._running:
            logger.info("Crawl cycle already running — skipping this trigger")
            return
        self._running = True
        try:
            counters = await run_crawl_cycle(
                self._config, self._db_path, self._embedder, self._vector_store)
            logger.info("Crawl cycle complete: %s", counters)
        except Exception:
            logger.exception("Crawl cycle raised")
        finally:
            self._running = False

    async def run_forever(self, *, run_on_start: bool = True) -> None:
        interval = self._config.crawler.crawl_schedule_hours * 3600.0
        if run_on_start:
            await self.run_once()
        while True:
            await asyncio.sleep(interval)
            await self.run_once()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_scheduler.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Type-check, lint, commit**

```bash
basedpyright estimator_king/bot/scheduler.py
ruff check estimator_king/bot/scheduler.py tests/test_scheduler.py
git add estimator_king/bot/scheduler.py tests/test_scheduler.py
git commit -m "feat(bot): add in-process daily CrawlScheduler with re-entrancy guard"
```

---

## Task 16: CLI entrypoint (`__main__.py`)

**Files:**
- Modify: `estimator_king/__main__.py`
- Test: `tests/test_cli.py` and `tests/test_main_async.py` (adapt)

- [ ] **Step 1: Adapt the failing tests**

In `tests/test_cli.py`, remove all `--dify-*` argument assertions; assert the parser still accepts
`--config`, `--db`, `--log-level`, `--force-refetch`. In `tests/test_main_async.py`, replace
`DifyKBClient` construction expectations with: `main()` builds `EmbeddingProvider` + `VectorStore`
and calls `run_crawl_cycle`. Minimal new test for `test_cli.py`:

```python
from estimator_king.__main__ import parse_args


def test_parse_args_has_no_dify_flags():
    args = parse_args(["--config", "c.yaml", "--force-refetch"])
    assert args.config == "c.yaml"
    assert args.force_refetch is True
    assert not hasattr(args, "dify_api_key")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py tests/test_main_async.py -v`
Expected: FAIL — `--dify-*` still present / Dify client still built.

- [ ] **Step 3: Rewrite `estimator_king/__main__.py`**

Replace its contents so the CLI runs exactly one crawl cycle:

```python
"""CLI entrypoint: run one crawl cycle (sitemap → fetch → embed → upsert)."""

import argparse
import asyncio
import json
import logging
import sys
from typing import Optional, Sequence

from estimator_king.config_schema import AppConfig
from estimator_king.crawler.cycle import run_crawl_cycle
from estimator_king.llm.embeddings import EmbeddingProvider
from estimator_king.vectorstore.store import VectorStore


def parse_args(args: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="estimator_king",
        description="Estimator King crawler — sync products to the local vector store",
    )
    parser.add_argument("--config", default="stores_config.yaml")
    parser.add_argument("--db", default=None)
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    parser.add_argument("--force-refetch", action="store_true", default=False)
    return parser.parse_args(args)


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s - %(levelname)s - %(message)s", stream=sys.stderr)
    try:
        config = AppConfig.from_yaml(args.config)
    except Exception as e:
        logging.error("Failed to load config from %s: %s", args.config, e)
        sys.exit(1)

    if args.db is not None:
        config.database_path = args.db
    provider_config = config.build_provider_config()
    if not provider_config.embedding_api_key:
        logging.error("OPENAI_API_KEY (or EMBEDDING_API_KEY) is required")
        sys.exit(2)

    embedder = EmbeddingProvider(provider_config)
    vector_store = VectorStore(config.chroma_path)
    try:
        counters = asyncio.run(
            run_crawl_cycle(config, config.database_path, embedder, vector_store,
                            force_refetch=args.force_refetch))
    except Exception as e:
        logging.error("Crawler failed: %s", e)
        sys.exit(1)

    print(json.dumps(counters, indent=2))
    sys.exit(0)


def _main() -> None:
    main()


if __name__ == "__main__":
    _main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli.py tests/test_main_async.py -v`
Expected: PASS.

- [ ] **Step 5: Type-check, lint, commit**

```bash
basedpyright estimator_king/__main__.py
ruff check estimator_king/__main__.py tests/test_cli.py tests/test_main_async.py
git add estimator_king/__main__.py tests/test_cli.py tests/test_main_async.py
git commit -m "refactor(cli): run one crawl cycle with VectorStore + providers, drop Dify args"
```

---

## Task 17: Bot entrypoint — shared singletons + scheduler

**Files:**
- Modify: `estimator_king/bot/__main__.py`
- Modify: `estimator_king/bot/commands.py` (inject shared instances)
- Test: `tests/test_bot_commands.py` (adjust modal construction)

- [ ] **Step 1: Inject the shared `Estimator` into `commands.py` (explicit edits)**

Make these exact changes to `estimator_king/bot/commands.py` (replacing the per-request provider
construction added in Task 12):

(a) `ProductInputModal.__init__`: change the signature from `def __init__(self, config: AppConfig)`
to `def __init__(self, estimator: Estimator)` and store `self._estimator = estimator` (drop
`self._config`).

(b) `ProductInputModal.on_submit`: replace the provider-building `try` block (from Task 12) with the
injected estimator and remove the now-dead imports:

```python
        try:
            user_id = f"discord-{interaction.user.id}"
            batch = self._estimator.estimate_products(product_list, user_id)
            for embed in format_estimates(batch):
                await interaction.followup.send(embed=embed)
        except EstimationError as e:
            await interaction.followup.send(f"❌ Estimation failed: {e}")
        except Exception as e:
            await interaction.followup.send(f"❌ Unexpected error: {e}")
```

Remove the now-unused imports `from estimator_king.llm.embeddings import EmbeddingProvider`,
`from estimator_king.llm.chat import ChatProvider`, and
`from estimator_king.vectorstore.store import VectorStore` (added in Task 12). Keep
`from estimator_king.bot.estimator import Estimator` and
`from estimator_king.llm.chat import EstimateBatch, EstimationError`.

(c) `setup_commands`: change the signature from `setup_commands(bot, config)` to
`setup_commands(bot: discord.Client, config: AppConfig, estimator: Estimator)`, and change the
`estimate` command callback body from `await interaction.response.send_modal(ProductInputModal(config))`
to `await interaction.response.send_modal(ProductInputModal(estimator))` (the closure now captures
`estimator`). `config` is retained in the signature for parity but no longer used to build providers.

Add a test:

```python
def test_modal_uses_injected_estimator():
    from estimator_king.bot.commands import ProductInputModal

    class FakeEstimator:
        def estimate_products(self, names, user_id):
            from estimator_king.llm.chat import EstimateBatch
            return EstimateBatch(estimates=[])

    modal = ProductInputModal(FakeEstimator())
    assert modal._estimator is not None
```

- [ ] **Step 2: Run the command tests to verify they fail**

Run: `pytest tests/test_bot_commands.py -v`
Expected: FAIL — `setup_commands`/`ProductInputModal` signatures changed.

- [ ] **Step 3: Edit `estimator_king/bot/__main__.py`**

In `main()`, after validating `config.discord_token`, build shared singletons and start the
scheduler as a background task before `bot.start`:

```python
    from estimator_king.llm.embeddings import EmbeddingProvider
    from estimator_king.llm.chat import ChatProvider
    from estimator_king.vectorstore.store import VectorStore
    from estimator_king.bot.estimator import Estimator
    from estimator_king.bot.scheduler import CrawlScheduler

    provider_config = config.build_provider_config()
    if not provider_config.embedding_api_key:
        sys.stderr.write("Error: OPENAI_API_KEY (or EMBEDDING_API_KEY) is required\n")
        sys.exit(1)

    embedder = EmbeddingProvider(provider_config)
    chat = ChatProvider(provider_config)
    vector_store = VectorStore(config.chroma_path)
    estimator = Estimator(embedder, chat, vector_store)

    bot = create_bot()
    tree = setup_commands(bot, config, estimator)

    scheduler = CrawlScheduler(config, config.database_path, embedder, vector_store)
    asyncio.create_task(scheduler.run_forever())
```

Remove the old `tree = setup_commands(bot, config)` line and the `DIFY_WORKFLOW_API_KEY` handling.
(The `commands.py` `on_submit`/modal/`setup_commands` edits — dropping the per-request provider
construction in favor of the injected `Estimator` — are done in Step 1 above.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_bot_commands.py -v`
Expected: PASS.

- [ ] **Step 5: Type-check, lint, commit**

```bash
basedpyright estimator_king/bot/__main__.py estimator_king/bot/commands.py
ruff check estimator_king/bot/__main__.py estimator_king/bot/commands.py tests/test_bot_commands.py
git add estimator_king/bot/__main__.py estimator_king/bot/commands.py tests/test_bot_commands.py
git commit -m "feat(bot): build shared providers/vector store and start crawl scheduler"
```

---

## Task 18: Delete Dify code and obsolete tests

**Files:**
- Delete: `estimator_king/sync/dify_client.py`, `estimator_king/sync/async_dify.py`,
  `estimator_king/bot/workflow_client.py`
- Delete tests: `tests/test_dify_client.py`, `tests/test_async_dify_wrapper.py`,
  `tests/test_poll_indexing_status.py`, `tests/test_bot_workflow_client.py`,
  `tests/test_sync_products_docid.py`, `tests/test_sync_fire_and_forget.py`, `tests/test_migration.py`

- [ ] **Step 1: Confirm nothing still imports the deleted modules**

Run: `grep -rn "dify_client\|async_dify\|workflow_client\|DifyKBClient\|_poll_indexing_status\|dify_document_id" estimator_king/`
Expected: no matches (all references removed in Tasks 6–17).

- [ ] **Step 2: Delete the modules and tests**

```bash
git rm estimator_king/sync/dify_client.py estimator_king/sync/async_dify.py estimator_king/bot/workflow_client.py
git rm tests/test_dify_client.py tests/test_async_dify_wrapper.py tests/test_poll_indexing_status.py \
       tests/test_bot_workflow_client.py tests/test_sync_products_docid.py \
       tests/test_sync_fire_and_forget.py tests/test_migration.py
```

- [ ] **Step 3: Update `sync/__init__.py` docstring**

Set `estimator_king/sync/__init__.py` to:

```python
"""Product → vector-store synchronization."""
```

- [ ] **Step 4: Run the full suite**

Run: `pytest -q`
Expected: PASS — no collection errors, no references to deleted modules.

- [ ] **Step 5: Type-check, lint, commit**

```bash
basedpyright estimator_king/
ruff check estimator_king/ tests/
git add estimator_king/sync/__init__.py
git commit -m "chore: delete Dify client/workflow modules and obsolete tests"
```

(The module/test deletions are already staged by the `git rm` calls in Step 2; only the
`sync/__init__.py` docstring edit remains to stage. Do **not** use `git add -u`/`-A`.)

---

## Task 19: Update integration/e2e tests

**Files:**
- Modify: `tests/test_integration_async_pipeline.py`, `tests/test_e2e_mocked.py`

- [ ] **Step 1: Rework the integration test**

In `tests/test_integration_async_pipeline.py`, replace Dify mocks with a `FakeEmbedder` +
`FakeVectorStore` (as in Task 9) and a temp SQLite DB; drive `run_crawl_cycle` (Task 14) end to end
with `populate_queue_from_sitemap` patched to enqueue 1–2 fake URLs and `fetch_product` patched to
return `ProductSnapshot`s; assert products land in the DB with `last_indexed_at` set and the fake
vector store received upserts.

- [ ] **Step 2: Rework the e2e-mocked test**

In `tests/test_e2e_mocked.py`, replace the Dify workflow mock with a `FakeEmbedder`, a real
`VectorStore` in a `tmp_path`, and a `FakeChat` returning a fixed `EstimateBatch`; assert
`Estimator.estimate_products([...])` returns estimates and that `format_estimates` renders embeds.

- [ ] **Step 3: Run the reworked tests**

Run: `pytest tests/test_integration_async_pipeline.py tests/test_e2e_mocked.py -v`
Expected: PASS.

- [ ] **Step 4: Type-check, lint, commit**

```bash
ruff check tests/test_integration_async_pipeline.py tests/test_e2e_mocked.py
git add tests/test_integration_async_pipeline.py tests/test_e2e_mocked.py
git commit -m "test: rework integration + e2e tests for ChromaDB/provider pipeline"
```

---

## Task 20: Deployment manifests

**Files:**
- Delete: `deploy/crawler-cronjob.yaml`, `dify-deploy/` (whole directory)
- Modify: `deploy/kustomization.yaml`, `deploy/bot-deployment.yaml`, `deploy/configmap.yaml`,
  `deploy/secrets.yaml`, `deploy/crawler-pvc.yaml`

- [ ] **Step 1: Delete the CronJob and the Dify stack**

```bash
git rm deploy/crawler-cronjob.yaml
git rm -r dify-deploy
```

- [ ] **Step 2: Edit `deploy/kustomization.yaml`**

Remove `crawler-cronjob.yaml` from `resources`. Final `resources` list:

```yaml
resources:
  - configmap.yaml
  - secrets.yaml
  - crawler-pvc.yaml
  - bot-deployment.yaml
```

- [ ] **Step 3: Edit `deploy/bot-deployment.yaml`**

Mount the PVC and set the new env. Under the `bot` container add `volumeMounts` + pod `volumes`,
set `DATABASE_PATH=/data/estimator_king.db` and `CHROMA_PATH=/data/chroma`, inject
`OPENAI_API_KEY` from the secret, and bump resources. The container/spec becomes:

```yaml
      containers:
        - name: bot
          image: estimator-king-bot:latest
          command:
            - python
            - -m
            - estimator_king.bot
            - --token
            - $(DISCORD_TOKEN)
          envFrom:
            - configMapRef:
                name: estimator-king-config
          env:
            - name: DISCORD_TOKEN
              valueFrom:
                secretKeyRef:
                  name: estimator-king-secrets
                  key: DISCORD_TOKEN
            - name: OPENAI_API_KEY
              valueFrom:
                secretKeyRef:
                  name: estimator-king-secrets
                  key: OPENAI_API_KEY
            - name: DATABASE_PATH
              value: "/data/estimator_king.db"
            - name: CHROMA_PATH
              value: "/data/chroma"
          volumeMounts:
            - name: state-storage
              mountPath: /data
            - name: stores-config
              mountPath: /config
              readOnly: true
          resources:
            requests:
              memory: "512Mi"
              cpu: "250m"
            limits:
              memory: "1Gi"
              cpu: "500m"
      volumes:
        - name: state-storage
          persistentVolumeClaim:
            claimName: estimator-king-state-pvc
        - name: stores-config
          configMap:
            name: estimator-king-stores-config
```

Also add `--config /config/stores_config.yaml` to the `command` list (after `estimator_king.bot`).

- [ ] **Step 4: Edit `deploy/configmap.yaml` and `deploy/secrets.yaml`**

In `configmap.yaml` drop `DIFY_BASE_URL`; add (only those that differ from defaults — e.g. when
using ollama): `OPENAI_BASE_URL`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`, `CHAT_MODEL`,
`CHROMA_PATH: "/data/chroma"`. In `secrets.yaml` remove `DIFY_API_KEY`, `DIFY_DATASET_ID`,
`DIFY_WORKFLOW_API_KEY`; add `OPENAI_API_KEY`.

- [ ] **Step 5: Edit `deploy/crawler-pvc.yaml`**

Relabel the PVC for bot ownership (it is now mounted by the bot): change the
`app.kubernetes.io/name` label value to `estimator-king-bot`. Keep `ReadWriteOnce` and `5Gi`.

- [ ] **Step 6: Validate kustomize build**

Run: `kubectl kustomize deploy/`
Expected: renders without error; output contains the bot Deployment (with the PVC mount and
`OPENAI_API_KEY`), the PVC, configmap, secret — and no CronJob.

- [ ] **Step 7: Commit**

```bash
git add deploy/
git commit -m "chore(deploy): remove CronJob + dify-deploy, bot mounts PVC with OpenAI/Chroma env"
```

(The `dify-deploy/` and `deploy/crawler-cronjob.yaml` deletions are already staged by the `git rm`
calls in Step 1; only the `deploy/` edits remain. Do **not** use `git add -A`.)

---

## Task 21: Docs, env example, and final validation

**Files:**
- Delete: `docs/dify-dataset-setup.md`, `docs/dify-workflow-contract.md`,
  `dify_python_sdk_research_report.md`, `estimator-dify-plan.md`, `dify/` (if present)
- Modify: `README.md`, `.env.example`, `docs/local-runbook.md`, `docs/ops-runbook.md`

- [ ] **Step 1: Delete Dify docs and stray dir**

```bash
git rm docs/dify-dataset-setup.md docs/dify-workflow-contract.md dify_python_sdk_research_report.md estimator-dify-plan.md
# `dify/` at repo root is an untracked nested dir (not tracked by this repo) — use rm, not git rm:
[ -d dify ] && rm -rf dify || true
```

- [ ] **Step 2: Rewrite `.env.example`**

Replace its contents with:

```bash
# Estimator King environment variables.
# Copy to .env and fill in:  cp .env.example .env

# ── Provider (OpenAI default; point *_BASE_URL at ollama's /v1 to swap) ──
OPENAI_API_KEY=
# OPENAI_BASE_URL=https://api.openai.com/v1
# EMBEDDING_MODEL=text-embedding-3-large
# EMBEDDING_DIMENSIONS=1024
# CHAT_MODEL=gpt-4o
# CHAT_STRUCTURED_OUTPUT=true

# ── Storage ──
DATABASE_PATH=./estimator_king.db
CHROMA_PATH=./chroma

# ── Discord Bot ──
DISCORD_BOT_TOKEN=
```

- [ ] **Step 3: Update `README.md`**

Replace the architecture/config/deployment sections: single bot process owning SQLite + ChromaDB;
provider env vars; the daily per-store budget crawl; remove the Dify env list and the multi-service
description. Note the embedding model recommendation (default `text-embedding-3-large@1024`; local
Japanese swap `bge-m3` via ollama) and the re-index requirement when changing the embedding model.

- [ ] **Step 4: Update the runbooks**

In `docs/local-runbook.md` and `docs/ops-runbook.md`, replace all Dify setup/credentials steps with
provider + Chroma configuration, the daily budget behavior, the manual one-cycle backfill
(`python -m estimator_king --force-refetch`), and the re-index procedure (delete `chroma/` and
re-crawl after changing the embedding model/dimensions).

- [ ] **Step 5: Full validation**

Run: `basedpyright estimator_king/`
Expected: 0 errors.

Run: `ruff check estimator_king/ tests/`
Expected: no violations.

Run: `pytest -q`
Expected: all tests pass; no references to Dify remain.

Run: `grep -rn "dify\|Dify\|DIFY" estimator_king/ deploy/ README.md .env.example docs/ | grep -v "dify-deploy"`
Expected: no matches (all Dify references removed).

- [ ] **Step 6: Commit**

```bash
git add README.md .env.example docs/local-runbook.md docs/ops-runbook.md
git commit -m "docs: remove Dify docs, document provider/Chroma setup and re-index procedure"
```

(The Dify doc deletions are already staged by the `git rm` in Step 1; the untracked `dify/` removal
needs no staging. Do **not** use `git add -A`.)

---

## Notes for the implementer

- **Re-index on embedding change:** vectors from different embedding models/dimensions are
  incompatible. Changing `EMBEDDING_MODEL`/`EMBEDDING_DIMENSIONS` requires deleting `chroma/` and
  re-crawling (`python -m estimator_king --force-refetch`).
- **`pytest-asyncio`:** if `pytest.ini` does not set `asyncio_mode = auto`, the async tests use the
  explicit `@pytest.mark.asyncio` marker shown in Task 15. Check `pytest.ini` and keep markers
  consistent with the existing async tests.
- **Single-replica bot:** the bot owns the RWO PVC; do not scale `replicas` above 1.
- **Provider swap to ollama:** set `OPENAI_BASE_URL=http://<host>:11434/v1`,
  `CHAT_STRUCTURED_OUTPUT=false`, `EMBEDDING_MODEL=bge-m3`, `EMBEDDING_DIMENSIONS` unset.
