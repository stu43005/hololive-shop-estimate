# Item-Level ChromaDB Indexing + Type-Aware Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-index ChromaDB at the granularity of a single priceable *item* (not the whole product), classify each item's type, and make `/estimate` retrieve type-aligned, recency-weighted references — fixing the diagnosed precision problems.

**Architecture:** Crawl path: `ProductSnapshot → decompose_items → classify_item → sync_products` writes one vector per item (own price, type, published date, spec snippet). Query path: per input line, tag type → type-filtered + plain vector queries → merge/dedup → recency rerank → reconcile to input lines. Item-type LLM classification is two-tier (controlled-vocab match first, small model only on miss/multi-hit) with a SQLite cache, decoupled from `content_hash`.

**Tech Stack:** Python 3.14, ChromaDB 1.5.9 (cosine, precomputed embeddings), SQLite (WAL), OpenAI SDK 2.x (chat/embeddings/typing), pydantic, pytest.

**Verification toolchain (run after each task):**
- Type check: `.venv/bin/basedpyright estimator_king` (gate: 0 errors in `estimator_king/`)
- Lint: `uvx ruff check estimator_king scripts`
- Per-file tests: `.venv/bin/python -m pytest <path> -v -o addopts=""`
- Full suite: `.venv/bin/python -m pytest`

---

## File Structure

**New files:**
- `estimator_king/sync/items.py` — `ProductItem` dataclass + `decompose_items()` (SET/¥0 exclusion, talent-gated dedup, naming, snippet extraction).
- `estimator_king/sync/typing.py` — `classify_item()` / `classify_query()` (two-tier orchestration + cache).
- `estimator_king/llm/typing_provider.py` — `TypingProvider` (lazy OpenAI client, `classify_via_llm`).
- `scripts/mine_talents.py` — one-time talent-seed miner (prints YAML list to stdout).
- Test files mirror each under `tests/`.

**Modified files:**
- `estimator_king/crawler/snapshot.py` — `published_at` field; expose `normalize_text`.
- `estimator_king/crawler/shopify.py` — parse `published_at`; `content_hash` default.
- `estimator_king/database/schema.sql` — `products.item_types_version`; `item_type_cache` table.
- `estimator_king/database/repository.py` — `ProductState.item_types_version`; idempotent ALTER; type-cache + `list_other_typed` methods.
- `estimator_king/llm/config.py` — `ProviderConfig` typing fields.
- `estimator_king/config_schema.py` — `AppConfig` fields; `load_config` parsing; cascade.
- `estimator_king/vectorstore/store.py` — `get_by_product()`.
- `estimator_king/sync/engine.py` — per-item `sync_products`; `_format_item_document`; `item_hash`; gating.
- `estimator_king/crawler/async_pipeline.py`, `cycle.py`, `scheduler.py` — thread typing params.
- `estimator_king/runtime.py` — `Providers.typing`; `build_providers`; serve wiring.
- `estimator_king/bot/runner.py` — `build_bot` typing param + Estimator injection.
- `estimator_king/bot/estimator.py` — `_Hit` Protocol; per-line retrieval; recency rerank; reconciliation; system prompt.
- `estimator_king/bot/commands.py` — `format_estimates` page-count + suffix fixes.
- `stores_config.yaml` — `item_types`, `item_types_version`, `talents`, `estimator`.
- `CLAUDE.md`, `docs/local-runbook.md`, `docs/ops-runbook.md` — migration notes.

**Dependency order:** snapshot → shopify → config(provider) → TypingProvider → schema/repository → config_schema → items → typing → vectorstore → engine → async_pipeline → cycle → scheduler → runtime → __main__ → estimator → runner → commands → stores_config/mine_talents → docs.

---

## Existing Tests Broken by Signature Changes (MUST update in the listed task)

Several signature changes break **pre-existing** tests. Each is handled inside the task that makes the change (also called out below so nothing is missed). The full suite (Task 21) must stay green.

A shared minimal fake is used wherever a `TypingProvider` is needed in these updates:

```python
class FakeTypingProvider:
    def classify_via_llm(self, text, item_types):
        return "その他"
```

| Existing test file | Breaks because | Fix (in Task) |
|---|---|---|
| `tests/test_sync_engine.py`, `tests/test_sync_engine_logging.py` | Test the **old** product-level `sync_products` + removed `_format_product_document`/`_min_variant_price` | **Task 10:** `git rm` both (obsolete; superseded by `tests/test_engine_items.py`). Port the "Sync failed for %s" exception-log assertion into `test_engine_items.py` if you want to keep it. |
| `tests/test_async_pipeline.py` (all `async_process_queue(...)` call sites), `tests/test_async_pipeline_logging.py` | `async_process_queue` gains required kw-only `typing_provider`/`talents`/`item_types`/`item_types_version` | **Task 11:** add `typing_provider=FakeTypingProvider(), talents=frozenset(), item_types=[], item_types_version=0` to **every** call. |
| `tests/test_crawl_cycle.py` (3 calls), `tests/test_integration_async_pipeline.py` (3 calls) | `run_crawl_cycle` gains positional `typing_provider` (5th arg, before `*`) | **Task 12:** insert `FakeTypingProvider()` as the 5th positional arg in every call. |
| `tests/test_scheduler.py` (4 `CrawlScheduler(...)` + `fake_cycle` signature) | `CrawlScheduler.__init__` gains required `typing_provider`; `run_once` passes it positionally to `run_crawl_cycle` | **Task 13:** add `typing_provider=object()` to the 4 constructions; change `fake_cycle` to `async def fake_cycle(config, db_path, embedder, vector_store, typing_provider, *, force_refetch=False)`. |
| `tests/test_runtime.py`, `tests/test_main_async.py`, `tests/test_cli.py` (`Providers(...)`) | `Providers` gains required `typing` field | **Task 14:** add `typing=MagicMock()` (or a `FakeTypingProvider()`) to every `Providers(...)` construction. Positional-index assertions on the `CrawlScheduler(...)`/`run_crawl_cycle(...)` mocks still hold (vector_store stays `args[3]`, `typing_provider` is keyword/5th-positional). |
| `tests/test_e2e_mocked.py` (4 `Estimator(...)` + pre-seeded metadata) | `Estimator.__init__` new signature; `_format_reference` reads new metadata keys | **Task 16:** add `FakeTypingProvider()` + `item_types=[...], item_types_version=1` to the 4 calls; change the pre-seeded vector metadata from `{title, price_jpy, store_id}` to the new schema (`item_name, item_type, price_jpy, published_at, store_id, detail_snippet`) and assert on `item_name`. |
| `tests/test_estimator_logging.py` (`Estimator(...)` + chunk-debug log assertions) | new `Estimator` signature; rewrite keeps the per-chunk `logger.debug` line so the assertion survives | **Task 16:** fix the constructor call; the rewritten `estimate_products` (below) **retains** the `chunk %d/%d: %d products` debug line so `test_chunk_debug_and_done_info` still passes. |

---

## Task 1: Expose `normalize_text` and add `published_at` to ProductSnapshot

**Files:**
- Modify: `estimator_king/crawler/snapshot.py`
- Test: `tests/test_snapshot.py` (existing — add cases)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_snapshot.py`:

```python
from estimator_king.crawler.snapshot import (
    ProductSnapshot,
    normalize_text,
    compute_content_hash,
)


def test_normalize_text_is_public_and_collapses_whitespace():
    assert normalize_text("  a　b  ") == "a b"


def test_published_at_defaults_to_zero_and_excluded_from_hash():
    base = ProductSnapshot(
        product_id=1, title="t", description="d", variants=[], html_details={}
    )
    with_date = ProductSnapshot(
        product_id=1, title="t", description="d", variants=[], html_details={},
        published_at=1700000000,
    )
    assert base.published_at == 0
    # published_at must NOT change the content hash (deterministic gating)
    assert compute_content_hash(base) == compute_content_hash(with_date)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_snapshot.py -v -o addopts=""`
Expected: FAIL — `ImportError: cannot import name 'normalize_text'` (and `published_at` unknown).

- [ ] **Step 3: Implement — rename `_normalize_text` → `normalize_text` (keep private alias) and add field**

In `estimator_king/crawler/snapshot.py`, add `published_at` to the dataclass (after `html_details`):

```python
@dataclass
class ProductSnapshot:
    """Canonical product snapshot for change detection."""

    product_id: int
    title: str
    description: str
    variants: List[ProductVariant]
    html_details: Dict[str, str]  # Section name → content
    published_at: int = 0  # epoch seconds; 0 when unknown. Excluded from content hash.
```

Rename the function and keep a backward-compatible private alias. Replace the `def _normalize_text` definition with:

```python
def normalize_text(text: str) -> str:
    """Normalize text: decode entities, collapse whitespace."""
    # Decode HTML entities
    decoded = html.unescape(text)
    # Collapse whitespace
    normalized = " ".join(decoded.split())
    return normalized.strip()


# Backward-compatible private alias (internal callers used the underscore name).
_normalize_text = normalize_text
```

Do **not** add `published_at` to `canonicalize_snapshot` (it must stay excluded — the existing canonical dict already omits timestamps).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_snapshot.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Type-check, lint, commit**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king
git add estimator_king/crawler/snapshot.py tests/test_snapshot.py
git commit -m "feat(snapshot): expose normalize_text; add published_at field (excluded from hash)"
```

---

## Task 2: Parse `published_at` in shopify parser; fix dataclass field ordering

**Files:**
- Modify: `estimator_king/crawler/shopify.py`
- Test: `tests/test_shopify.py` (existing — add cases)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_shopify.py`:

```python
import json
from estimator_king.crawler.shopify import _build_snapshot, ProductSnapshotWithHash


def _product_json(**extra) -> str:
    product = {
        "id": 123,
        "title": "Test Product",
        "body_html": "<p>desc</p>",
        "variants": [{"id": 1, "title": "グッズ / Item A", "price": "500", "sku": None}],
    }
    product.update(extra)
    return json.dumps({"product": product})


def test_published_at_parsed_to_epoch():
    snap = _build_snapshot(
        _product_json(published_at="2023-06-30T19:00:07+09:00"), "<html></html>", "http://x/products/123"
    )
    # 2023-06-30T19:00:07+09:00 == 2023-06-30T10:00:07Z == 1688119207
    assert snap.published_at == 1688119207


def test_published_at_falls_back_to_created_at_then_zero():
    snap_created = _build_snapshot(
        _product_json(created_at="2023-06-30T19:00:07+09:00"), "<html></html>", "http://x/products/123"
    )
    assert snap_created.published_at == 1688119207
    snap_none = _build_snapshot(_product_json(), "<html></html>", "http://x/products/123")
    assert snap_none.published_at == 0


def test_snapshot_with_hash_constructs_with_published_at():
    # Guards the dataclass field-ordering fix (content_hash must have a default).
    obj = ProductSnapshotWithHash(
        product_id=1, title="t", description="d", variants=[], html_details={},
        published_at=42, content_hash="abc",
    )
    assert obj.published_at == 42 and obj.content_hash == "abc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_shopify.py -v -o addopts=""`
Expected: FAIL — `published_at` not parsed (0) / `TypeError` on dataclass ordering.

- [ ] **Step 3: Implement**

In `estimator_king/crawler/shopify.py`:

(a) Add imports near the top (after `import json`):

```python
from datetime import datetime
```

(b) Give `content_hash` a default so the base-class `published_at` default does not break field ordering:

```python
@dataclass
class ProductSnapshotWithHash(ProductSnapshot):
    content_hash: str = ""
```

(c) Add a parse helper (module level, after `_clean_body_html`):

```python
def _parse_published_at(product: dict[str, object]) -> int:
    """Epoch seconds from product.published_at, falling back to created_at, else 0."""
    for key in ("published_at", "created_at"):
        raw = product.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                return int(datetime.fromisoformat(raw).timestamp())
            except ValueError:
                continue
    return 0
```

