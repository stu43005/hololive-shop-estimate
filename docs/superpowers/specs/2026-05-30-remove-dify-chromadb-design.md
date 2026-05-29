# Design Spec: Remove Dify → Embedded ChromaDB + OpenAI-compatible providers

- **Date**: 2026-05-30
- **Status**: Draft for review
- **Scope**: Replace all Dify dependencies (Knowledge Base + Workflow) with an in-process
  ChromaDB vector store and a provider abstraction over OpenAI-compatible embedding and
  chat APIs. Merge the standalone crawler into the bot process. Redesign the SQLite schema
  greenfield. Rework the crawl scheduler to a daily, per-store budgeted run.

---

## 1. Background

Estimator King estimates prices for Japanese merchandise (hololive / vspo Shopify stores)
and serves estimates through a Discord bot. Today it depends on a self-hosted Dify instance
for two distinct jobs:

1. **Knowledge Base (vector store + embeddings)** — the crawler formats each product into a
   markdown document and pushes it to Dify's KB (`estimator_king/sync/dify_client.py`,
   `engine.py`, `async_dify.py`). Dify handles chunking, embedding, indexing, and storage.
   The server-assigned document id is stored in SQLite (`products.dify_document_id`).
2. **Workflow engine (RAG + LLM)** — the bot calls a Dify *workflow*
   (`estimator_king/bot/workflow_client.py`) that does *Knowledge Retrieval → LLM →
   Code (JSON parse) → End* and returns structured price estimates.

Dify is operationally heavy: the `dify-deploy/` stack runs postgres, redis, weaviate, an API
server, workers, a sandbox, an SSRF proxy, nginx, and a plugin daemon. This spec removes Dify
entirely and replaces it with lightweight in-process components.

### Deployment topology change

| | Today | After |
| --- | --- | --- |
| Crawler | weekly `CronJob`, mounts RWO PVC, pushes to Dify KB | **removed** — folded into the bot as an in-process scheduled task |
| Bot | always-on `Deployment`, stateless, calls Dify workflow API | always-on `Deployment`, **owns** SQLite + ChromaDB on the RWO PVC, runs estimation locally |
| Dify stack | full `dify-deploy/` kustomization | **deleted** |

Because the bot becomes the single owner of the PVC, ChromaDB's embedded `PersistentClient`
(single-process) is safe — there is no concurrent writer/reader across pods.

---

## 2. Goals & non-goals

### Goals
- Remove every Dify import, client, doc, deploy manifest, and env var from the project.
- Store product vectors in an **embedded ChromaDB** (`PersistentClient`) on the existing PVC.
- Abstract embeddings + chat behind a provider layer that defaults to **OpenAI** and swaps to
  **ollama / any OpenAI-compatible endpoint** via configuration (`base_url` + `api_key`).
- Re-implement the price-estimation pipeline (retrieve → prompt → LLM → structured output) in
  Python, preserving the existing `ProductEstimate` output shape and Discord rendering.
- Merge the crawler into the bot process with a **daily** scheduler and a **per-store fetch
  budget** that always fetches new products and updates the oldest existing products up to the
  budget.
- Redesign the SQLite schema greenfield (no migration, no legacy columns/machinery).
- Recommend an embedding model with strong Japanese support.

### Non-goals
- No data migration from the existing Dify KB or SQLite DB — the first crawl repopulates
  everything from scratch (vectors are re-derivable by re-crawling).
- No change to the Shopify crawling, sitemap enumeration, snapshotting, or content-hashing
  logic beyond what is needed to swap Dify for ChromaDB.
- No change to the Discord embed rendering format users see.
- No multi-replica bot support (single replica owns the PVC; out of scope).

---

## 3. Architecture overview

A single process (the bot) owns all state and logic:

```
┌───────────────────────── estimator-king (single process) ─────────────────────────┐
│                                                                                    │
│  Discord bot (discord.py, async)                                                   │
│    /estimate ─▶ Estimator.estimate_products()                                      │
│                   │  for each product line (chunked):                              │
│                   │    EmbeddingProvider.embed(query)                              │
│                   │    VectorStore.query(embedding, n_results, where) ─▶ refs      │
│                   │    ChatProvider.estimate(query, refs) ─▶ ProductEstimate       │
│                   ▼                                                                 │
│                 Discord embeds (existing format_workflow_result rendering)         │
│                                                                                    │
│  Scheduler (in-process asyncio loop, daily)                                        │
│    for each store:                                                                 │
│      populate_queue_from_sitemap()  → new products always enqueued (new_count)     │
│      enqueue_oldest_products(limit = max_products_per_run − new_count)             │
│      async_process_queue()  → fetch → EmbeddingProvider.embed → VectorStore.upsert │
│    after all stores (once per cycle):                                              │
│      mark_inactive_products() → VectorStore.delete(inactive ids)                   │
│                                                                                    │
│  State on one RWO PVC:                                                             │
│    • estimator_king.db   (SQLite — products, crawl_queue)                          │
│    • chroma/             (ChromaDB PersistentClient directory)                     │
│                                                                                    │
│  Providers (OpenAI SDK; base_url-swappable → OpenAI default / ollama / compatible) │
│    • EmbeddingProvider     • ChatProvider                                          │
└────────────────────────────────────────────────────────────────────────────────────┘
```