(d) In `_build_snapshot_from_product_json`, set `published_at` on the returned snapshot. Replace the final `return ProductSnapshot(...)` with:

```python
    return ProductSnapshot(
        product_id=product_id,
        title=title,
        description=description,
        variants=variants,
        html_details=html_details,
        published_at=_parse_published_at(product),
    )
```

(e) In `_build_snapshot`, carry it onto `ProductSnapshotWithHash`. Replace the final `return ProductSnapshotWithHash(...)` with:

```python
    return ProductSnapshotWithHash(
        product_id=snapshot.product_id,
        title=snapshot.title,
        description=snapshot.description,
        variants=snapshot.variants,
        html_details=snapshot.html_details,
        published_at=snapshot.published_at,
        content_hash=content_hash,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_shopify.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Type-check, lint, commit**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king
git add estimator_king/crawler/shopify.py tests/test_shopify.py
git commit -m "feat(shopify): parse published_at to epoch; default content_hash for field ordering"
```

---

## Task 3: Add typing-provider fields to `ProviderConfig`

**Files:**
- Modify: `estimator_king/llm/config.py`
- Test: `tests/test_llm_config.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_llm_config.py`:

```python
from estimator_king.llm.config import ProviderConfig


def test_typing_fields_have_defaults():
    cfg = ProviderConfig(embedding_api_key="e", chat_api_key="c")
    assert cfg.typing_model == "gpt-4o-mini"
    assert cfg.typing_base_url is None
    assert cfg.typing_api_key == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_llm_config.py -v -o addopts=""`
Expected: FAIL — unexpected keyword/attribute `typing_model`.

- [ ] **Step 3: Implement**

In `estimator_king/llm/config.py`, add to the `ProviderConfig` dataclass after the Chat block:

```python
    # Typing (item-type classification; small/cheap model)
    typing_model: str = "gpt-4o-mini"
    typing_base_url: str | None = None   # cascade: typing → chat → openai
    typing_api_key: str = ""             # cascade: typing → chat → openai
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_llm_config.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Type-check, lint, commit**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king
git add estimator_king/llm/config.py tests/test_llm_config.py
git commit -m "feat(llm-config): add typing model/base_url/api_key to ProviderConfig"
```

---

## Task 4: `TypingProvider` with lazy OpenAI client

**Files:**
- Create: `estimator_king/llm/typing_provider.py`
- Test: `tests/test_typing_provider.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_typing_provider.py`:

```python
from estimator_king.llm.config import ProviderConfig
from estimator_king.llm.typing_provider import TypingProvider


def test_construct_with_empty_key_does_not_build_client():
    # Lazy: empty key must NOT raise at construction (crawl path safety).
    tp = TypingProvider(ProviderConfig(embedding_api_key="e", chat_api_key="", typing_api_key=""))
    assert tp._client is None  # client not built yet


def test_classify_via_llm_returns_item_type(monkeypatch):
    tp = TypingProvider(ProviderConfig(embedding_api_key="e", chat_api_key="c", typing_api_key="k"))

    class _Msg:
        content = '{"item_type": "ぬいぐるみ"}'

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kwargs):
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _FakeClient:
        chat = _Chat()

    # Inject a fake client so no network/key is needed.
    tp._client = _FakeClient()
    out = tp.classify_via_llm("もちもちぬいぐるみ", ["ぬいぐるみ", "タオル"])
    assert out == "ぬいぐるみ"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_typing_provider.py -v -o addopts=""`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement**

Create `estimator_king/llm/typing_provider.py`:

```python
"""Item-type classifier provider (small/cheap model). Lazy OpenAI client.

The client is built on first ``classify_via_llm`` call, not at construction, so
the crawl path (which may have no chat/typing key) never raises at startup.
Two-tier orchestration and caching live in ``estimator_king.sync.typing``; this
class is only the LLM wrapper.
"""

import json
import logging

from openai import OpenAI

from estimator_king.llm.config import ProviderConfig

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Role: You classify one Japanese merchandise item into exactly one category.\n\n"
    "# Goal\nPick the single best category for the given item text.\n\n"
    "<constraints>\n"
    "- Choose EXACTLY ONE value from this allowed list: {item_types}.\n"
    "- If none clearly fits, output \"その他\". Never invent a category outside the list.\n"
    "- Decide from the item name/description tokens; ignore talent names and event titles.\n"
    "</constraints>\n\n"
    "# Output\nReturn JSON only: {{\"item_type\": \"<one allowed value or その他>\"}}. No prose."
)


class TypingProvider:
    _config: ProviderConfig
    _client: OpenAI | None

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self._client = None  # lazy

    def _get_client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(
                api_key=self._config.typing_api_key,
                base_url=self._config.typing_base_url,
            )
        return self._client

    def classify_via_llm(self, text: str, item_types: list[str]) -> str:
        system = _SYSTEM_PROMPT.format(item_types=", ".join(item_types))
        response = self._get_client().chat.completions.create(
            model=self._config.typing_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or ""
        data = json.loads(content)
        return str(data.get("item_type", "その他"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_typing_provider.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Type-check, lint, commit**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king
git add estimator_king/llm/typing_provider.py tests/test_typing_provider.py
git commit -m "feat(llm): add TypingProvider with lazy OpenAI client and classify_via_llm"
```

---

## Task 5: SQLite schema + repository (item_types_version, type cache)

**Files:**
- Modify: `estimator_king/database/schema.sql`
- Modify: `estimator_king/database/repository.py`
- Test: `tests/test_repository_typing.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_repository_typing.py`:

```python
from datetime import datetime, timezone

from estimator_king.database.repository import ProductState, ProductStateRepository


def _repo() -> ProductStateRepository:
    repo = ProductStateRepository(":memory:")
    repo.open()
    return repo


def test_product_state_carries_item_types_version():
    repo = _repo()
    now = datetime.now(tz=timezone.utc)
    repo.upsert(ProductState(
        external_key="s:1", store_id="s", product_id="1", product_url="u",
        content_hash="h", normalizer_version=2, item_types_version=3,
        last_seen_in_sitemap_at=now, last_fetch_success_at=now,
    ))
    got = repo.get_by_external_key("s:1")
    assert got is not None and got.item_types_version == 3
    repo.close()


def test_type_cache_roundtrip_and_list_other():
    repo = _repo()
    assert repo.get_cached_type("hash-a") is None
    repo.put_cached_type("hash-a", "ぬいぐるみ", 1, text_sample="もちもちぬいぐるみ")
    repo.put_cached_type("hash-b", "その他", 1, text_sample="謎の物体")
    assert repo.get_cached_type("hash-a") == "ぬいぐるみ"
    # list_other_typed returns readable text samples (not hashes) for vocab review.
    assert repo.list_other_typed(10) == ["謎の物体"]
    repo.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_repository_typing.py -v -o addopts=""`
Expected: FAIL — `ProductState` has no `item_types_version`; no cache methods.

- [ ] **Step 3: Implement schema**

In `estimator_king/database/schema.sql`, add `item_types_version INTEGER` to the `products` table (after `normalizer_version`):

```sql
    content_hash   TEXT NOT NULL,
    normalizer_version INTEGER NOT NULL,
    item_types_version INTEGER,
```

Add the cache table at the end of the file. It stores the **normalized text sample** (not just the hash) so `list_other_typed` can surface human-readable items for vocabulary review (spec §6.4):

```sql
CREATE TABLE IF NOT EXISTS item_type_cache (
    text_hash          TEXT PRIMARY KEY,
    text_sample        TEXT NOT NULL,
    item_type          TEXT NOT NULL,
    item_types_version INTEGER NOT NULL,
    created_at         TEXT NOT NULL
);
```

- [ ] **Step 4: Implement repository changes**

In `estimator_king/database/repository.py`:

(a) Add field to `ProductState` (after `normalizer_version: int`):

```python
    item_types_version: int | None = None
```

(b) In `_ensure_schema`, add an idempotent migration after `executescript`:

```python
    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_read_schema_sql())
        # Idempotent additive migration for pre-existing databases: schema.sql uses
        # CREATE TABLE IF NOT EXISTS and will not add columns to an existing table.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(products)").fetchall()}
        if "item_types_version" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN item_types_version INTEGER")
```

(c) In `upsert`, add the column to INSERT, VALUES, ON CONFLICT, and the params tuple. Change the INSERT column list to include `item_types_version` (after `normalizer_version`), add a `?` to VALUES, add `item_types_version=excluded.item_types_version` to the ON CONFLICT SET, and add `int(state.item_types_version) if state.item_types_version is not None else None` to the params tuple right after `state.normalizer_version`:

```python
                INSERT INTO products (
                    external_key, store_id, product_id, product_url,
                    content_hash, normalizer_version, item_types_version,
                    last_seen_in_sitemap_at, last_fetch_success_at, last_indexed_at,
                    created_at, updated_at,
                    consecutive_failures, consecutive_sitemap_misses,
                    inactive, inactive_reason, inactive_since
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(external_key) DO UPDATE SET
                    store_id=excluded.store_id,
                    product_id=excluded.product_id,
                    product_url=COALESCE(excluded.product_url, products.product_url),
                    content_hash=excluded.content_hash,
                    normalizer_version=excluded.normalizer_version,
                    item_types_version=excluded.item_types_version,
                    last_seen_in_sitemap_at=COALESCE(excluded.last_seen_in_sitemap_at, products.last_seen_in_sitemap_at),
                    last_fetch_success_at=COALESCE(excluded.last_fetch_success_at, products.last_fetch_success_at),
                    last_indexed_at=COALESCE(excluded.last_indexed_at, products.last_indexed_at),
                    updated_at=excluded.updated_at,
                    consecutive_failures=excluded.consecutive_failures,
                    consecutive_sitemap_misses=excluded.consecutive_sitemap_misses,
                    inactive=excluded.inactive,
                    inactive_reason=excluded.inactive_reason,
                    inactive_since=excluded.inactive_since
```

And the params tuple (insert `item_types_version` right after `state.normalizer_version`):

```python
                (
                    state.external_key, state.store_id, state.product_id, state.product_url,
                    state.content_hash, state.normalizer_version,
                    int(state.item_types_version) if state.item_types_version is not None else None,
                    _dt_to_iso(state.last_seen_in_sitemap_at),
                    _dt_to_iso(state.last_fetch_success_at),
                    _dt_to_iso(state.last_indexed_at),
                    _dt_to_iso(state.created_at), _dt_to_iso(state.updated_at),
                    int(state.consecutive_failures), int(state.consecutive_sitemap_misses),
                    1 if state.inactive else 0, state.inactive_reason,
                    _dt_to_iso(state.inactive_since),
                ),
```

(d) In `_row_to_state`, read the new column (after `normalizer_version=...`):

```python
        item_types_version=(
            int(cast(int, row["item_types_version"]))
            if row["item_types_version"] is not None else None
        ),
```

(e) Add cache + list methods to `ProductStateRepository` (anywhere among the public methods):

```python
    def get_cached_type(self, text_hash: str) -> str | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT item_type FROM item_type_cache WHERE text_hash = ?",
                (text_hash,),
            ).fetchone()
            return None if row is None else str(row["item_type"])

    def put_cached_type(self, text_hash: str, item_type: str, version: int,
                        text_sample: str) -> None:
        with self._lock:
            _ = self.connection.execute(
                "INSERT INTO item_type_cache (text_hash, text_sample, item_type, item_types_version, created_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(text_hash) DO UPDATE SET"
                " text_sample=excluded.text_sample, item_type=excluded.item_type,"
                " item_types_version=excluded.item_types_version",
                (text_hash, text_sample, item_type, int(version), _dt_to_iso(_utc_now())),
            )

    def list_other_typed(self, limit: int) -> list[str]:
        """Distinct readable item texts classified as 'その他', for vocab review (§6.4)."""
        with self._lock:
            rows = self.connection.execute(
                "SELECT DISTINCT text_sample FROM item_type_cache WHERE item_type = 'その他'"
                " ORDER BY text_sample LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [str(r["text_sample"]) for r in rows]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_repository_typing.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 6: Type-check, lint, full repository test, commit**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king
.venv/bin/python -m pytest tests/test_repository.py tests/test_repository_typing.py -v -o addopts=""
git add estimator_king/database/schema.sql estimator_king/database/repository.py tests/test_repository_typing.py
git commit -m "feat(db): item_types_version column + idempotent migration; item_type_cache + helpers"
```

---

## Task 6: `AppConfig` structural fields + cascade

**Files:**
- Modify: `estimator_king/config_schema.py`
- Test: `tests/test_config_schema.py` (create — `tests/test_config.py` already exists with a different one-arg `_write_yaml`; a separate file avoids the helper-name collision)

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_schema.py`:

```python
import os
import textwrap

from estimator_king.config_schema import load_config


def _write_yaml(tmp_path, body: str) -> str:
    p = tmp_path / "stores.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(p)


def test_load_config_parses_typing_and_estimator_sections(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPING_MODEL", raising=False)
    monkeypatch.delenv("TYPING_API_KEY", raising=False)
    monkeypatch.delenv("TYPING_BASE_URL", raising=False)
    monkeypatch.delenv("CHAT_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    path = _write_yaml(tmp_path, """
        stores:
          - id: s
            base_url: https://x
            sitemap_url: https://x/sitemap.xml
        item_types: [ぬいぐるみ, タオル]
        item_types_version: 2
        talents: [博衣こより, 白銀ノエル]
        estimator:
          top_k: 7
          recency_weight: 0.1
    """)
    cfg = load_config(path)
    assert cfg.item_types == ["ぬいぐるみ", "タオル"]
    assert cfg.item_types_version == 2
    assert cfg.talents == frozenset({"博衣こより", "白銀ノエル"})
    assert cfg.estimator_top_k == 7
    assert cfg.estimator_recency_weight == 0.1
    # typing cascade: no TYPING_API_KEY/CHAT_API_KEY -> falls back to OPENAI_API_KEY
    pc = cfg.build_provider_config()
    assert pc.typing_api_key == "k"
    assert pc.typing_model == "gpt-4o-mini"


def test_load_config_defaults_when_sections_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    path = _write_yaml(tmp_path, """
        stores:
          - id: s
            base_url: https://x
            sitemap_url: https://x/sitemap.xml
    """)
    cfg = load_config(path)
    assert cfg.item_types == []
    assert cfg.item_types_version == 0
    assert cfg.talents == frozenset()
    assert cfg.estimator_top_k == 10
    assert cfg.estimator_recency_weight == 0.05
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_config_schema.py -v -o addopts=""`
Expected: FAIL — `AppConfig` lacks the new fields / not parsed.

- [ ] **Step 3: Implement — AppConfig fields**

In `estimator_king/config_schema.py`, add to `AppConfig` (after the `chroma_path` line):

```python
    # Item-type classification + retrieval tuning (structural, from YAML)
    item_types: List[str] = field(default_factory=list)
    item_types_version: int = 0
    talents: frozenset[str] = field(default_factory=frozenset)
    estimator_top_k: int = 10
    estimator_recency_weight: float = 0.05

    # Typing provider (credentials, from env)
    typing_model: str = "gpt-4o-mini"
    typing_base_url: str | None = None
    typing_api_key: str | None = None
```

- [ ] **Step 4: Implement — build_provider_config cascade**

In `build_provider_config`, add the typing args to the returned `ProviderConfig(...)`:

```python
        return ProviderConfig(
            embedding_api_key=emb_key,
            chat_api_key=chat_key,
            embedding_base_url=self.embedding_base_url or self.openai_base_url,
            embedding_model=self.embedding_model,
            embedding_dimensions=self.embedding_dimensions,
            embedding_max_tokens=self.embedding_max_tokens,
            embedding_query_prefix=self.embedding_query_prefix,
            embedding_doc_prefix=self.embedding_doc_prefix,
            chat_base_url=self.chat_base_url or self.openai_base_url,
            chat_model=self.chat_model,
            chat_structured_output=self.chat_structured_output,
            typing_model=self.typing_model,
            typing_base_url=self.typing_base_url or self.chat_base_url or self.openai_base_url,
            typing_api_key=self.typing_api_key or self.chat_api_key or self.openai_api_key or "",
        )
```

- [ ] **Step 5: Implement — load_config parsing**

In `load_config`, after the `proxy = ProxyConfig(...)` block, add:

```python
    est = yaml_data.get("estimator", {}) or {}
```

Then add these keyword args to the `config = AppConfig(...)` constructor call (after `database_path=...`):

```python
        item_types=list(yaml_data.get("item_types", []) or []),
        item_types_version=int(yaml_data.get("item_types_version", 0) or 0),
        talents=frozenset(yaml_data.get("talents", []) or []),
        estimator_top_k=int(est.get("top_k", 10)),
        estimator_recency_weight=float(est.get("recency_weight", 0.05)),
        typing_model=os.getenv("TYPING_MODEL", "gpt-4o-mini"),
        typing_base_url=os.getenv("TYPING_BASE_URL"),
        typing_api_key=os.getenv("TYPING_API_KEY"),
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_config_schema.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 7: Type-check, lint, commit**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king
git add estimator_king/config_schema.py tests/test_config_schema.py
git commit -m "feat(config): item_types/talents/estimator YAML fields + typing env cascade"
```

---

## Task 7: `items.py` — ProductItem + decompose_items

**Files:**
- Create: `estimator_king/sync/items.py`
- Test: `tests/test_items.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_items.py`:

```python
from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.sync.items import ProductItem, decompose_items

TALENTS = frozenset({"さくらみこ", "白上フブキ", "博衣こより"})


def _snap(title, variants, html_details=None, pid=1):
    return ProductSnapshot(
        product_id=pid, title=title, description="",
        variants=[ProductVariant(variant_id=i + 1, title=t, price=p)
                  for i, (t, p) in enumerate(variants)],
        html_details=html_details or {},
    )


def test_excludes_set_and_zero_price():
    snap = _snap("P", [
        ("セット / フルセット", "2000"),
        ("グッズ / 特典ステッカー", "0"),
        ("グッズ / アクリルスタンド", "500"),
    ])
    items = decompose_items(snap, talents=TALENTS)
    assert [i.item_name for i in items] == ["アクリルスタンド"]
    assert items[0].price_jpy == 500


def test_talent_variants_merge_to_product_title():
    snap = _snap("3Dアクリルスタンド Blue Journey衣装ver.", [
        ("グッズ / さくらみこ Blue Journey衣装ver.", "330"),
        ("グッズ / 白上フブキ Blue Journey衣装ver.", "330"),
    ])
    items = decompose_items(snap, talents=TALENTS)
    assert len(items) == 1
    assert items[0].item_name == "3Dアクリルスタンド Blue Journey衣装ver."
    assert items[0].price_jpy == 330
    assert len(items[0].source_variant_ids) == 2
    assert set(items[0].talents) == {"さくらみこ", "白上フブキ"}


def test_themed_series_not_merged_even_at_same_price():
    snap = _snap("生日記念", [
        ("グッズ / Start your Journey ポーチ", "440"),
        ("グッズ / Start your Journey プレート", "440"),
    ])
    items = decompose_items(snap, talents=TALENTS)
    names = sorted(i.item_name for i in items)
    # Codepoint sort: プ (U+30D7) < ポ (U+30DD).
    assert names == ["Start your Journey プレート", "Start your Journey ポーチ"]


def test_unparseable_price_variant_is_dropped():
    snap = _snap("P", [
        ("グッズ / 謎の値段", "N/A"),
        ("グッズ / アクリルスタンド", "500"),
    ])
    items = decompose_items(snap, talents=TALENTS)
    assert [i.item_name for i in items] == ["アクリルスタンド"]


def test_short_option_value_prepends_product_title():
    snap = _snap("ぶいすぽっ！オリジナルTシャツ", [
        ("バリエーション / 黒　M", "5500"),
        ("バリエーション / 白　L", "5500"),
    ])
    items = decompose_items(snap, talents=TALENTS)
    names = sorted(i.item_name for i in items)
    assert names == ["ぶいすぽっ！オリジナルTシャツ 白 L", "ぶいすぽっ！オリジナルTシャツ 黒 M"]


def test_detail_snippet_substring_match():
    snap = _snap("誕生日記念", [
        ("グッズ / Eternity アクリルジオラマスタンド", "995"),
        ("グッズ / イオフィカラー ショルダーバッグ", "600"),
    ], html_details={
        "グッズ詳細": (
            "◇記念グッズ ・Eternity アクリルジオラマスタンド サイズ：約H250×W150×D60mm 素材：アクリル"
            " ・イオフィカラー ショルダーバッグ サイズ：約H18.5×W13×D5cm 素材：ポリエステル"
        )
    })
    items = {i.item_name: i for i in decompose_items(snap, talents=TALENTS)}
    assert "H250" in items["Eternity アクリルジオラマスタンド"].detail_snippet
    assert "ポリエステル" in items["イオフィカラー ショルダーバッグ"].detail_snippet


def test_voice_item_has_no_snippet():
    snap = _snap("誕生日記念", [
        ("デジタルコンテンツ / シチュエーションボイス「君となら」", "140"),
    ], html_details={"グッズ詳細": "◇記念グッズ ・アクリルスタンド サイズ：H100"})
    items = decompose_items(snap, talents=TALENTS)
    assert items[0].detail_snippet == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_items.py -v -o addopts=""`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement**

Create `estimator_king/sync/items.py`:

```python
"""Decompose a ProductSnapshot into priceable items.

Pipeline per product: drop SET / ¥0 variants → talent-gated canonical-key dedup
→ name each item → best-effort spec-snippet extraction. published_at is carried
from the snapshot onto every item of that product.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections import defaultdict

from estimator_king.crawler.snapshot import ProductSnapshot, normalize_text

_SIZE_RE = re.compile(
    r"(^|[\s/])(XX?[SML]|[SML]|フリー)?サイズ|^(XX?[SML]|[SML])([\s/]|$)|フリーサイズ"
)
_SEGMENT_SPLIT = re.compile(r"[・◇\n]")


@dataclass(frozen=True)
class ProductItem:
    product_id: int
    product_title: str
    item_name: str
    price_jpy: int
    source_variant_ids: tuple[int, ...]
    talents: tuple[str, ...]
    detail_snippet: str
    published_at: int


def _strip_prefix(title: str) -> tuple[str, str | None]:
    """Return (residual, option_prefix) from a Shopify variant title 'X / Y'."""
    if " / " in title:
        prefix, rest = title.split(" / ", 1)
        return rest.strip(), prefix.strip()
    return title.strip(), None


def _price_to_int(price: str) -> int | None:
    try:
        return int(float(price))
    except (TypeError, ValueError):
        return None


def _meaningful_tokens(text: str) -> list[str]:
    return [t for t in normalize_text(text).split() if len(t) >= 2]


def _canonical_key(residual: str, talents: frozenset[str]) -> tuple[str, list[str]]:
    """Drop talent tokens; return (canonical_key, removed_talent_tokens)."""
    kept: list[str] = []
    removed: list[str] = []
    for tok in normalize_text(residual).split():
        if tok in talents:
            removed.append(tok)
        else:
            kept.append(tok)
    return " ".join(kept), removed


def _is_option_value(residual: str, product_title: str) -> bool:
    norm = normalize_text(residual)
    return len(norm) < 4 or bool(_SIZE_RE.search(norm))


def _extract_snippet(item_name: str, html_details: dict[str, str], talents: frozenset[str]) -> str:
    cores: list[str] = [normalize_text(item_name)]
    if " - " in item_name:
        cores.append(item_name.split(" - ")[0].strip())
        cores.append(item_name.split(" - ")[-1].strip())
    stripped = " ".join(t for t in normalize_text(item_name).split() if t not in talents)
    if stripped:
        cores.append(stripped)
    item_tokens = set(_meaningful_tokens(item_name))

    best = ""
    best_score = 0
    for text in html_details.values():
        for seg in _SEGMENT_SPLIT.split(text):
            seg = seg.strip()
            if not seg:
                continue
            score = 0
            for core in cores:
                if len(core) >= 4 and core in seg:
                    score = max(score, len(core))
            if score == 0:
                overlap = len(item_tokens & set(_meaningful_tokens(seg)))
                if overlap >= 2:
                    score = overlap
            if score > best_score:
                best_score = score
                best = seg
    return best


def decompose_items(snapshot: ProductSnapshot, *, talents: frozenset[str]) -> list[ProductItem]:
    # Step 1+2: keep non-SET, non-zero variants as (residual, price_int, variant_id).
    kept: list[tuple[str, int, int]] = []
    for v in snapshot.variants:
        residual, prefix = _strip_prefix(v.title)
        if prefix is not None and prefix.startswith("セット"):
            continue
        price = _price_to_int(v.price)
        if price is None or price == 0:
            continue
        kept.append((residual, price, v.variant_id))

    # Step 3: talent-gated canonical-key dedup, grouped by price.
    by_price: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for residual, price, vid in kept:
        by_price[price].append((residual, vid))

    @dataclass
    class _Item:
        residual: str | None  # None => whole-group merge (name from product title)
        price: int
        variant_ids: list[int]
        talents: list[str]

    raw_items: list[_Item] = []
    for price, members in by_price.items():
        groups: dict[str, list[tuple[str, int, list[str]]]] = defaultdict(list)
        for residual, vid in members:
            key, removed = _canonical_key(residual, talents)
            groups[key].append((residual, vid, removed))
        for key, group in groups.items():
            removed_any = any(r for _, _, r in group)
            if len(group) >= 2 and key.strip() and removed_any:
                merged_talents: list[str] = []
                for _, _, removed in group:
                    for t in removed:
                        if t not in merged_talents:
                            merged_talents.append(t)
                raw_items.append(_Item(
                    residual=None, price=price,
                    variant_ids=[vid for _, vid, _ in group], talents=merged_talents,
                ))
            else:
                for residual, vid, _ in group:
                    raw_items.append(_Item(residual=residual, price=price,
                                           variant_ids=[vid], talents=[]))

    # Step 4: naming (three branches) + snippet.
    whole_product_single = (
        len(raw_items) == 1 and raw_items[0].residual is None and len(raw_items[0].variant_ids) >= 2
    )
    items: list[ProductItem] = []
    for ri in raw_items:
        if ri.residual is None or whole_product_single:
            name = snapshot.title
        elif _is_option_value(ri.residual, snapshot.title):
            name = f"{snapshot.title} {normalize_text(ri.residual)}".strip()
        else:
            name = ri.residual
        items.append(ProductItem(
            product_id=snapshot.product_id,
            product_title=snapshot.title,
            item_name=name,
            price_jpy=ri.price,
            source_variant_ids=tuple(ri.variant_ids),
            talents=tuple(ri.talents),
            detail_snippet=_extract_snippet(name, snapshot.html_details, talents),
            published_at=snapshot.published_at,
        ))
    return items
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_items.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Type-check, lint, commit**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king
git add estimator_king/sync/items.py tests/test_items.py
git commit -m "feat(sync): decompose_items with talent-gated dedup, naming, snippet extraction"
```

---

## Task 8: `typing.py` — classify_item / classify_query orchestration

**Files:**
- Create: `estimator_king/sync/typing.py`
- Test: `tests/test_typing.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_typing.py`:

```python
from estimator_king.sync.typing import classify_item, classify_query

ITEM_TYPES = ["ぬいぐるみ", "キーホルダー", "ポーチ", "タオル"]


class FakeTypingProvider:
    def __init__(self, answer="ぬいぐるみ"):
        self.answer = answer
        self.calls = 0

    def classify_via_llm(self, text, item_types):
        self.calls += 1
        return self.answer


class FakeRepo:
    def __init__(self):
        self.store = {}

    def get_cached_type(self, h):
        return self.store.get(h)

    def put_cached_type(self, h, t, v, text_sample):
        self.store[h] = t


def test_single_vocab_hit_no_llm():
    tp = FakeTypingProvider()
    out = classify_item("もちもちぬいぐるみ", item_types=ITEM_TYPES,
                        item_types_version=1, typing_provider=tp, repository=FakeRepo())
    assert out == "ぬいぐるみ"
    assert tp.calls == 0


def test_classify_item_multi_hit_goes_to_llm():
    tp = FakeTypingProvider(answer="ぬいぐるみ")
    out = classify_item("ぬいぐるみポーチ", item_types=ITEM_TYPES,
                        item_types_version=1, typing_provider=tp, repository=FakeRepo())
    assert out == "ぬいぐるみ"
    assert tp.calls == 1  # multi-hit -> LLM picks one


def test_classify_item_zero_hit_llm_validates_to_sonota():
    tp = FakeTypingProvider(answer="存在しない型")
    out = classify_item("謎の物体", item_types=ITEM_TYPES,
                        item_types_version=1, typing_provider=tp, repository=FakeRepo())
    assert out == "その他"


def test_cache_hit_skips_llm():
    repo = FakeRepo()
    tp = FakeTypingProvider(answer="ぬいぐるみ")
    classify_item("謎の物体", item_types=ITEM_TYPES, item_types_version=1,
                  typing_provider=tp, repository=repo)
    classify_item("謎の物体", item_types=ITEM_TYPES, item_types_version=1,
                  typing_provider=tp, repository=repo)
    assert tp.calls == 1  # second call served from cache


def test_classify_query_multi_hit_keeps_all_no_llm():
    tp = FakeTypingProvider()
    out = classify_query("ぬいぐるみポーチ", item_types=ITEM_TYPES,
                         item_types_version=1, typing_provider=tp)
    assert set(out) == {"ぬいぐるみ", "ポーチ"}
    assert tp.calls == 0


def test_classify_query_sonota_returns_empty_list():
    tp = FakeTypingProvider(answer="その他")
    out = classify_query("謎の物体", item_types=ITEM_TYPES,
                         item_types_version=1, typing_provider=tp)
    assert out == []


def test_llm_exception_returns_sonota():
    class Boom:
        def classify_via_llm(self, text, item_types):
            raise RuntimeError("boom")

    out = classify_item("謎の物体", item_types=ITEM_TYPES, item_types_version=1,
                        typing_provider=Boom(), repository=None)
    assert out == "その他"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_typing.py -v -o addopts=""`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement**

Create `estimator_king/sync/typing.py`:

```python
"""Two-tier item-type classification orchestration.

Tier 1: controlled-vocabulary longest-substring match (zero LLM, deterministic).
Tier 2: small-model fallback (TypingProvider.classify_via_llm), with a SQLite
cache keyed on (normalized text, item_types_version). classify_item always
returns one type ('その他' floor); classify_query may return 0..N types.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Protocol

from estimator_king.crawler.snapshot import normalize_text

logger = logging.getLogger(__name__)

OTHER = "その他"


class _TypingProvider(Protocol):
    def classify_via_llm(self, text: str, item_types: list[str]) -> str: ...


class _Cache(Protocol):
    def get_cached_type(self, text_hash: str) -> str | None: ...
    def put_cached_type(self, text_hash: str, item_type: str, version: int,
                        text_sample: str) -> None: ...


def _vocab_hits(text: str, item_types: list[str]) -> list[str]:
    """Controlled-vocab substring matches, longest first."""
    norm = normalize_text(text)
    hits = [t for t in item_types if t and t in norm]
    hits.sort(key=len, reverse=True)
    return hits


def _cache_key(text: str, version: int) -> str:
    return hashlib.sha256(f"{normalize_text(text)}:{version}".encode("utf-8")).hexdigest()


def _llm_classify(
    text: str, item_types: list[str], version: int,
    typing_provider: _TypingProvider, repository: _Cache | None,
) -> str:
    key = _cache_key(text, version)
    if repository is not None:
        cached = repository.get_cached_type(key)
        if cached is not None:
            return cached
    try:
        result = typing_provider.classify_via_llm(text, item_types)
    except Exception:
        logger.exception("typing LLM classify failed; defaulting to %s", OTHER)
        result = OTHER
    if result not in item_types:
        result = OTHER
    if repository is not None:
        repository.put_cached_type(key, result, version, normalize_text(text))
    return result


def classify_item(
    text: str, *, item_types: list[str], item_types_version: int,
    typing_provider: _TypingProvider, repository: _Cache | None,
) -> str:
    hits = _vocab_hits(text, item_types)
    if len(hits) == 1:
        return hits[0]
    # zero or multiple hits -> LLM picks exactly one (cached on the index side).
    return _llm_classify(text, item_types, item_types_version, typing_provider, repository)


def classify_query(
    text: str, *, item_types: list[str], item_types_version: int,
    typing_provider: _TypingProvider, repository: _Cache | None = None,
) -> list[str]:
    hits = _vocab_hits(text, item_types)
    if hits:
        return hits  # one or many -> query each; no LLM
    result = _llm_classify(text, item_types, item_types_version, typing_provider, repository)
    return [] if result == OTHER else [result]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_typing.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Type-check, lint, commit**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king
git add estimator_king/sync/typing.py tests/test_typing.py
git commit -m "feat(sync): two-tier classify_item/classify_query with vocab match + cached LLM fallback"
```

---

## Task 9: `VectorStore.get_by_product`

**Files:**
- Modify: `estimator_king/vectorstore/store.py`
- Test: `tests/test_vectorstore_get_by_product.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_vectorstore_get_by_product.py`:

```python
from estimator_king.vectorstore.store import VectorStore


def test_get_by_product_filters_by_store_and_product(tmp_path):
    vs = VectorStore(str(tmp_path / "chroma"))
    emb = [0.1, 0.2, 0.3]
    vs.upsert("s:1:a", "doc a", emb, {"store_id": "s", "product_id": "1", "item_hash": "ha"})
    vs.upsert("s:1:b", "doc b", emb, {"store_id": "s", "product_id": "1", "item_hash": "hb"})
    vs.upsert("s:2:c", "doc c", emb, {"store_id": "s", "product_id": "2", "item_hash": "hc"})
    hits = vs.get_by_product("s", "1")
    assert sorted(h.id for h in hits) == ["s:1:a", "s:1:b"]
    assert {h.metadata["item_hash"] for h in hits} == {"ha", "hb"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_vectorstore_get_by_product.py -v -o addopts=""`
Expected: FAIL — `get_by_product` not defined.

- [ ] **Step 3: Implement**

In `estimator_king/vectorstore/store.py`, add a method to `VectorStore` (after `query`):

```python
    def get_by_product(self, store_id: str, product_id: str) -> list[QueryHit]:
        result = self._collection.get(
            where={"$and": [{"store_id": store_id}, {"product_id": product_id}]},
            include=["documents", "metadatas"],
        )
        ids = result.get("ids") or []
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        return [
            QueryHit(
                id=ids[i],
                document=(documents[i] if i < len(documents) else "") or "",
                metadata=dict(metadatas[i] or {}) if i < len(metadatas) else {},
                distance=0.0,
            )
            for i in range(len(ids))
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_vectorstore_get_by_product.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Type-check, lint, commit**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king
git add estimator_king/vectorstore/store.py tests/test_vectorstore_get_by_product.py
git commit -m "feat(vectorstore): add get_by_product to list a product's item vectors"
```

---

## Task 10: `sync_products` rewrite — per-item upsert, item_hash, stale delete, gating

**Files:**
- Modify: `estimator_king/sync/engine.py`
- Test: `tests/test_engine_items.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_engine_items.py`:

```python
from datetime import datetime, timezone

from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.database.repository import ProductState, ProductStateRepository
from estimator_king.sync.engine import sync_products

TALENTS = frozenset({"さくらみこ"})
ITEM_TYPES = ["アクリルスタンド", "ポーチ"]


class FakeEmbedder:
    def embed_documents(self, texts):
        return [[float(len(t)), 0.0, 0.0] for t in texts]


class FakeVectorStore:
    def __init__(self):
        self.docs = {}  # id -> (document, metadata)

    def upsert(self, id, document, embedding, metadata):
        self.docs[id] = (document, dict(metadata))

    def delete(self, ids):
        for i in ids:
            self.docs.pop(i, None)

    def get_by_product(self, store_id, product_id):
        from estimator_king.vectorstore.store import QueryHit
        return [
            QueryHit(id=i, document=d, metadata=m, distance=0.0)
            for i, (d, m) in self.docs.items()
            if m.get("store_id") == store_id and m.get("product_id") == product_id
        ]


class FakeTypingProvider:
    def classify_via_llm(self, text, item_types):
        return "その他"


def _snap():
    return ProductSnapshot(
        product_id=10, title="P", description="",
        variants=[
            ProductVariant(variant_id=1, title="グッズ / アクリルスタンド", price="500"),
            ProductVariant(variant_id=2, title="グッズ / 旅のポーチ", price="800"),
        ],
        html_details={},
    )


def _repo():
    repo = ProductStateRepository(":memory:")
    repo.open()
    return repo


def _sync(repo, vs, snap):
    return sync_products(
        [("http://x/products/10", snap)], "hololive", repo,
        FakeEmbedder(), vs,
        typing_provider=FakeTypingProvider(), talents=TALENTS,
        item_types=ITEM_TYPES, item_types_version=1,
    )


def test_creates_one_vector_per_item_with_own_price():
    repo, vs = _repo(), FakeVectorStore()
    _sync(repo, vs, _snap())
    prices = sorted(m["price_jpy"] for _, m in vs.docs.values())
    assert prices == [500, 800]
    assert all("item_type" in m for _, m in vs.docs.values())
    repo.close()


def test_unchanged_product_skips_reembed():
    repo, vs = _repo(), FakeVectorStore()
    _sync(repo, vs, _snap())
    first = {i: d for i, (d, _) in vs.docs.items()}
    # mark vectors so we detect re-writes
    for i in vs.docs:
        vs.docs[i] = (vs.docs[i][0] + "_orig", vs.docs[i][1])
    res = _sync(repo, vs, _snap())
    assert res.skipped >= 1
    # docs not overwritten (still carry the _orig marker)
    assert all(d.endswith("_orig") for d, _ in vs.docs.values())
    repo.close()


def test_stale_item_vector_deleted_when_variant_removed():
    repo, vs = _repo(), FakeVectorStore()
    _sync(repo, vs, _snap())
    assert len(vs.docs) == 2
    # Re-sync with one variant removed AND a content change (force rebuild)
    snap2 = ProductSnapshot(
        product_id=10, title="P2", description="",
        variants=[ProductVariant(variant_id=1, title="グッズ / アクリルスタンド", price="500")],
        html_details={},
    )
    _sync(repo, vs, snap2)
    assert len(vs.docs) == 1
    repo.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_engine_items.py -v -o addopts=""`
Expected: FAIL — `sync_products` has the old signature / product-level behavior.

- [ ] **Step 3: Implement — rewrite `engine.py`**

Replace the entire contents of `estimator_king/sync/engine.py` with:

```python
"""Sync engine: decompose products into items, classify, embed, and upsert one
vector per item. sync_products is the single writer of product rows on success.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Protocol

from estimator_king.crawler.snapshot import (
    NORMALIZER_VERSION,
    ProductSnapshot,
    compute_content_hash,
    normalize_text,
)
from estimator_king.database.repository import ProductState, ProductStateRepository
from estimator_king.sync.items import ProductItem, decompose_items
from estimator_king.sync.typing import classify_item

logger = logging.getLogger(__name__)


class _Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


class _VectorStoreHit(Protocol):
    id: str
    metadata: dict[str, object]


class _VectorStore(Protocol):
    def upsert(self, id: str, document: str, embedding: list[float],
               metadata: dict[str, object]) -> None: ...
    def delete(self, ids: list[str]) -> None: ...
    def get_by_product(self, store_id: str, product_id: str) -> list[_VectorStoreHit]: ...


class _TypingProvider(Protocol):
    def classify_via_llm(self, text: str, item_types: list[str]) -> str: ...


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    failed_ids: list[str] = field(default_factory=list)


def _item_slug(item_name: str) -> str:
    return hashlib.sha256(normalize_text(item_name).encode("utf-8")).hexdigest()[:16]


def _format_item_document(item: ProductItem, item_type: str) -> str:
    parts = [f"{item_type} {item.item_name}", "", f"# {item.product_title}"]
    if item.detail_snippet.strip():
        parts.extend(["", item.detail_snippet])
    return "\n".join(parts).rstrip()


def _item_hash(document: str, price_jpy: int, item_type: str) -> str:
    payload = f"{document}\x1f{price_jpy}\x1f{item_type}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sync_products(
    items: Iterable[tuple[str, ProductSnapshot]],
    store_id: str,
    repository: ProductStateRepository,
    embedder: _Embedder,
    vector_store: _VectorStore,
    *,
    typing_provider: _TypingProvider,
    talents: frozenset[str],
    item_types: list[str],
    item_types_version: int,
) -> SyncResult:
    result = SyncResult()
    for product_url, snapshot in items:
        now = datetime.now(tz=timezone.utc)
        external_key = f"{store_id}:{snapshot.product_id}"
        content_hash = compute_content_hash(snapshot)
        state = repository.get_by_external_key(external_key)

        seen_at = state.last_seen_in_sitemap_at if state else now
        sitemap_misses = state.consecutive_sitemap_misses if state else 0

        unchanged = (
            state is not None
            and state.content_hash == content_hash
            and state.normalizer_version == NORMALIZER_VERSION
            and state.item_types_version == item_types_version
            and state.last_indexed_at is not None
        )

        last_indexed_at = state.last_indexed_at if state else None
        try:
            if unchanged:
                result.skipped += 1
            else:
                _rebuild_product_items(
                    snapshot, store_id, product_url, repository, embedder,
                    vector_store, typing_provider, talents, item_types, item_types_version,
                )
                last_indexed_at = now
                if state is None:
                    result.created += 1
                else:
                    result.updated += 1
        except Exception:  # embedding/vector/typing failure: fire-and-forget
            logger.exception("Sync failed for %s", external_key)
            result.failed += 1
            result.failed_ids.append(external_key)

        repository.upsert(
            ProductState(
                external_key=external_key,
                store_id=store_id,
                product_id=str(snapshot.product_id),
                product_url=product_url,
                content_hash=content_hash,
                normalizer_version=NORMALIZER_VERSION,
                item_types_version=item_types_version,
                last_seen_in_sitemap_at=seen_at,
                last_fetch_success_at=now,
                last_indexed_at=last_indexed_at,
                consecutive_failures=0,
                consecutive_sitemap_misses=sitemap_misses,
            )
        )
    return result


def _rebuild_product_items(
    snapshot: ProductSnapshot,
    store_id: str,
    product_url: str,
    repository: ProductStateRepository,
    embedder: _Embedder,
    vector_store: _VectorStore,
    typing_provider: _TypingProvider,
    talents: frozenset[str],
    item_types: list[str],
    item_types_version: int,
) -> None:
    product_id = str(snapshot.product_id)
    existing = {h.id: str(h.metadata.get("item_hash", "")) for h in
                vector_store.get_by_product(store_id, product_id)}

    decomposed = decompose_items(snapshot, talents=talents)
    desired_ids: set[str] = set()
    for item in decomposed:
        item_type = classify_item(
            f"{item.item_name} {item.product_title}", item_types=item_types,
            item_types_version=item_types_version, typing_provider=typing_provider,
            repository=repository,
        )
        document = _format_item_document(item, item_type)
        item_hash = _item_hash(document, item.price_jpy, item_type)
        item_id = f"{store_id}:{product_id}:{_item_slug(item.item_name)}"
        desired_ids.add(item_id)
        if existing.get(item_id) == item_hash:
            continue  # unchanged item — skip re-embed
        embedding = embedder.embed_documents([document])[0]
        metadata: dict[str, object] = {
            "store_id": store_id,
            "product_id": product_id,
            "product_url": product_url,
            "product_title": item.product_title,
            "item_name": item.item_name,
            "item_type": item_type,
            "price_jpy": item.price_jpy,
            "published_at": item.published_at,
            "detail_snippet": item.detail_snippet,
            "item_hash": item_hash,
        }
        vector_store.upsert(item_id, document, embedding, metadata)

    stale = [vid for vid in existing if vid not in desired_ids]
    if stale:
        vector_store.delete(stale)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_engine_items.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Remove obsolete product-level tests**

`tests/test_sync_engine.py` and `tests/test_sync_engine_logging.py` test the **old** product-level `sync_products` and the removed `_format_product_document`/`_min_variant_price` — they no longer apply. The new behavior is covered by `tests/test_engine_items.py`. If you want to keep the "Sync failed for %s" exception-log assertion, port that one test into `test_engine_items.py` first.

```bash
git rm tests/test_sync_engine.py tests/test_sync_engine_logging.py
```

- [ ] **Step 6: Type-check, lint, commit**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king
git add estimator_king/sync/engine.py tests/test_engine_items.py
git commit -m "feat(sync): per-item vectors with own price/type/published_at, item_hash gating, stale delete"
```

---

## Task 11: Thread typing params through `async_process_queue`

**Files:**
- Modify: `estimator_king/crawler/async_pipeline.py`
- Test: covered by `tests/test_async_pipeline.py` (existing — update call) and Task 12.

- [ ] **Step 1: Update the failing call sites in tests (if present)**

Run the existing pipeline tests to see the new-signature failures:

Run: `.venv/bin/python -m pytest tests/test_async_pipeline.py -v -o addopts=""`
Expected: FAIL (after Task 10) — `sync_products` now requires keyword args.

- [ ] **Step 2: Implement**

In `estimator_king/crawler/async_pipeline.py`:

(a) Add to the `TYPE_CHECKING` block:

```python
    from estimator_king.llm.typing_provider import TypingProvider
```

(b) Add keyword params to `async_process_queue` (after `vector_store`):

```python
async def async_process_queue(
    store_id: str,
    policy: CrawlerPolicy,
    state_repo: ProductStateRepository,
    embedder: EmbeddingProvider,
    vector_store: VectorStore,
    *,
    typing_provider: "TypingProvider",
    talents: frozenset[str],
    item_types: list[str],
    item_types_version: int,
    proxy: ProxyConfig | None = None,
) -> PipelineResult:
```

(c) Pass them into the `sync_products` call inside `_handle`:

```python
                sync_result = await asyncio.to_thread(
                    lambda: sync_products(
                        [(product_url, snapshot)], store_id,
                        state_repo, embedder, vector_store,
                        typing_provider=typing_provider, talents=talents,
                        item_types=item_types, item_types_version=item_types_version,
                    )
                )
```

- [ ] **Step 3: Update existing test calls (per migration table) and run**

Add a module-level `FakeTypingProvider` (shared fake) to `tests/test_async_pipeline.py` and `tests/test_async_pipeline_logging.py`, then add the new keyword args (`typing_provider=FakeTypingProvider(), talents=frozenset(), item_types=[], item_types_version=0`) to **every** `async_process_queue(...)` call in both files.

Run: `.venv/bin/python -m pytest tests/test_async_pipeline.py tests/test_async_pipeline_logging.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 4: Type-check, lint, commit**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king
git add estimator_king/crawler/async_pipeline.py tests/test_async_pipeline.py tests/test_async_pipeline_logging.py
git commit -m "feat(crawler): thread typing_provider/talents/item_types through async_process_queue"
```

---

## Task 12: Thread `typing_provider` through `run_crawl_cycle`

**Files:**
- Modify: `estimator_king/crawler/cycle.py`
- Test: `tests/test_crawl_cycle.py`, `tests/test_integration_async_pipeline.py` (existing — update calls per the migration table)

- [ ] **Step 1: Implement**

In `estimator_king/crawler/cycle.py`:

(a) Add to `TYPE_CHECKING`:

```python
    from estimator_king.llm.typing_provider import TypingProvider
```

(b) Add `typing_provider` parameter to `run_crawl_cycle` (after `vector_store`, before the `*`):

```python
async def run_crawl_cycle(
    config: AppConfig,
    db_path: str,
    embedder: EmbeddingProvider,
    vector_store: VectorStore,
    typing_provider: "TypingProvider",
    *,
    force_refetch: bool = False,
) -> dict[str, int]:
```

(c) Pass typing args into `async_process_queue` (the values come from `config`):

```python
                    result = await async_process_queue(
                        store.id, config.crawler, repo, embedder, vector_store,
                        typing_provider=typing_provider,
                        talents=config.talents,
                        item_types=config.item_types,
                        item_types_version=config.item_types_version,
                        proxy=config.proxy)
```

- [ ] **Step 2: Update existing test calls (per migration table)**

Add a module-level `FakeTypingProvider` (the shared fake) and insert `FakeTypingProvider()` as the 5th positional arg into every `run_crawl_cycle(...)` call in `tests/test_crawl_cycle.py` (3 calls) and `tests/test_integration_async_pipeline.py` (3 calls), e.g. `run_crawl_cycle(cfg, db_path, embedder, vector_store, FakeTypingProvider(), force_refetch=...)`.

Run: `.venv/bin/python -m pytest tests/test_crawl_cycle.py tests/test_integration_async_pipeline.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 3: Type-check, lint, commit**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king
git add estimator_king/crawler/cycle.py tests/test_crawl_cycle.py tests/test_integration_async_pipeline.py
git commit -m "feat(crawler): pass typing_provider into run_crawl_cycle and down to async_process_queue"
```

---

## Task 13: `CrawlScheduler` accepts `typing_provider`

**Files:**
- Modify: `estimator_king/crawler/scheduler.py`
- Test: `tests/test_scheduler.py` (existing — add the new test below AND update the 4 existing `CrawlScheduler(...)` calls + `fake_cycle` signature per the migration table)

- [ ] **Step 1: Add the new failing test**

Append to `tests/test_scheduler.py`:

```python
import asyncio

from estimator_king.crawler.scheduler import CrawlScheduler


def test_scheduler_forwards_typing_provider(monkeypatch):
    captured = {}

    async def fake_cycle(config, db_path, embedder, vector_store, typing_provider, *, force_refetch=False):
        captured["typing_provider"] = typing_provider
        return {}

    monkeypatch.setattr("estimator_king.crawler.scheduler.run_crawl_cycle", fake_cycle)

    class C:
        class crawler:
            crawl_schedule_hours = 24.0

    sentinel = object()
    sched = CrawlScheduler(C(), ":memory:", embedder=object(), vector_store=object(),
                           typing_provider=sentinel)
    asyncio.run(sched.run_once())
    assert captured["typing_provider"] is sentinel
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_scheduler.py -v -o addopts=""`
Expected: FAIL — `__init__` has no `typing_provider`.

- [ ] **Step 3: Implement**

In `estimator_king/crawler/scheduler.py`:

(a) Add to `TYPE_CHECKING`:

```python
    from estimator_king.llm.typing_provider import TypingProvider
```

(b) Update `__init__` and `run_once`:

```python
    def __init__(self, config: AppConfig, db_path: str,
                 embedder: EmbeddingProvider, vector_store: VectorStore,
                 typing_provider: "TypingProvider") -> None:
        self._config = config
        self._db_path = db_path
        self._embedder = embedder
        self._vector_store = vector_store
        self._typing_provider = typing_provider
        self._running = False
```

In `run_once`, pass it through:

```python
            counters = await run_crawl_cycle(
                self._config, self._db_path, self._embedder, self._vector_store,
                self._typing_provider)
```

> The test passes `embedder=object()` / `vector_store=object()` as keyword args; keep `__init__` parameter names `embedder`, `vector_store`, `typing_provider`.

- [ ] **Step 3b: Update the 4 pre-existing tests (per migration table)**

In `tests/test_scheduler.py`, add `typing_provider=object()` to the 4 existing `CrawlScheduler(...)` constructions, and change the pre-existing `fake_cycle` (the one at the top of the file, not the new test's) to accept `typing_provider` positionally: `async def fake_cycle(config, db_path, embedder, vector_store, typing_provider, *, force_refetch=False)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_scheduler.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Type-check, lint, commit**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king
git add estimator_king/crawler/scheduler.py tests/test_scheduler.py
git commit -m "feat(crawler): CrawlScheduler accepts and forwards typing_provider"
```

---

## Task 14: `runtime.py` — Providers.typing, build_providers, serve wiring

**Files:**
- Modify: `estimator_king/runtime.py`
- Test: `tests/test_runtime.py` (existing — add cases)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_runtime.py`:

```python
from estimator_king.config_schema import AppConfig, Store
from estimator_king.runtime import build_providers


def _cfg(**kw):
    return AppConfig(
        stores=[Store(id="s", base_url="https://x", sitemap_url="https://x/s.xml")],
        embedding_api_key="e", **kw,
    )


def test_build_providers_includes_typing_and_does_not_raise_on_empty_typing_key():
    providers = build_providers(_cfg())  # no chat/typing key set
    assert providers.typing is not None
    assert providers.typing._client is None  # lazy: not built at construction
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_runtime.py -v -o addopts=""`
Expected: FAIL — `Providers` has no `typing`.

- [ ] **Step 3: Implement**

In `estimator_king/runtime.py`:

(a) Add import:

```python
from estimator_king.llm.typing_provider import TypingProvider
```

(b) Add field to `Providers`:

```python
@dataclass
class Providers:
    embedder: EmbeddingProvider
    vector_store: VectorStore
    typing: TypingProvider
    chat: Optional[ChatProvider] = None
```

(c) Build it (unconditionally — lazy client makes this safe) in `build_providers`:

```python
    embedder = EmbeddingProvider(provider_config)
    vector_store = VectorStore(config.chroma_path)
    typing = TypingProvider(provider_config)
    chat = ChatProvider(provider_config) if with_chat else None
    return Providers(embedder=embedder, vector_store=vector_store, typing=typing, chat=chat)
```

(d) Wire both serve paths. Update the `CrawlScheduler(...)` construction and the `build_bot(...)` call in `serve`:

```python
    scheduler = CrawlScheduler(
        config, config.database_path, providers.embedder, providers.vector_store,
        typing_provider=providers.typing)
```

```python
    bot = build_bot(
        config,
        embedder=providers.embedder,
        chat=providers.chat,
        vector_store=providers.vector_store,
        typing_provider=providers.typing,
        guild_id=guild_id,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_runtime.py -v -o addopts=""`
Expected: PASS.

> `build_bot` gains its `typing_provider` parameter in Task 17; if running tasks strictly in order, this serve call references a not-yet-added param. Implement Task 17's `build_bot` signature change together with this step if your tooling type-checks across modules before Task 17. (Subagent-driven execution applies tasks sequentially and runs the full suite at the end; the `serve` body is not exercised by Step 3's unit test.)