### Module layout

New / changed modules:

```
estimator_king/
├── llm/                         # NEW provider abstraction
│   ├── __init__.py
│   ├── config.py                # ProviderConfig dataclass (model/base_url/key/dims)
│   ├── embeddings.py            # EmbeddingProvider
│   └── chat.py                  # ChatProvider + Pydantic estimate models
├── vectorstore/                 # NEW
│   ├── __init__.py
│   └── store.py                 # VectorStore (ChromaDB PersistentClient wrapper)
├── sync/
│   ├── engine.py                # REWORKED: embed + upsert to VectorStore (no Dify)
│   ├── inactive.py              # CHANGED: also delete inactive vectors from VectorStore
│   ├── dify_client.py           # DELETED
│   └── async_dify.py            # DELETED
├── crawler/
│   ├── pipeline.py              # CHANGED: enqueue_oldest_products replaces enqueue_stale_products
│   └── async_pipeline.py        # CHANGED: takes VectorStore instead of DifyKBClient
├── bot/
│   ├── estimator.py             # NEW: replaces workflow_client.py
│   ├── workflow_client.py       # DELETED
│   ├── commands.py              # CHANGED: wire to Estimator
│   ├── scheduler.py             # NEW: in-process daily crawl scheduler
│   └── __main__.py / runner     # CHANGED: start scheduler alongside the bot
├── database/
│   ├── schema.sql               # REWRITTEN greenfield
│   └── repository.py            # REWORKED: store_id column, new queries, no migrations
├── config_schema.py             # CHANGED: drop Dify + interval fields; add provider + budget
└── __main__.py                  # CHANGED: build VectorStore + providers instead of Dify client
```

---

## 4. Provider abstraction (`estimator_king/llm/`)

All external model calls go through `openai.OpenAI(base_url=..., api_key=...)`. Pointing
`base_url` at `https://api.openai.com/v1` (default) uses OpenAI; pointing it at
`http://<ollama-host>:11434/v1` uses ollama. Confirmed working for both chat and embeddings
(openai SDK 2.38.0).

### 4.1 `ProviderConfig` (`llm/config.py`)

```python
@dataclass
class ProviderConfig:
    # Embeddings
    embedding_base_url: str | None      # None → OpenAI default base
    embedding_api_key: str
    embedding_model: str                # default "text-embedding-3-large"
    embedding_dimensions: int | None    # default 1024 (None → model native)
    embedding_query_prefix: str = ""    # e.g. "query: " for e5-style models
    embedding_doc_prefix: str = ""      # e.g. "passage: " for e5-style models

    # Chat
    chat_base_url: str | None           # None → OpenAI default base
    chat_api_key: str
    chat_model: str                     # default "gpt-4o"
    chat_structured_output: bool = True # True → use chat.completions.parse; False → json_object
```

Embedding and chat each have their own `base_url`/`api_key`/`model` so the "split provider"
option (local embeddings + hosted chat) works without code change. When only a single
`OPENAI_API_KEY` / `OPENAI_BASE_URL` is provided, both default to it.

### 4.2 `EmbeddingProvider` (`llm/embeddings.py`)

```python
class EmbeddingProvider:
    def __init__(self, config: ProviderConfig): ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed product documents (applies embedding_doc_prefix). Batches input."""

    def embed_query(self, text: str) -> list[float]:
        """Embed a single search query (applies embedding_query_prefix)."""
```