- [ ] **Step 5: Update existing `Providers(...)` constructions (per migration table)**

`Providers` now has a required `typing` field. Add `typing=MagicMock()` (import `from unittest.mock import MagicMock` where needed) to every `Providers(...)` construction in `tests/test_runtime.py`, `tests/test_main_async.py`, and `tests/test_cli.py`. Positional-index assertions on the patched `CrawlScheduler`/`run_crawl_cycle` mocks still hold (`vector_store` stays `args[3]`).

Run: `.venv/bin/python -m pytest tests/test_runtime.py tests/test_main_async.py tests/test_cli.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 6: Type-check, lint, commit**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king
git add estimator_king/runtime.py tests/test_runtime.py tests/test_main_async.py tests/test_cli.py
git commit -m "feat(runtime): add typing to Providers; wire typing_provider into scheduler and build_bot"
```

---

## Task 15: CLI `crawl` passes `providers.typing`

**Files:**
- Modify: `estimator_king/__main__.py`
- Test: `tests/test_cli.py` (existing — adjust if it asserts the call signature)

- [ ] **Step 1: Implement**

In `estimator_king/__main__.py`, update the `run_crawl_cycle(...)` call in `run_crawl`:

```python
        counters = asyncio.run(
            run_crawl_cycle(config, config.database_path,
                            providers.embedder, providers.vector_store,
                            providers.typing,
                            force_refetch=args.force_refetch))
```

- [ ] **Step 2: Run CLI + main tests**

`run_crawl` now passes `providers.typing` as the 5th positional arg. In `tests/test_main_async.py` / `tests/test_cli.py`, `run_crawl_cycle` is patched with a `MagicMock`, so the extra positional arg is absorbed; positional-index assertions (`args[1]` db_path, `args[2]` embedder, `args[3]` vector_store) are unchanged and still hold. The `Providers(...)` constructions in these files are already fixed in Task 14.

Run: `.venv/bin/python -m pytest tests/test_cli.py tests/test_main_async.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 3: Type-check, lint, commit**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king
git add estimator_king/__main__.py
git commit -m "feat(cli): pass providers.typing into run_crawl_cycle on the crawl command"
```

---

## Task 16: `estimator.py` — per-line retrieval, recency rerank, reconciliation, prompt

**Files:**
- Modify: `estimator_king/bot/estimator.py`
- Test: `tests/test_estimator.py` (replace/extend), `tests/test_estimator_logging.py` (fix constructor), `tests/test_e2e_mocked.py` (fix constructor + pre-seeded metadata) — per migration table

- [ ] **Step 1: Write the failing test**

Create/replace `tests/test_estimator.py`:

```python
from estimator_king.bot.estimator import Estimator
from estimator_king.llm.chat import EstimateBatch, ProductEstimate, PriceRange
from estimator_king.vectorstore.store import QueryHit


class FakeEmbedder:
    def embed_query(self, text):
        return [1.0, 0.0, 0.0]


class FakeTypingProvider:
    def __init__(self, answer="その他"):
        self.answer = answer

    def classify_via_llm(self, text, item_types):
        return self.answer


class RecordingVectorStore:
    def __init__(self, hits):
        self._hits = hits
        self.where_calls = []

    def query(self, embedding, n_results, where=None):
        self.where_calls.append(where)
        return list(self._hits)


def _hit(id, item_type, price, pub, dist):
    return QueryHit(id=id, document="", distance=dist, metadata={
        "item_name": id, "item_type": item_type, "price_jpy": price,
        "published_at": pub, "store_id": "s", "detail_snippet": ""})


class FakeChat:
    def __init__(self, estimates):
        self._estimates = estimates
        self.last_user_prompt = None

    def estimate(self, system_prompt, user_prompt):
        self.last_user_prompt = user_prompt
        return EstimateBatch(estimates=self._estimates)


def _est(name):
    return ProductEstimate(
        product_name=name, suggested_price_jpy=100,
        price_range_jpy=PriceRange(min=100, max=100), confidence="high",
        rationale="r", reference_products=[])


def _estimator(vs, chat, typing=None, top_k=10, recency=0.05):
    return Estimator(FakeEmbedder(), chat, vs, typing_provider=(typing or FakeTypingProvider()),
                     item_types=["ぬいぐるみ"], item_types_version=1,
                     top_k=top_k, recency_weight=recency)


def test_type_filtered_query_when_type_matched():
    vs = RecordingVectorStore([_hit("a", "ぬいぐるみ", 500, 0, 0.1)])
    chat = FakeChat([_est("もちもちぬいぐるみ")])
    est = _estimator(vs, chat)
    est.estimate_products(["もちもちぬいぐるみ"], "u")
    # one type-filtered query (where set) + one plain query (where None)
    assert {"item_type": "ぬいぐるみ"} in vs.where_calls
    assert None in vs.where_calls


def test_zero_type_only_plain_query():
    vs = RecordingVectorStore([_hit("a", "その他", 500, 0, 0.2)])
    chat = FakeChat([_est("謎の物体")])
    est = _estimator(vs, chat, typing=FakeTypingProvider("その他"))
    est.estimate_products(["謎の物体"], "u")
    assert vs.where_calls == [None]


def test_reconciliation_pads_missing_and_preserves_order():
    vs = RecordingVectorStore([_hit("a", "ぬいぐるみ", 500, 0, 0.1)])
    # chat returns only the second line, reordered
    chat = FakeChat([_est("line B")])
    est = _estimator(vs, chat)
    batch = est.estimate_products(["line A", "line B"], "u")
    assert [e.product_name for e in batch.estimates] == ["line A", "line B"]
    assert batch.estimates[0].confidence == "low"  # padded placeholder
    assert batch.estimates[0].suggested_price_jpy == 0


def test_recency_rerank_prefers_newer_when_similar():
    # two same-distance hits, different published_at; newer should sort first
    hits = [_hit("old", "ぬいぐるみ", 500, 1000, 0.1),
            _hit("new", "ぬいぐるみ", 900, 2000, 0.1)]
    vs = RecordingVectorStore(hits)
    chat = FakeChat([_est("もちもちぬいぐるみ")])
    est = _estimator(vs, chat, top_k=2, recency=0.5)
    est.estimate_products(["もちもちぬいぐるみ"], "u")
    # 'new' line should appear before 'old' line in the reference block
    prompt = chat.last_user_prompt
    assert prompt.index("new") < prompt.index("old")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -v -o addopts=""`