- Wraps `client.embeddings.create(model=..., input=[...], dimensions=...)`.
  `dimensions` is only sent when not None (OpenAI `text-embedding-3-*` support it; omit for
  models that don't, e.g. bge-m3 via ollama).
- Extracts vectors via `response.data[i].embedding`, preserving input order.
- Batches `embed_documents` input in a single `create` call per batch (configurable batch size,
  default 100) to limit request count.
- Prefixes: `embed_documents` prepends `embedding_doc_prefix`; `embed_query` prepends
  `embedding_query_prefix`. Empty by default (OpenAI needs no prefix).

### 4.3 `ChatProvider` (`llm/chat.py`)

Pydantic models defining the structured estimate output (mirrors the current dataclasses and
the documented Dify output schema):

```python
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
    confidence: str               # "high" | "medium" | "low"
    rationale: str
    reference_products: list[ReferenceProduct]

class EstimateBatch(BaseModel):
    estimates: list[ProductEstimate]
```

```python
class ChatProvider:
    def __init__(self, config: ProviderConfig): ...

    def estimate(self, system_prompt: str, user_prompt: str) -> EstimateBatch:
        """Call the chat model and return validated estimates."""
```

- When `chat_structured_output` is True: use
  `client.chat.completions.parse(model=..., messages=[...], response_format=EstimateBatch)` and
  return `response.choices[0].message.parsed` (a validated `EstimateBatch`). Requires a
  structured-output-capable model (e.g. `gpt-4o-2024-08-06`+).
- When False (e.g. ollama or a model without strict schema support): use
  `response_format={"type": "json_object"}`, read `response.choices[0].message.content`,
  `json.loads` it, then validate with `EstimateBatch.model_validate(...)`. The system prompt
  must explicitly demand JSON matching the schema.
- On refusal (`message.refusal` set) or validation failure → raise `EstimationError`
  (defined in `llm/chat.py`).

---

## 5. Vector store (`estimator_king/vectorstore/store.py`)

Wraps an embedded ChromaDB `PersistentClient`. We pass **precomputed embeddings** from
`EmbeddingProvider` rather than configuring a Chroma embedding function — this keeps a single
provider abstraction governing both embeddings and chat, and gives full control over prefixes
and dimensions.

```python
class VectorStore:
    COLLECTION = "products"

    def __init__(self, path: str):
        self._client = chromadb.PersistentClient(path=path)
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )

    def upsert(self, id: str, document: str, embedding: list[float],
               metadata: dict[str, str]) -> None:
        self._collection.upsert(ids=[id], documents=[document],
                                embeddings=[embedding], metadatas=[metadata])

    def delete(self, ids: list[str]) -> None:
        if ids:
            self._collection.delete(ids=ids)

    def query(self, embedding: list[float], n_results: int,
              where: dict | None = None) -> list[QueryHit]:
        res = self._collection.query(
            query_embeddings=[embedding], n_results=n_results,
            where=where, include=["documents", "metadatas", "distances"],
        )
        # res["documents"]/["metadatas"]/["distances"] are list-of-lists (one per query);
        # we use index [0]. Return a flat list[QueryHit].
```

- `id` = product `external_key` (`"{store_id}:{product_id}"`), deterministic — no
  server-assigned id needed. `upsert` therefore handles both create and update.
- `metadata` per vector: `{"store_id", "product_id", "product_url", "content_hash", "title",
  "price_jpy"}` (strings; ChromaDB metadata values must be str/int/float/bool). `price_jpy`
  and `title` enable building `ReferenceProduct`s from retrieval hits without re-parsing the
  document text.
- `where` supports metadata filters, e.g. `{"store_id": "hololive"}`.
- `QueryHit` is a small dataclass `{id, document, metadata, distance}`.
- Collection uses cosine distance (`hnsw:space: cosine`).
- ChromaDB writes are synchronous and durable — **no indexing-status polling** (the Dify
  `_poll_indexing_status` logic is deleted entirely).

---

## 6. Sync engine rework (`estimator_king/sync/engine.py`)

Keep the document formatting; replace the Dify calls with embed + upsert.

- **Keep** `_format_product_document(snapshot, store_id, product_url)` → returns
  `(document_name, text_content, metadata)`. The markdown `text_content` is what gets embedded
  and stored as the Chroma `document`. Extend the returned `metadata` to include `title` and a
  representative `price_jpy` (e.g. the minimum variant price, integer) for retrieval display.
- **Replace** `_try_create_document` / `_try_update_document` / `_poll_indexing_status` /
  `DifyKBClient` usage. `sync_products(snapshots, store_id, base_url, repository, embedder,
  vector_store)` replaces the `dify_client` parameter with `embedder` + `vector_store`. For each
  snapshot (which has already been fetched successfully by the caller):
  1. compute `external_key = "{store_id}:{product_id}"`, `product_url`, `content_hash`; load
     `state = repository.get_by_external_key(external_key)`;
  2. **skip** if `state` exists, `state.content_hash == content_hash`, and `state.last_indexed_at`
     is not null → increment `skipped`. The product was still fetched, so the persisted
     `ProductState` (step 4) is written with the existing `content_hash`/`last_indexed_at`
     carried forward;
  3. otherwise (new or changed) embed `text_content` via
     `EmbeddingProvider.embed_documents([text])[0]`, then
     `VectorStore.upsert(external_key, text, embedding, metadata)`; set `last_indexed_at = now`;
     increment `created` if `state` is None else `updated`;
  4. **persist `ProductState`** (the single write — see "Single writer" below) with `store_id`,
     `product_id`, `product_url`, `content_hash`, `normalizer_version`,
     `last_fetch_success_at = now`, `last_indexed_at` (now if (re)indexed in step 3, else the
     carried-forward value), `consecutive_failures = 0`, and the **sitemap-tracking fields carried
     forward** from `state`: `last_seen_in_sitemap_at = state.last_seen_in_sitemap_at` and
     `consecutive_sitemap_misses = state.consecutive_sitemap_misses` when `state` exists, else
     `last_seen_in_sitemap_at = now` (a brand-new product was just seen in the sitemap that
     enqueued it) and `consecutive_sitemap_misses = 0`.

- **Single writer (reconciles the async double-upsert).** Today `async_process_queue` writes each
  product twice — once via `normalizer(...) → state_repo.upsert(normalized)` and again via
  `sync_products(...)`. In the new design `sync_products` is the **sole** writer of product rows
  on the success path: the separate `state_repo.upsert(normalized)` call **and** the `normalizer`
  parameter are **removed** from `async_process_queue`. The normalizer is deleted outright (not
  reduced to a predicate): `fetch_product` raises on every HTTP/JSON error
  (`ShopifyHTTPError` / `ShopifyJSONError`), so any snapshot it returns is already valid and
  indexable — there is no in-band "skip this fetched product" decision to make, and disappearance
  is handled separately (see below). `sync_products` derives `store_id`/`product_id` from the
  snapshot + `store_id` argument, so the new schema columns are populated by this single write.

- **Fetch success/failure bookkeeping (makes the budget + inactive logic work).** Because
  `last_fetch_success_at` drives the oldest-first budget query (§7) and `consecutive_failures`
  drives inactivation (§10), the async path must maintain them — today only the sync pipeline did:
  - On a **successful fetch + sync** of a product, `sync_products` sets
    `last_fetch_success_at = now` and `consecutive_failures = 0` in the persisted `ProductState`
    (covers create/update/skip — all imply a successful fetch).
  - On a **fetch failure** in `async_process_queue` (the `except` branch), look up the existing
    row via `repository.get_by_product_url(store_id, product_url)`; if found, call
    `repository.increment_consecutive_failures(state.external_key)` (a brand-new URL with no row
    yet is simply left in the queue for retry — there is no `external_key` to record against). The
    queue entry is **not** deleted on failure (existing resumability behavior).
  - `repository.upsert` must **`COALESCE`** only the **nullable** timestamp columns
    `last_fetch_success_at` and `last_indexed_at` (in addition to those it already coalesces:
    `dify_document_id` is gone; `product_url` stays coalesced), so a write that legitimately omits
    one of these never wipes a previously-stored value. `consecutive_failures` and
    `consecutive_sitemap_misses` are non-nullable `int`s and are **not** coalesced — they are
    always written with the concrete value the caller supplies.
  - **Do not clobber sitemap-tracking state.** Within a cycle, `populate_queue_from_sitemap` runs
    *before* `async_process_queue` and calls `record_sitemap_seen` (sets
    `last_seen_in_sitemap_at = now`, `consecutive_sitemap_misses = 0`) / `increment_sitemap_miss`
    on existing rows. Because `upsert` overwrites non-coalesced columns from `excluded.*`, the
    single-writer `sync_products` MUST carry `last_seen_in_sitemap_at` and
    `consecutive_sitemap_misses` forward from the loaded `state` (step 4) so the fetch does not
    reset the sitemap-seen timestamp to NULL or zero out accrued misses — otherwise the §10
    sitemap-miss inactivation path is broken. (`record_sitemap_seen` / `increment_sitemap_miss` /
    `increment_consecutive_failures` / `reset_consecutive_failures` remain targeted `UPDATE`
    statements that touch only their own columns, so they never clobber each other.)

- **Vector deletion on disappearance** is **not** done per-fetch. A removed product simply stops
  appearing in the sitemap (→ `consecutive_sitemap_misses` accrues) and/or starts failing to
  fetch (→ `consecutive_failures` accrues); once either threshold is exceeded,
  `mark_inactive_products` flags it inactive and deletes its vector in one place (§10). There is
  no separate `should_index`-driven deletion path.

- **Error handling**: keep the fire-and-forget pattern. On embedding/vector errors inside
  `sync_products`, log, persist the `ProductState` **without** advancing `last_indexed_at` (but
  still advancing `last_fetch_success_at`, since the fetch itself succeeded), and count `failed` —
  the product is re-indexed on a later run because `content_hash != last indexed`. Provider
  exceptions are caught here; unexpected exceptions are logged via `logging.exception` and counted
  as failed.
- `SyncResult` (created/updated/skipped/failed/failed_ids) is unchanged.

---

## 7. Crawl scheduling & budget

### 7.1 Queue population change (`crawler/pipeline.py`)

- `populate_queue_from_sitemap` is **unchanged**: it enqueues every sitemap URL not yet in the
  `products` table (new products) and returns `new_count`; it records sitemap-seen for existing
  products and increments sitemap-miss for active products absent from the sitemap.
- `enqueue_stale_products` is **removed**. Replaced by:

```python
def enqueue_oldest_products(store, repo, *, limit: int) -> int:
    """Enqueue up to `limit` existing active products, oldest last_fetch first."""
    if limit <= 0:
        return 0
    products = repo.get_oldest_active_products(store.id, limit)
    enqueued = 0
    for state in products:
        if repo.enqueue_url(store.id, state.product_url):
            enqueued += 1
    return enqueued
```

- Under `--force-refetch`, `enqueue_oldest_products` is **not** called; instead the orchestrator
  (§7.2) enqueues **all** active products via `repo.list_active(store.id)` (bypasses the budget),
  for backfills. `enqueue_oldest_products` itself does not take a `force_refetch` argument — the
  branch lives in the orchestrator.

### 7.2 Per-run orchestration (per store)

```
new_count = populate_queue_from_sitemap(store, repo, enumerator)   # new products always enqueued
if force_refetch:
    for state in repo.list_active(store.id):                       # backfill: all active
        repo.enqueue_url(store.id, state.product_url)
else:
    remaining = max(0, policy.max_products_per_run - new_count)
    enqueue_oldest_products(store, repo, limit=remaining)          # oldest existing, budgeted
async_process_queue(...)                                           # fetch → embed → upsert
```

- New products are **always** fetched (even if `new_count > max_products_per_run`).
- Existing-product updates per store per run = `max(0, max_products_per_run − new_count)`,
  chosen oldest-first by `last_fetch_success_at` (never-fetched sort first).
- No overlap between the two enqueue steps: new products are not yet in `products`
  (they enter only after a successful fetch), so `get_oldest_active_products` cannot return them.

### 7.3 Scheduler (`bot/scheduler.py`)

In-process asyncio loop, no new dependency:

- On bot startup, start a background task. It runs one full crawl cycle (all stores) immediately
  (configurable; default run-on-start), then sleeps `crawl_schedule_hours` (default 24) and
  repeats.
- A re-entrancy guard (`asyncio.Lock` or a "running" flag) prevents a new cycle from starting
  while one is still in progress.
- **Cycle structure** (mirrors today's `run_crawler` in `__main__.py`): iterate the stores,
  running the §7.2 per-store orchestration for each; then, **once after all stores**, call
  `mark_inactive_products(repo, vector_store, failure_threshold=..., miss_threshold=...)`. The
  inactive check is intentionally cross-store and runs a single time per cycle (matching current
  behavior and the §10 note) — it is **not** inside the per-store loop.
- The cycle opens its own `ProductStateRepository` (the SQLite connection allows cross-thread
  access; the ChromaDB client / `VectorStore` is the single shared instance) for the duration of
  a cycle.
- Errors in one store's processing are logged and do not abort the other stores or the loop.
- A manual entry point is retained: `python -m estimator_king` runs exactly one crawl cycle
  (same structure: all stores, then one cross-store inactive check) and exits — used for local
  runs and backfills via `--force-refetch`.

---

## 8. Estimation pipeline (`estimator_king/bot/estimator.py`)

Re-implements the Dify workflow (`Start → Knowledge Retrieval → LLM → Code → End`) in Python.

```python
class Estimator:
    def __init__(self, embedder: EmbeddingProvider, chat: ChatProvider,
                 vector_store: VectorStore, *, top_k: int = 10): ...

    def estimate_products(self, product_names: list[str], user_id: str) -> EstimateBatch:
        ...
```

Flow (preserving existing chunking — `CHUNK_SIZE = 10` per LLM call):

1. For the chunk, retrieve references: for each product line, `embedder.embed_query(line)` →
   `vector_store.query(embedding, n_results=top_k)` → collect hits. (Optionally filtered by
   store via `where`; default no filter — search across all stores.)
2. Build the **context** block from retrieved hits (product title, price_jpy, store) and the
   **system + user prompts** — same intent as the documented Dify system prompt: "you are the
   Estimator King… for each product line find closest matches in context… return estimates with
   suggested_price_jpy, price_range_jpy, confidence (high/medium/low), rationale,
   reference_products (≤3)".
3. `chat.estimate(system_prompt, user_prompt)` → validated `EstimateBatch`.
4. Aggregate estimates across chunks into a single `EstimateBatch`.

- `confidence` semantics preserved: high = direct/close match, medium = similar types, low = no
  strong matches.
- The `WorkflowResult` shape (`estimates`, optional `workflow_run_id`, `elapsed_time`) is
  replaced by `EstimateBatch`; `commands.py` rendering is adapted (see §9).

---

## 9. Discord bot wiring (`estimator_king/bot/commands.py`)

- `format_workflow_result` rendering logic is **kept** (same Discord embed output users see), but
  retyped to consume `EstimateBatch.estimates` (`list[ProductEstimate]`). `workflow_run_id` is
  dropped from the footer (no longer meaningful); `elapsed_time` may be computed locally
  (wall-clock around the estimate call) or omitted.
- `ProductInputModal.on_submit` builds an `Estimator` from app config (providers + shared
  `VectorStore`) instead of a `WorkflowClient`, calls `estimate_products`, and renders embeds.
- Error handling: replace `requests.Timeout`/`requests.HTTPError` branches with provider/OpenAI
  exceptions and `EstimationError`; show user-friendly messages (config error, upstream error,
  invalid response).
- The bot constructs **one** shared `VectorStore` (and providers) at startup and passes it to
  both `setup_commands` and the scheduler (single ChromaDB client per process).

---

## 10. Inactive products (`estimator_king/sync/inactive.py`)

- `mark_inactive_products` still flags products exceeding failure / sitemap-miss thresholds.
- **New behavior**: when a product is marked inactive, its vector is **deleted** from ChromaDB
  (`VectorStore.delete([external_key])`) so estimates never cite dead products. This is the
  **single** disappearance-handling path — products that leave the sitemap (sitemap-miss
  threshold) or repeatedly fail to fetch incl. 404 (failure threshold) flow through here (§6).
- `mark_inactive_products` gains a `vector_store` parameter; it collects the external_keys it
  deactivates this run and deletes them in one `VectorStore.delete(ids)` call.
- The existing cross-store coupling note (inactive check runs across all stores) is unchanged
  and out of scope.

---

## 11. Database schema (greenfield) & repository

### 11.1 `schema.sql` (rewritten)

```sql
-- Estimator King state database (SQLite). Greenfield — created fresh, no migrations.
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS products (
    external_key   TEXT PRIMARY KEY,            -- "{store_id}:{product_id}" — also the ChromaDB id
    store_id       TEXT NOT NULL,
    product_id     TEXT NOT NULL,
    product_url    TEXT NOT NULL,

    content_hash   TEXT NOT NULL,               -- hash of last indexed snapshot
    normalizer_version INTEGER NOT NULL,

    last_seen_in_sitemap_at TEXT,
    last_fetch_success_at   TEXT,               -- NULL = never fetched (sorts oldest-first)
    last_indexed_at         TEXT,               -- when last upserted into ChromaDB
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

Removed vs the old schema: `dify_document_id` column + index; `schema_version` table; the
`last_seen_in_sitemap_at` index (unused by remaining queries). Added: `store_id`/`product_id`
columns, `last_indexed_at`, and a purpose-built composite index for the oldest-first budget
query. `product_url` is now `NOT NULL`.

### 11.2 `repository.py` changes

- `ProductState` dataclass: **remove** `dify_document_id`; **add** `store_id`, `product_id`,
  `last_indexed_at`. Update `with_updated_timestamps` accordingly.
- **Remove** the migration system entirely: `_SCHEMA_VERSION`, `_migrate`, the `schema_version`
  row handling in `_ensure_schema`. `_ensure_schema` becomes `conn.executescript(schema_sql)`.
- **Replace** all `WHERE external_key LIKE '{store_id}:%'` filters with `WHERE store_id = ?`
  (affects `list_active`, `get_by_product_url`, and any per-store query).
- **Remove** `get_products_needing_fetch` (interval-based, superseded) and `get_stale_products`
  (days-based staleness, now unused — only referenced by tests today; its supporting
  `idx_products_last_seen_in_sitemap_at` index is also dropped, §11.1).
- **Add** `get_oldest_active_products(store_id, limit)`:
  ```sql
  SELECT * FROM products
  WHERE store_id = ? AND inactive = 0
  ORDER BY last_fetch_success_at ASC      -- NULLs first in SQLite ASC → never-fetched first
  LIMIT ?
  ```
- `upsert` writes the new columns; `external_key` remains the conflict key. `store_id` /
  `product_id` are derived from the snapshot + `store_id` argument by `sync_products` (the single
  writer, §6), which constructs the `ProductState` it persists.
- Keep `check_same_thread=False` (the scheduler thread-pool and async pipeline need it).

---

## 12. Configuration (`config_schema.py`, env, YAML)

### 12.1 Removed
- `Store.fetch_interval_hours`
- `CrawlerPolicy.default_fetch_interval_hours`
- `AppConfig.dify_api_key`, `dify_base_url`, `dify_dataset_id`, `dify_workflow_api_key`,
  `dify_workflow_base_url`
- All `DIFY_*` env handling and the corresponding `--dify-*` CLI args in `__main__.py`.

### 12.2 Added

`CrawlerPolicy` (from YAML `crawler:`, validated `> 0`):
- `max_products_per_run: int = 32` — per-store fetch budget per scheduled run.
- `crawl_schedule_hours: float = 24.0` — scheduler cadence.

`AppConfig` provider/store fields (from env):
- `OPENAI_API_KEY` (required for bot) — default key for both embeddings and chat.
- `OPENAI_BASE_URL` (optional) — default base for both; set to ollama's `/v1` to swap.
- `EMBEDDING_MODEL` (default `text-embedding-3-large`)
- `EMBEDDING_DIMENSIONS` (default `1024`; empty/unset → model native)
- `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY` (optional overrides; fall back to `OPENAI_*`)
- `EMBEDDING_QUERY_PREFIX`, `EMBEDDING_DOC_PREFIX` (default empty)
- `CHAT_MODEL` (default `gpt-4o`)
- `CHAT_BASE_URL`, `CHAT_API_KEY` (optional overrides; fall back to `OPENAI_*`)
- `CHAT_STRUCTURED_OUTPUT` (default `true`; set `false` for endpoints without strict schema)
- `CHROMA_PATH` (default `./chroma`)
- `DATABASE_PATH` (default `./estimator_king.db`) — unchanged

`load_config` assembles a `ProviderConfig` from these, with embedding/chat overrides falling
back to the shared `OPENAI_*` values.

### 12.3 Validation
- Bot entry point validates: `OPENAI_API_KEY` (or per-purpose keys) present; `CHROMA_PATH`
  writable. Crawler-cycle entry point validates the same provider config (it embeds).
- `Store.validate` drops the `fetch_interval_hours` check. `CrawlerPolicy.validate` drops
  `default_fetch_interval_hours`, adds `max_products_per_run > 0` and `crawl_schedule_hours > 0`.

### 12.4 `stores_config.yaml` example
```yaml
crawler:
  rate_limit_rps: 1.5
  jitter_max: 0.5
  concurrency_per_domain: 3
  timeout_connect: 10
  timeout_read: 30
  max_retries: 3
  inactive_failure_threshold: 3
  inactive_sitemap_miss_threshold: 4
  max_products_per_run: 32       # per-store daily fetch budget
  crawl_schedule_hours: 24.0     # scheduler cadence

stores:
  - id: hololive
    base_url: https://shop.hololivepro.com
    sitemap_url: https://shop.hololivepro.com/sitemap.xml
```

(Per-store `fetch_interval_hours` lines are removed.)

---

## 13. Embedding model recommendation (Japanese support)

Research against the JMTEB (Japanese Massive Text Embedding Benchmark) and OpenAI docs:

- **Default — `text-embedding-3-large` at `dimensions=1024`.** Consistent with the OpenAI-default
  decision; good multilingual incl. Japanese; 1024 dims is a solid size/quality balance for short
  product text (native 3072 is overkill). No prefixes required. Pricing ~$0.13 / 1M input tokens.
  *Caveat documented*: OpenAI's `text-embedding-3-*` is multilingual but not Japanese-optimized;
  Japanese-specialized models score higher on JMTEB.
- **Documented local Japanese swap — `bge-m3` via ollama.** 1024-dim, no prefixes, served on
  ollama's OpenAI-compatible `/v1` endpoint → only env changes needed
  (`EMBEDDING_BASE_URL=http://<ollama>:11434/v1`, `EMBEDDING_MODEL=bge-m3`,
  `EMBEDDING_DIMENSIONS` unset). Strong multilingual Japanese.
- **Noted highest-Japanese-quality option — `cl-nagoya/ruri-v3-310m`** (JMTEB 77.24, 768-dim,
  no prefixes, 8k context). Not available on ollama; requires a small sentence-transformers / HF
  serving process exposing an OpenAI-compatible endpoint. Listed for later if Japanese retrieval
  quality becomes critical.

> **Re-index warning (documented):** changing the embedding model or dimensions changes the
> vector space; the ChromaDB collection must be rebuilt (delete `chroma/` and re-crawl, or run
> `--force-refetch`). Mixed-model vectors are invalid.

---

## 14. Dependencies & deployment

### 14.1 `requirements.txt`
- **Remove**: `dify-client`
- **Add**: `chromadb` (1.5.x), `openai` (2.x), `pydantic` (2.x)
- Keep: `requests`, `aiohttp`, `beautifulsoup4`, `markdownify`, `lxml`, `discord.py`, `tenacity`,
  `pyyaml`, and test deps.

### 14.2 Kubernetes (`deploy/`)
- **Delete** `crawler-cronjob.yaml` and remove it from `kustomization.yaml`.
- `crawler-pvc.yaml` (RWO) is **kept** but now mounted by the **bot** Deployment at `/data`
  (holds `estimator_king.db` and `chroma/`). Rename label/clarify ownership.
- `bot-deployment.yaml`: mount the PVC at `/data`; set `DATABASE_PATH=/data/estimator_king.db`,
  `CHROMA_PATH=/data/chroma`; inject `OPENAI_API_KEY` (+ optional `*_BASE_URL` / overrides) from
  the secret/config; mount the `stores_config.yaml` ConfigMap; bump resources (embedding + LLM +
  crawl now run here — e.g. requests 512Mi/250m, limits 1Gi/500m).
- `configmap.yaml`: drop `DIFY_BASE_URL`; add `OPENAI_BASE_URL` (if non-default), `EMBEDDING_*`,
  `CHAT_*`, `CHROMA_PATH`.
- `secrets.yaml`: drop `DIFY_API_KEY` / `DIFY_DATASET_ID` / `DIFY_WORKFLOW_API_KEY`; add
  `OPENAI_API_KEY` (+ optional per-purpose keys).

### 14.3 Deletions (full Dify removal)
- **Delete** the entire `dify-deploy/` directory (postgres, redis, weaviate, api, workers,
  sandbox, ssrf-proxy, nginx, plugin-daemon, ingress, etc.) and its kustomization.
- **Delete** Dify docs: `docs/dify-dataset-setup.md`, `docs/dify-workflow-contract.md`,
  `dify_python_sdk_research_report.md`, `estimator-dify-plan.md`.
- The stray `dify/` directory at repo root is removed if present.

### 14.4 Docs to update
- `README.md`: architecture (single process), env vars (OpenAI/Chroma), remove Dify.
- `docs/local-runbook.md`, `docs/ops-runbook.md`: replace Dify setup with provider + Chroma
  config, the daily budget behavior, and the re-index procedure.
- `.env.example`: drop `DIFY_*`; add `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `EMBEDDING_*`,
  `CHAT_*`, `CHROMA_PATH`.

---

## 15. Error handling summary

| Failure | Behavior |
| --- | --- |
| Embedding API error during crawl | log, count `failed`, leave `last_indexed_at` unchanged → retried next run (fire-and-forget) |
| Vector upsert error | same as above |
| Chat API error / refusal / invalid JSON during `/estimate` | raise `EstimationError`; bot shows a user-friendly upstream-error message |
| ChromaDB unavailable / path not writable at startup | fail fast with a clear log (bot cannot serve estimates) |
| Sitemap returns 0 URLs | existing behavior: warn, skip store, do not mark misses |
| Fetch error in queue | existing behavior: increment `consecutive_failures`, leave entry in queue for retry |
| Scheduler cycle exception | logged; does not crash the bot or stop future cycles |
| Circuit breaker open | existing behavior: pause queue draining, resume next cycle |

---

## 16. Testing strategy

- **Providers**: unit-test `EmbeddingProvider` / `ChatProvider` with a mocked `openai.OpenAI`
  client — verify request shape (model, input, dimensions, prefixes), structured-output vs
  json_object branches, vector extraction, and error/refusal handling.
- **VectorStore**: integration-test against a real ChromaDB `PersistentClient` in a tmp dir —
  upsert/query/delete, cosine ordering, metadata `where` filter, precomputed-embedding path.
- **Sync engine**: unit-test the embed+upsert path with a fake embedder + in-memory/tmp
  VectorStore — create/update/skip/failed accounting, `content_hash` skip logic,
  `last_indexed_at` advancement.
- **Pipeline/budget**: unit-test `enqueue_oldest_products` and the per-store budget arithmetic
  (`new_count`, `remaining`, oldest-first ordering, `force_refetch` bypass).
- **Repository**: test `get_oldest_active_products` ordering (NULLs first), `store_id`-column
  queries, fresh-schema creation (no migration path).
- **Estimator**: unit-test the retrieve → prompt → estimate flow with mocked providers + tmp
  VectorStore — chunking, reference assembly, aggregation across chunks.
- **Scheduler**: test one cycle runs all stores, re-entrancy guard, and that a store error does
  not abort the cycle (with mocked pipeline).
- **Bot commands**: keep/adapt rendering tests for `format_workflow_result` against
  `EstimateBatch`; adapt error-path tests to provider exceptions.
- **Remove** (Dify-specific, will not survive the rewrite): `test_dify_client.py`,
  `test_async_dify_wrapper.py`, `test_poll_indexing_status.py`, `test_bot_workflow_client.py`,
  `test_sync_products_docid.py` (asserts on the removed `dify_document_id`),
  `test_sync_fire_and_forget.py` (Dify async-indexing fire-and-forget), and
  `test_migration.py` (the migration system is deleted).
- **Adapt** (rework, not delete) — rewritten against the embed/upsert + provider/VectorStore
  equivalents above (and the new `store_id` column, budget, and scheduler behavior):
  - `test_sync_*` (remaining), `test_config.py`, `test_main_async.py`, `test_cli.py`,
    `test_integration_async_pipeline.py`, `test_e2e_mocked.py` — Dify-specific assertions → new equivalents.
  - `test_async_pipeline.py` — drop the removed `normalizer` parameter / double-upsert assertions.
  - `test_pipeline.py` — drop the `enqueue_stale_products`, `process_queue` (sync `DifyKBClient`
    path), and `get_products_needing_fetch` suites; rewrite around `enqueue_oldest_products`, the
    §7.2 budget arithmetic, and the embed/upsert async path.
  - `test_inactive.py` — pass the new required `vector_store` argument to every
    `mark_inactive_products(...)` call and assert inactive-vector deletion (§10).
  - `test_repository.py` — remove the `get_stale_products` / `get_products_needing_fetch` cases;
    add `get_oldest_active_products` ordering (NULLs first) and `store_id`-column query tests.
- All work validated with `pyright`/`basedpyright`, `ruff`, and `pytest` per project rules.

---

## 17. Open decisions (defaults chosen; flag to change)

1. **Delete `dify-deploy/` + Dify docs entirely** — *default: delete* (full removal).
2. **Delete inactive products' vectors from ChromaDB** — *default: delete*.
3. **`EMBEDDING_DIMENSIONS` default 1024** for `text-embedding-3-large` — *default: 1024*.
4. **Cross-store retrieval** in `/estimate` (no `where` store filter) — *default: search all
   stores*, since users query product names without specifying a store.