Expected: FAIL — `Estimator.__init__` lacks new params; no reconciliation/rerank.

- [ ] **Step 3: Implement — rewrite `estimator.py`**

Replace the entire contents of `estimator_king/bot/estimator.py` with:

```python
"""Price estimation: per-line type-aware retrieval + recency rerank, then ask the
chat model for structured estimates, reconciled back to the input lines."""

import logging
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Protocol

from estimator_king.crawler.snapshot import normalize_text
from estimator_king.llm.chat import EstimateBatch, PriceRange, ProductEstimate
from estimator_king.sync.typing import classify_query

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Role: You are the Estimator King, a price estimator for Japanese hololive/vspo "
    "merchandise. You price one item per input line using only the provided references.\n\n"
    "# Goal\nFor each product line, output a JPY price estimate grounded in the reference items.\n\n"
    "# Success criteria\n"
    "- One estimate per input line, in the same order; none skipped.\n"
    "- suggested_price and price_range are integer JPY justified by the references.\n"
    "- confidence reflects match quality (see constraints).\n\n"
    "<constraints>\n"
    "- Ground every estimate ONLY in the provided reference context; never invent prices "
    "or products not present in it.\n"
    "- Prefer references of the SAME item_type as the queried line; use cross-type references "
    "only as weak signal.\n"
    "- When references of comparable type span different dates, weight more RECENT prices "
    "higher (merchandise prices drift upward over time).\n"
    "- Match size/material using each reference's item_name and detail line when present.\n"
    "- Prices are integer JPY. Include up to 3 reference_products actually drawn from the context.\n"
    "</constraints>\n\n"
    "# Output\n"
    "Return an estimate object per line (product_name, suggested_price_jpy, price_range_jpy, "
    "confidence, rationale, reference_products). confidence: high = direct/near-exact same-type "
    "match; medium = same-type but size/variant differs; low = only cross-type or weak matches.\n\n"
    "<stop_rules>\n"
    "- If no strong match exists, still return an estimate with confidence \"low\" and a rationale "
    "stating the limitation — do NOT fabricate a closer match.\n"
    "</stop_rules>"
)


class _Embedder(Protocol):
    def embed_query(self, text: str) -> list[float]: ...


class _Chat(Protocol):
    def estimate(self, system_prompt: str, user_prompt: str) -> EstimateBatch: ...


class _TypingProvider(Protocol):
    def classify_via_llm(self, text: str, item_types: list[str]) -> str: ...


class _Hit(Protocol):
    id: str
    metadata: dict[str, Any]
    distance: float


class _VectorStore(Protocol):
    def query(self, embedding: list[float], n_results: int,
              where: dict[str, Any] | None = None) -> Sequence[_Hit]: ...


class Estimator:
    CHUNK_SIZE = 10

    def __init__(self, embedder: _Embedder, chat: _Chat, vector_store: _VectorStore,
                 typing_provider: _TypingProvider, *, item_types: list[str],
                 item_types_version: int, top_k: int = 10,
                 recency_weight: float = 0.05) -> None:
        self._embedder = embedder
        self._chat = chat
        self._vector_store = vector_store
        self._typing_provider = typing_provider
        self._item_types = item_types
        self._item_types_version = item_types_version
        self._top_k = top_k
        self._recency_weight = recency_weight

    def estimate_products(self, product_names: list[str], user_id: str) -> EstimateBatch:
        if not product_names:
            return EstimateBatch(estimates=[])
        logger.info("estimate request from %s for %d products", user_id, len(product_names))
        start = time.monotonic()
        total_chunks = (len(product_names) + self.CHUNK_SIZE - 1) // self.CHUNK_SIZE
        all_estimates: list[ProductEstimate] = []
        for start_idx in range(0, len(product_names), self.CHUNK_SIZE):
            chunk = product_names[start_idx:start_idx + self.CHUNK_SIZE]
            logger.debug("chunk %d/%d: %d products",
                         start_idx // self.CHUNK_SIZE + 1, total_chunks, len(chunk))
            batch = self._estimate_chunk(chunk)
            all_estimates.extend(batch.estimates)
        reconciled = self._reconcile(product_names, all_estimates)
        logger.info("estimate done for %s: %d estimates in %.1fs",
                    user_id, len(reconciled), time.monotonic() - start)
        return EstimateBatch(estimates=reconciled)

    def _estimate_chunk(self, chunk: list[str]) -> EstimateBatch:
        context_blocks: list[str] = []
        for name in chunk:
            embedding = self._embedder.embed_query(name)
            types = classify_query(
                name, item_types=self._item_types,
                item_types_version=self._item_types_version,
                typing_provider=self._typing_provider, repository=None,
            )
            merged: dict[str, _Hit] = {}
            queries: list[dict[str, Any] | None] = [{"item_type": t} for t in types]
            queries.append(None)  # always one plain query
            for where in queries:
                for hit in self._vector_store.query(embedding, self._top_k, where=where):
                    prev = merged.get(hit.id)
                    if prev is None or hit.distance < prev.distance:
                        merged[hit.id] = hit
            ranked = self._rerank(list(merged.values()))[: self._top_k]
            refs = "\n".join(self._format_reference(h) for h in ranked)
            context_blocks.append(f"### Query: {name}\n{refs or '(no matches)'}")
        user_prompt = (
            "Products to estimate (one per line):\n"
            + "\n".join(chunk)
            + "\n\nReference context:\n"
            + "\n\n".join(context_blocks)
        )
        return self._chat.estimate(SYSTEM_PROMPT, user_prompt)

    def _rerank(self, hits: list[_Hit]) -> list[_Hit]:
        pubs = [int(h.metadata.get("published_at", 0) or 0) for h in hits]
        positive = [p for p in pubs if p > 0]
        min_pub = min(positive) if positive else 0
        max_pub = max(positive) if positive else 0
        span = max_pub - min_pub

        def score(h: _Hit) -> float:
            similarity = 1.0 - h.distance
            pub = int(h.metadata.get("published_at", 0) or 0)
            if span > 0 and pub > 0:
                recency = (pub - min_pub) / span
            else:
                recency = 0.0
            return similarity + self._recency_weight * recency

        return sorted(hits, key=score, reverse=True)

    def _format_reference(self, hit: _Hit) -> str:
        m = hit.metadata
        pub = int(m.get("published_at", 0) or 0)
        date = "?" if pub == 0 else datetime.fromtimestamp(pub, tz=timezone.utc).strftime("%Y-%m")
        line = (f"- {m.get('item_name')} | {m.get('item_type')} | "
                f"¥{m.get('price_jpy')} | {date} | {m.get('store_id')}")
        snippet = str(m.get("detail_snippet", "") or "")
        if snippet:
            line += f"\n    {snippet[:120]}"
        return line

    def _reconcile(self, product_names: list[str],
                   estimates: list[ProductEstimate]) -> list[ProductEstimate]:
        by_name: dict[str, ProductEstimate] = {}
        for est in estimates:
            key = normalize_text(est.product_name)
            by_name.setdefault(key, est)
        matched_keys: set[str] = set()
        out: list[ProductEstimate] = []
        for line in product_names:
            key = normalize_text(line)
            est = by_name.get(key)
            if est is not None:
                matched_keys.add(key)
                out.append(est)
            else:
                out.append(ProductEstimate(
                    product_name=line, suggested_price_jpy=0,
                    price_range_jpy=PriceRange(min=0, max=0), confidence="low",
                    rationale="No estimate returned for this item.", reference_products=[]))
        surplus = len(estimates) - len(matched_keys)
        if surplus > 0:
            logger.warning("estimate reconciliation dropped %d unmatched estimate(s)", surplus)
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Update existing estimator tests (per migration table)**

- `tests/test_estimator_logging.py`: add the new `Estimator(...)` args (`FakeTypingProvider()`, `item_types=[...]`, `item_types_version=1`) to its construction. The rewritten `estimate_products` keeps the `chunk %d/%d: %d products` debug line, so `test_chunk_debug_and_done_info` still passes unchanged.
- `tests/test_e2e_mocked.py`: add the new `Estimator(...)` args to all 4 constructions (add a `FakeTypingProvider`); change the pre-seeded vector metadata in `test_estimate_products_with_pre_seeded_store` from `{title, price_jpy, store_id}` to `{item_name, item_type, price_jpy, published_at, store_id, detail_snippet}` and assert on the `item_name` value (the new `_format_reference` reads `item_name`, not `title`).

Run: `.venv/bin/python -m pytest tests/test_estimator_logging.py tests/test_e2e_mocked.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 6: Type-check, lint, commit**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king
git add estimator_king/bot/estimator.py tests/test_estimator.py tests/test_estimator_logging.py tests/test_e2e_mocked.py
git commit -m "feat(estimator): per-line type-aware retrieval, recency rerank, reconciliation, GPT-5.4 prompt"
```

---

## Task 17: `build_bot` injects typing_provider into Estimator

**Files:**
- Modify: `estimator_king/bot/runner.py`
- Test: none directly — no existing test constructs `build_bot` (verified). The serve-path wiring that calls `build_bot(..., typing_provider=...)` is covered by `tests/test_runtime.py` (Task 14, which patches `build_bot`). The `Estimator(...)` construction inside `build_bot` is exercised indirectly; its unit behavior is covered by Task 16's `tests/test_estimator.py`.

- [ ] **Step 1: Implement**

In `estimator_king/bot/runner.py`:

(a) Add to `TYPE_CHECKING`:

```python
    from estimator_king.llm.typing_provider import TypingProvider
```

(b) Add the parameter and use it in the `Estimator(...)` construction:

```python
def build_bot(
    config: AppConfig,
    *,
    embedder: "EmbeddingProvider",
    chat: "ChatProvider",
    vector_store: "VectorStore",
    typing_provider: "TypingProvider",
    guild_id: Optional[int],
) -> discord.Client:
    """Construct a fully-configured (but not yet started) Discord client: build
    the Estimator from injected providers, register commands, and wire the
    on_ready command-sync handler. The caller starts it via bot.start()."""
    from estimator_king.bot.estimator import Estimator

    estimator = Estimator(
        embedder, chat, vector_store, typing_provider,
        item_types=config.item_types, item_types_version=config.item_types_version,
        top_k=config.estimator_top_k, recency_weight=config.estimator_recency_weight)
```

- [ ] **Step 2: Run the runtime + runner-related tests**

Run: `.venv/bin/python -m pytest tests/test_runtime.py tests/test_runner_shutdown.py tests/test_runner_logging.py -v -o addopts=""`
Expected: PASS (no `build_bot(...)` call sites need editing — verified).

- [ ] **Step 3: Type-check, lint, commit**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king
git add estimator_king/bot/runner.py
git commit -m "feat(bot): build_bot injects typing_provider + config tuning into Estimator"
```

---

## Task 18: `format_estimates` — page-count + suffix fixes

**Files:**
- Modify: `estimator_king/bot/commands.py`
- Test: `tests/test_bot_commands.py` (existing — add cases)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_bot_commands.py`:

```python
from estimator_king.bot.commands import format_estimates
from estimator_king.llm.chat import EstimateBatch, ProductEstimate, PriceRange


def _est(name, rationale="r"):
    return ProductEstimate(
        product_name=name, suggested_price_jpy=100,
        price_range_jpy=PriceRange(min=100, max=100), confidence="high",
        rationale=rationale, reference_products=[])


def test_page_denominator_consistent_across_pages():
    batch = EstimateBatch(estimates=[_est(f"item {i}", rationale="x" * 250) for i in range(12)])
    embeds = format_estimates(batch, max_length=400)
    total = len(embeds)
    assert total >= 3
    for i, embed in enumerate(embeds, start=1):
        assert f"page {i}/{total}" in embed.title


def test_trailing_dash_in_rationale_not_stripped():
    batch = EstimateBatch(estimates=[_est("solo", rationale="ends with dash -")])
    embeds = format_estimates(batch, max_length=2000)
    assert "ends with dash -" in embeds[0].description
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bot_commands.py -v -o addopts=""`
Expected: FAIL — "page i/total" wrong; trailing dash stripped.

- [ ] **Step 3: Implement**

In `estimator_king/bot/commands.py`, replace the embed-assembly section of `format_estimates` (the part after `full_content = "".join(formatted_products)`) with a two-pass approach that builds page strings first, then titles them with the real total:

```python
    # Pass 1: pack product blocks into page-sized strings.
    pages: list[str] = []
    current_content = ""
    for product_block in formatted_products:
        test_content = current_content + product_block
        if len(test_content) > max_length and current_content:
            pages.append(current_content)
            current_content = product_block
        else:
            current_content = test_content
    if current_content:
        pages.append(current_content)

    # Pass 2: build embeds with a correct page/total denominator.
    total = len(pages)
    embeds = []
    for i, content in enumerate(pages, start=1):
        embed = discord.Embed(
            title=f"Price Estimates (page {i}/{total})",
            description=content.removesuffix("\n\n---\n\n"),
            color=discord.Color.blue(),
        )
        embeds.append(embed)

    return embeds
```

Remove the now-unused `full_content` line if it is no longer referenced (it was only used for the buggy denominator).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_bot_commands.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Type-check, lint, commit**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king
git add estimator_king/bot/commands.py tests/test_bot_commands.py
git commit -m "fix(bot): correct format_estimates page denominator and use removesuffix for separator"
```

---

## Task 19: Talent miner script + `stores_config.yaml` values

**Files:**
- Create: `scripts/mine_talents.py`
- Modify: `stores_config.yaml`
- Test: `tests/test_mine_talents.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_mine_talents.py`:

```python
from scripts.mine_talents import mine_talents


def test_mine_talents_returns_high_frequency_single_diff_tokens():
    # Two products, each a price-group whose variants differ by exactly one token.
    docs = [
        [("グッズ / さくらみこ 衣装ver.", 330.0), ("グッズ / 白上フブキ 衣装ver.", 330.0)],
        [("グッズ / さくらみこ 記念", 500.0), ("グッズ / 白上フブキ 記念", 500.0)],
    ]
    talents = mine_talents(docs, min_freq=2)
    assert "さくらみこ" in talents and "白上フブキ" in talents


def test_mine_talents_filters_version_noise():
    docs = [[("グッズ / A 数量限定ver.", 330.0), ("グッズ / B 数量限定ver.", 330.0)]]
    # 'A'/'B' too short/low-freq; '数量限定ver.' filtered by ver./限定 rule
    talents = mine_talents(docs, min_freq=1)
    assert "数量限定ver." not in talents
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_mine_talents.py -v -o addopts=""`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement**

Create `scripts/__init__.py` (empty) if it does not exist, then create `scripts/mine_talents.py`:

```python
"""One-time talent-seed miner. Reads the live ChromaDB 'products' collection,
finds tokens that vary as the single differing token within same-price variant
groups (these are reliably talent names), and prints a YAML 'talents:' list for
human review before adding to stores_config.yaml.

Usage: .venv/bin/python -m scripts.mine_talents [chroma_path]
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict


def mine_talents(
    docs: list[list[tuple[str, float]]], *, min_freq: int = 20
) -> set[str]:
    """docs: per-product list of (variant_title, price). Returns talent candidates."""
    counts: Counter[str] = Counter()
    for variants in docs:
        by_price: dict[float, list[str]] = defaultdict(list)
        for title, price in variants:
            residual = title.split(" / ", 1)[1].strip() if " / " in title else title.strip()
            by_price[price].append(residual)
        for residuals in by_price.values():
            if len(residuals) < 2:
                continue
            token_sets = [r.split() for r in residuals]
            common = set(token_sets[0])
            for ts in token_sets[1:]:
                common &= set(ts)
            for ts in token_sets:
                unique = [t for t in ts if t not in common]
                if len(unique) == 1:
                    counts[unique[0]] += 1
    return {
        tok for tok, freq in counts.items()
        if freq >= min_freq and "ver." not in tok and "限定" not in tok and not tok.isdigit()
    }


def _load_docs_from_chroma(path: str) -> list[list[tuple[str, float]]]:  # pragma: no cover
    import chromadb

    client = chromadb.PersistentClient(path=path)
    col = client.get_collection("products")
    res = col.get(include=["documents"])
    row_re = re.compile(r"^\|\s*(.+?)\s*\|\s*([\d.]+)\s*\|$")
    out: list[list[tuple[str, float]]] = []
    for doc in res["documents"]:
        variants: list[tuple[str, float]] = []
        for line in doc.splitlines():
            m = row_re.match(line)
            if m and m.group(1) != "Title" and set(m.group(1)) != {"-"}:
                variants.append((m.group(1).strip(), float(m.group(2))))
        if variants:
            out.append(variants)
    return out


def main() -> None:  # pragma: no cover
    path = sys.argv[1] if len(sys.argv) > 1 else "chroma"
    talents = sorted(mine_talents(_load_docs_from_chroma(path)))
    print("talents:")
    for t in talents:
        print(f"  - {t}")


if __name__ == "__main__":  # pragma: no cover
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_mine_talents.py -v -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Add config values to `stores_config.yaml`**

Append to `stores_config.yaml` (top level). Run the miner first to seed talents (the list below is a starter — extend from miner output after human review):

```yaml
item_types:
  - タペストリー
  - アクリルスタンド
  - アクリルキーホルダー
  - 缶バッジ
  - ぬいぐるみ
  - キーホルダー
  - ネックレス
  - ポーチ
  - ボイス
  - Tシャツ
  - タオル
item_types_version: 1

talents:
  - 博衣こより
  - 白銀ノエル
  - 尾丸ポルカ

estimator:
  top_k: 10
  recency_weight: 0.05
```

- [ ] **Step 6: Type-check, lint, commit**

```bash
.venv/bin/basedpyright estimator_king
uvx ruff check estimator_king scripts
git add scripts/mine_talents.py scripts/__init__.py stores_config.yaml tests/test_mine_talents.py
git commit -m "feat(scripts): talent-seed miner; add item_types/talents/estimator config"
```

---

## Task 20: Migration docs

**Files:**
- Modify: `CLAUDE.md`, `docs/local-runbook.md`, `docs/ops-runbook.md`

- [ ] **Step 1: Update CLAUDE.md Gotchas**

In `CLAUDE.md` under "Gotchas", extend the re-index note to cover the ID/format change:

```markdown
- **Re-index on indexing-model change**: vectors from different models/dimensions are incompatible, AND this build changed the vector ID scheme (one vector per *item*, not per product) and the embedding document format. Any of these requires `rm -rf chroma/` then `crawl --force-refetch`. The SQLite `products` table migrates additively (new `item_types_version` column via idempotent ALTER); bumping `item_types_version` in `stores_config.yaml` forces a full re-index on the next crawl.
```

- [ ] **Step 2: Update runbooks**

In `docs/local-runbook.md` and `docs/ops-runbook.md`, add a short "Re-index after upgrade" note:

```markdown
### Re-index after the item-level indexing upgrade

The vector ID scheme and document format changed (per-item vectors). After deploying:

```bash
rm -rf chroma/
.venv/bin/python -m estimator_king crawl --force-refetch
```

Changing `EMBEDDING_MODEL`/`EMBEDDING_DIMENSIONS` or bumping `item_types_version` in `stores_config.yaml` also triggers a re-index.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/local-runbook.md docs/ops-runbook.md
git commit -m "docs: note item-level re-index requirement and item_types_version bump"
```

---

## Task 21: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Type check (gate: 0 errors in production code)**

Run: `.venv/bin/basedpyright estimator_king`
Expected: 0 errors in `estimator_king/` (test files may show the known `reportArgumentType` noise from duck-typed fakes — that is existing convention).

- [ ] **Step 2: Lint**

Run: `uvx ruff check estimator_king scripts tests`
Expected: no errors.

- [ ] **Step 3: Full test suite with coverage**

Run: `.venv/bin/python -m pytest`
Expected: all tests pass.

- [ ] **Step 4: Operational verification (manual, requires keys + data)**

After `rm -rf chroma/ && crawl --force-refetch` against real data, run `/estimate` (or a scripted call to `Estimator.estimate_products`) with the §15 acceptance queries and confirm:
- `RIONA ON THE ステージタペストリー` → references dominated by `タペストリー` items.
- `リオナとおそろいネックレス` → `ネックレス` type-aligned (or graceful plain-embedding fallback).
- `くしゃみ連発ぬいキーホルダー` → `キーホルダー`/`ぬいぐるみ` references.
- Mixed-product items carry their own price (not product min).
- Blue Journey-style talent variants collapse to one item; themed series stay separate.
- N input lines → exactly N estimates in order; missing lines show `low`-confidence placeholders; multi-page output shows `page i/total`.

- [ ] **Step 5: Final commit (if any doc/cleanup remains)**

```bash
git status
# commit any remaining stray changes with a descriptive message
```

---

## Notes for the implementer

- **Test invocation:** always `-o addopts=""` for single-file runs (pytest.ini injects `--cov`; do not use `-p no:cov`).
- **Duck-typed fakes:** the codebase uses `Fake*` classes that satisfy Protocols structurally; basedpyright's `reportArgumentType` noise on those in test files is existing convention, not a regression. The 0-error gate applies to `estimator_king/` only.
- **`normalize_text`:** imported from `estimator_king.crawler.snapshot` by `items.py`, `typing.py`, and `estimator.py`. It must be public (Task 1).
- **Ordering caveat (Tasks 14/17):** `serve` references `build_bot(..., typing_provider=...)` (Task 14) before `build_bot` gains the parameter (Task 17). Apply Task 17's signature change in the same session; the full type check in Task 21 must pass with both in place.
