# vspo Locale Sitemap Filter + crawl_queue Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Filter the shared Shopify sitemap enumerator down to a single per-store locale (default Japanese for vspo & hololive), and add a reusable maintenance script that purges the flooded `crawl_queue`.

**Architecture:** A pure `locale_of_url()` helper classifies any Shopify URL's locale segment. `SitemapEnumerator.enumerate_products()` takes a single `locale` argument and keeps only matching sitemaps (index level) and product URLs (URL level), cutting vspo from 402 sitemap fetches to 1. A new `Store.locale` config field (default `"default"`) feeds this through `populate_queue_from_sitemap`. A standalone `scripts/clean_crawl_queue.py` clears `crawl_queue` via existing repository methods.

**Tech Stack:** Python 3, stdlib `urllib.parse` / `argparse`, SQLite (`ProductStateRepository`), pytest. Type check `basedpyright`, lint `uvx ruff`.

---

## Verification commands (used throughout)

- Single test file: `.venv/bin/python -m pytest <path> -v -o addopts=""`
- Type check: `.venv/bin/basedpyright estimator_king/ scripts/` (production code must be 0 errors)
- Lint: `uvx ruff check estimator_king/ scripts/ tests/`
- Full suite: `.venv/bin/python -m pytest`

## File Structure

- **Modify** `estimator_king/crawler/sitemap.py` — add `DEFAULT_LOCALE`, `locale_of_url()`; rework `enumerate_products()` / `_extract_products_sitemaps()` to filter by a single locale.
- **Modify** `estimator_king/config_schema.py` — add `Store.locale` field, validation, and `from_yaml` parsing.
- **Modify** `estimator_king/crawler/pipeline.py` — pass `store.locale` into `enumerate_products()`.
- **Modify** `stores_config.yaml` — add explicit `locale: default` to both stores.
- **Create** `scripts/clean_crawl_queue.py` — maintenance script to purge `crawl_queue`.
- **Create** `docs/scripts/clean-crawl-queue.md` — usage doc.
- **Modify** `tests/test_sitemap.py` — `locale_of_url` unit tests + multi-locale enumerator tests.
- **Modify** `tests/test_config.py` — `Store.locale` default / validation / YAML parsing tests.
- **Modify** `tests/test_pipeline.py`, `tests/test_pipeline_logging.py` — update `FakeEnumerator` to accept `locale`.
- **Create** `tests/test_clean_crawl_queue.py` — script behaviour tests.

---

## Task 1: `locale_of_url` helper

**Files:**
- Modify: `estimator_king/crawler/sitemap.py`
- Test: `tests/test_sitemap.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_sitemap.py`. First extend the existing import at the top of the file (currently `from estimator_king.crawler.sitemap import (SitemapEnumerator, SitemapError, SitemapParseError,)`) to also import `DEFAULT_LOCALE` and `locale_of_url`:

```python
from estimator_king.crawler.sitemap import (
    DEFAULT_LOCALE,
    SitemapEnumerator,
    SitemapError,
    SitemapParseError,
    locale_of_url,
)
```

Then append this test class at the end of the file:

```python
class TestLocaleOfUrl:
    def test_default_product_url(self):
        assert locale_of_url("https://shop.example.com/products/x") == DEFAULT_LOCALE

    def test_default_sitemap_loc(self):
        assert locale_of_url("https://shop.example.com/sitemap_products_1.xml") == DEFAULT_LOCALE

    def test_default_sitemap_loc_with_query(self):
        assert locale_of_url(
            "https://shop.example.com/sitemap_products_1.xml?from=1&to=2"
        ) == DEFAULT_LOCALE

    def test_en_product_url(self):
        assert locale_of_url("https://shop.example.com/en/products/x") == "en"

    def test_ja_al_product_url(self):
        assert locale_of_url("https://store.vspo.jp/ja-al/products/x") == "ja-al"

    def test_en_dz_sitemap_loc(self):
        assert locale_of_url("https://store.vspo.jp/en-dz/sitemap_products_1.xml") == "en-dz"

    def test_uppercase_segment_normalized_to_lower(self):
        assert locale_of_url("https://shop.example.com/EN/products/x") == "en"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_sitemap.py::TestLocaleOfUrl -v -o addopts=""`
Expected: FAIL — `ImportError: cannot import name 'DEFAULT_LOCALE'` (or `locale_of_url`).

- [ ] **Step 3: Implement the helper**

In `estimator_king/crawler/sitemap.py`, change the import line `from urllib.parse import urljoin` to:

```python
from urllib.parse import urljoin, urlparse
```

Then add, immediately after the `SITEMAP_NS` constant (before `class SitemapError`):

```python
DEFAULT_LOCALE = "default"


def locale_of_url(url: str) -> str:
    """Return the locale segment of a Shopify store URL, or DEFAULT_LOCALE.

    Multi-locale Shopify stores prefix localized paths with a locale segment,
    e.g. ``/en/products/x`` or ``/ja-al/sitemap_products_1.xml``. Default-locale
    paths start directly with a structural segment (``products`` or
    ``sitemap_...``). The first path segment is therefore the locale unless it is
    one of those structural segments. The result is lowercased.
    """
    path = urlparse(url).path.lstrip("/")
    first = path.split("/", 1)[0].lower()
    if first == "products" or first.startswith("sitemap"):
        return DEFAULT_LOCALE
    return first
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_sitemap.py::TestLocaleOfUrl -v -o addopts=""`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add estimator_king/crawler/sitemap.py tests/test_sitemap.py
git commit -m "feat(crawler): add locale_of_url helper for Shopify URL locale detection"
```

---

## Task 2: enumerator filters by a single locale

**Files:**
- Modify: `estimator_king/crawler/sitemap.py`
- Test: `tests/test_sitemap.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sitemap.py`:

```python
_MULTILOCALE_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://shop.example.com/sitemap_products_1.xml</loc></sitemap>
  <sitemap><loc>https://shop.example.com/en/sitemap_products_1.xml</loc></sitemap>
  <sitemap><loc>https://shop.example.com/ja-al/sitemap_products_1.xml</loc></sitemap>
  <sitemap><loc>https://shop.example.com/en-dz/sitemap_products_1.xml</loc></sitemap>
  <sitemap><loc>https://shop.example.com/sitemap_pages_1.xml</loc></sitemap>
</sitemapindex>
"""

_DEFAULT_PRODUCTS = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://shop.example.com/products/jp-001</loc></url>
  <url><loc>https://shop.example.com/products/jp-002</loc></url>
</urlset>
"""

_EN_PRODUCTS = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://shop.example.com/en/products/en-001</loc></url>
</urlset>
"""


def _multilocale_router(url: str) -> str:
    if url.endswith("/sitemap.xml"):
        return _MULTILOCALE_INDEX
    if "/en/sitemap_products" in url:
        return _EN_PRODUCTS
    if "sitemap_products" in url:  # default (no prefix) and any other locale
        return _DEFAULT_PRODUCTS
    raise AssertionError(f"Unexpected URL: {url}")


class TestSitemapEnumeratorLocaleFiltering:
    def test_default_locale_returns_only_default_urls(self):
        client = FakeAsyncClient(_multilocale_router)
        enumerator = SitemapEnumerator(http_client=client)
        urls = asyncio.run(enumerator.enumerate_products("https://shop.example.com"))
        assert urls == [
            "https://shop.example.com/products/jp-001",
            "https://shop.example.com/products/jp-002",
        ]

    def test_index_level_skips_locale_sitemaps(self):
        client = FakeAsyncClient(_multilocale_router)
        enumerator = SitemapEnumerator(http_client=client)
        asyncio.run(enumerator.enumerate_products("https://shop.example.com"))
        assert any(
            u.endswith("/sitemap_products_1.xml") and "/en/" not in u
            and "/ja-al/" not in u and "/en-dz/" not in u
            for u in client.call_urls
        )
        assert not any("/en/sitemap_products" in u for u in client.call_urls)
        assert not any("/ja-al/sitemap_products" in u for u in client.call_urls)
        assert not any("/en-dz/sitemap_products" in u for u in client.call_urls)

    def test_custom_locale_returns_only_that_locale(self):
        client = FakeAsyncClient(_multilocale_router)
        enumerator = SitemapEnumerator(http_client=client)
        urls = asyncio.run(enumerator.enumerate_products("https://shop.example.com", "en"))
        assert urls == ["https://shop.example.com/en/products/en-001"]
        assert any("/en/sitemap_products" in u for u in client.call_urls)
        assert not any(
            u.endswith("/sitemap_products_1.xml") and "/en/" not in u
            for u in client.call_urls
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_sitemap.py::TestSitemapEnumeratorLocaleFiltering -v -o addopts=""`
Expected: FAIL — `test_index_level_skips_locale_sitemaps` fails because the current code fetches every locale sitemap; `test_custom_locale_returns_only_that_locale` fails because `enumerate_products` takes no `locale` argument (TypeError).

- [ ] **Step 3: Rework the enumerator**

In `estimator_king/crawler/sitemap.py`, replace the `enumerate_products` method body. The new version (note the new `locale` parameter, the lowercasing, and the two filter changes):

```python
    async def enumerate_products(
        self, base_url: str, locale: str = DEFAULT_LOCALE
    ) -> list[str]:
        """Enumerate all product URLs from a Shopify store for one locale.

        Args:
            base_url: Store base URL (e.g., "https://shop.example.com")
            locale: Locale to keep; ``DEFAULT_LOCALE`` (default) means the
                unprefixed default-language store. Compared case-insensitively.

        Returns:
            Sorted, deduplicated list of product URLs for the given locale.

        Raises:
            SitemapError: If sitemap parsing or fetching fails
        """
        locale = locale.lower()
        sitemap_index_url = urljoin(base_url, "/sitemap.xml")

        try:
            products_sitemap_urls = await self._extract_products_sitemaps(
                sitemap_index_url, locale
            )

            all_product_urls: set[str] = set()
            for sitemap_url in products_sitemap_urls:
                urls = await self._extract_product_urls(sitemap_url)
                all_product_urls.update(urls)

            filtered = [
                url for url in all_product_urls
                if "/products/" in url and locale_of_url(url) == locale
            ]
            return sorted(filtered)

        except (ET.ParseError, AsyncHTTPClientError) as e:
            raise SitemapError(
                f"Failed to enumerate products from {base_url}: {e}"
            ) from e
```

Then update `_extract_products_sitemaps` to take and apply `locale`. Replace its signature and the filter line:

```python
    async def _extract_products_sitemaps(
        self, sitemap_index_url: str, locale: str
    ) -> list[str]:
        """Extract products sitemap URLs for `locale` from the sitemapindex."""
        try:
            text = await self.http_client.get(sitemap_index_url)
            root = ET.fromstring(text)
        except ET.ParseError as e:
            raise SitemapParseError(f"Failed to parse sitemapindex: {e}") from e
        except AsyncHTTPClientError as e:
            raise SitemapParseError(f"Failed to fetch sitemapindex: {e}") from e

        products_urls: list[str] = []

        for sitemap_elem in root.findall("sitemap:sitemap", SITEMAP_NS):
            loc_elem = sitemap_elem.find("sitemap:loc", SITEMAP_NS)
            if loc_elem is not None and loc_elem.text:
                url = loc_elem.text.strip()
                if "products" in url and locale_of_url(url) == locale:
                    products_urls.append(url)

        return products_urls
```

Also update the class docstring of `SitemapEnumerator`: change the line `4. Filter out /en/ locale paths` to `4. Keep only the requested locale's sitemaps and product URLs`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_sitemap.py -v -o addopts=""`
Expected: PASS — the new `TestSitemapEnumeratorLocaleFiltering` tests pass AND all pre-existing tests still pass (the default-locale path keeps `/products/...` and excludes `/en/...`, so `test_enumerate_products_excludes_en_paths` and `test_enumerate_with_real_fixtures` remain green). The continued green state of `test_enumerate_products_excludes_en_paths` is what verifies acceptance criterion §5.2 (hololive behavior unchanged: default kept, `/en/` excluded).

- [ ] **Step 5: Commit**

```bash
git add estimator_king/crawler/sitemap.py tests/test_sitemap.py
git commit -m "feat(crawler): enumerate sitemap products for a single locale only"
```

---

## Task 3: `Store.locale` config field

**Files:**
- Modify: `estimator_king/config_schema.py`
- Modify: `stores_config.yaml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`. Inside the existing `class TestStore`, append:

```python
    def test_store_locale_defaults_to_default(self):
        store = Store(
            id="hololive",
            base_url="https://shop.hololivepro.com",
            sitemap_url="https://shop.hololivepro.com/sitemap.xml",
        )
        assert store.locale == "default"

    def test_store_validation_invalid_locale(self):
        store = Store(
            id="test",
            base_url="https://example.com",
            sitemap_url="https://example.com/sitemap.xml",
            locale="",
        )
        with pytest.raises(ValueError, match="must have a valid 'locale'"):
            store.validate()
```

Then add a module-level test pinning the `DEFAULT_LOCALE` invariant (spec §3.3: the dataclass default, the YAML, and `DEFAULT_LOCALE` must stay in sync). This catches drift if `DEFAULT_LOCALE` is ever changed:

```python
def test_store_locale_default_matches_default_locale_constant():
    from estimator_king.crawler.sitemap import DEFAULT_LOCALE

    store = Store(id="x", base_url="https://x", sitemap_url="https://x/s.xml")
    assert store.locale == DEFAULT_LOCALE
```

Then add a module-level test that `load_config` reads `locale` from YAML:

```python
@patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x"}, clear=False)
def test_load_config_reads_store_locale(tmp_path):
    path = tmp_path / "stores.yaml"
    path.write_text(
        "stores:\n"
        "  - id: vspo\n"
        "    base_url: https://store.vspo.jp\n"
        "    sitemap_url: https://store.vspo.jp/sitemap.xml\n"
        "    locale: default\n"
        "  - id: other\n"
        "    base_url: https://x\n"
        "    sitemap_url: https://x/sitemap.xml\n",
        encoding="utf-8",
    )
    cfg = load_config(str(path))
    assert cfg.stores[0].locale == "default"   # explicit
    assert cfg.stores[1].locale == "default"   # omitted → default
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_config.py -k "locale" -v -o addopts=""`
Expected: FAIL — `Store(...)` rejects the `locale=` kwarg (TypeError) and `store.locale` does not exist (AttributeError).

- [ ] **Step 3: Implement the config changes**

In `estimator_king/config_schema.py`, add the field to the `Store` dataclass (after `sitemap_url`):

```python
@dataclass
class Store:
    """Store configuration."""

    id: str
    base_url: str
    sitemap_url: str
    locale: str = "default"
```

Add the validation check inside `Store.validate()`, after the `sitemap_url` check:

```python
        if not self.locale or not isinstance(self.locale, str):
            raise ValueError(f"Store '{self.id}' must have a valid 'locale'")
```

Add the YAML parse in `AppConfig.from_yaml`, inside the `Store(...)` construction:

```python
    stores = [
        Store(
            id=s["id"],
            base_url=s["base_url"],
            sitemap_url=s["sitemap_url"],
            locale=s.get("locale", "default"),
        )
        for s in stores_data
    ]
```

- [ ] **Step 4: Edit `stores_config.yaml`**

Add an explicit `locale` line to each store:

```yaml
stores:
  - id: hololive
    base_url: https://shop.hololivepro.com
    sitemap_url: https://shop.hololivepro.com/sitemap.xml
    # 只抓預設語系（無語系前綴）；排除 /en/ 等所有語系版本
    locale: default

  - id: vspo
    base_url: https://store.vspo.jp
    sitemap_url: https://store.vspo.jp/sitemap.xml
    # 只抓預設日文（無語系前綴）；排除 /en/、/en-al/、/ja-al/ 等所有語系版本
    locale: default
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_config.py -v -o addopts=""`
Expected: PASS (all config tests, including the new locale ones).

- [ ] **Step 6: Commit**

```bash
git add estimator_king/config_schema.py stores_config.yaml tests/test_config.py
git commit -m "feat(config): add per-store locale (default 'default')"
```

---

## Task 4: wire `store.locale` through the pipeline

**Files:**
- Modify: `estimator_king/crawler/pipeline.py:38`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_pipeline_logging.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_pipeline.py`, add a test that asserts `populate_queue_from_sitemap` forwards `store.locale` to the enumerator. First, replace the existing `FakeEnumerator` class (lines 48-53) with a version that records the locale and accepts it:

```python
class FakeEnumerator:
    def __init__(self, urls):
        self._urls = urls
        self.received_locale = None

    async def enumerate_products(self, base_url, locale="default"):
        self.received_locale = locale
        return self._urls
```

Then add:

```python
def test_populate_passes_store_locale_to_enumerator(repo):
    store = Store(id="hololive", base_url="https://x",
                  sitemap_url="https://x/sitemap.xml", locale="en")
    enum = FakeEnumerator(["https://x/en/products/1"])

    asyncio.run(populate_queue_from_sitemap(store, repo, enum))

    assert enum.received_locale == "en"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py::test_populate_passes_store_locale_to_enumerator -v -o addopts=""`
Expected: FAIL — `received_locale` is still `"default"` because `populate_queue_from_sitemap` calls `enumerate_products(store.base_url)` without the locale.

- [ ] **Step 3: Implement the wiring**

In `estimator_king/crawler/pipeline.py`, change line 38 from:

```python
    sitemap_urls = await enumerator.enumerate_products(store.base_url)
```

to:

```python
    sitemap_urls = await enumerator.enumerate_products(store.base_url, store.locale)
```

- [ ] **Step 4: Update the other fake**

In `tests/test_pipeline_logging.py`, replace the `FakeEnumerator` class (lines 20-25) with:

```python
class FakeEnumerator:
    def __init__(self, urls):
        self._urls = urls

    async def enumerate_products(self, base_url, locale="default"):
        return self._urls
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py tests/test_pipeline_logging.py -v -o addopts=""`
Expected: PASS (all pipeline tests, including the new locale-forwarding test).

- [ ] **Step 6: Commit**

```bash
git add estimator_king/crawler/pipeline.py tests/test_pipeline.py tests/test_pipeline_logging.py
git commit -m "feat(crawler): pass store locale into sitemap enumeration"
```

---

## Task 5: `clean_crawl_queue` maintenance script

**Files:**
- Create: `scripts/clean_crawl_queue.py`
- Test: `tests/test_clean_crawl_queue.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_clean_crawl_queue.py`:

```python
from pathlib import Path

import pytest

from estimator_king.database.repository import ProductStateRepository
from scripts.clean_crawl_queue import clean


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "state.db")


def _seed(db_path: str) -> None:
    with ProductStateRepository(db_path) as repo:
        repo.enqueue_url("vspo", "https://store.vspo.jp/products/a")
        repo.enqueue_url("vspo", "https://store.vspo.jp/en/products/a")
        repo.enqueue_url("hololive", "https://shop.hololivepro.com/products/b")


def test_clean_purges_all_by_default(db_path: str) -> None:
    _seed(db_path)
    before, deleted = clean(db_path)
    assert before == 3
    assert deleted == 3
    with ProductStateRepository(db_path) as repo:
        assert repo.queue_size() == 0


def test_clean_dry_run_keeps_queue(db_path: str) -> None:
    _seed(db_path)
    before, deleted = clean(db_path, dry_run=True)
    assert before == 3
    assert deleted == 0
    with ProductStateRepository(db_path) as repo:
        assert repo.queue_size() == 3


def test_clean_store_scope(db_path: str) -> None:
    _seed(db_path)
    before, deleted = clean(db_path, store_id="vspo")
    assert before == 2
    assert deleted == 2
    with ProductStateRepository(db_path) as repo:
        assert repo.queue_size() == 1
        assert repo.queue_size("hololive") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_clean_crawl_queue.py -v -o addopts=""`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.clean_crawl_queue'`.

- [ ] **Step 3: Implement the script**

Create `scripts/clean_crawl_queue.py`:

```python
"""Maintenance script: purge the crawl queue.

``crawl_queue`` is a work-to-do queue, not authoritative state — clearing it
loses no data. Product rows self-heal on the next normal crawl (the stored
``product_url`` is rewritten when the default-locale URL is fetched again). Use
this to clear a queue that has been flooded (e.g. by a sitemap-locale-filter
bug) before re-crawling.

Run with the bot stopped (single DB writer).

Usage (either form works):
    .venv/bin/python -m scripts.clean_crawl_queue [--db PATH] [--store STORE_ID] [--dry-run]
    .venv/bin/python scripts/clean_crawl_queue.py [--db PATH] [--store STORE_ID] [--dry-run]

--db falls back to $DATABASE_PATH, then ./estimator_king.db.
"""

from __future__ import annotations

import argparse
import os
import sys

# Make `estimator_king` importable when run as a plain script: `python
# scripts/x.py` puts scripts/ on sys.path[0], not the repo root. Running via
# `-m` already adds the cwd, so this insert is a harmless no-op there.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from estimator_king.database.repository import ProductStateRepository  # noqa: E402


def clean(
    db_path: str, *, store_id: str | None = None, dry_run: bool = False
) -> tuple[int, int]:
    """Clear crawl_queue (optionally scoped to one store).

    Returns (queue_size_before, rows_deleted). On dry-run, rows_deleted is 0 and
    the queue is left untouched.
    """
    with ProductStateRepository(db_path) as repo:
        before = repo.queue_size(store_id)
        if dry_run:
            return before, 0
        deleted = repo.clear_queue(store_id)
        return before, deleted


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="clean_crawl_queue",
        description="Purge the crawl_queue (run with the bot stopped).",
    )
    parser.add_argument(
        "--db", default=None,
        help="SQLite path (default: $DATABASE_PATH, then ./estimator_king.db)",
    )
    parser.add_argument(
        "--store", default=None,
        help="Only clear this store_id (default: all stores)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report counts without deleting",
    )
    args = parser.parse_args(argv[1:])

    db_path = args.db or os.environ.get("DATABASE_PATH", "./estimator_king.db")
    before, deleted = clean(db_path, store_id=args.store, dry_run=args.dry_run)

    scope = f"store={args.store}" if args.store else "all stores"
    if args.dry_run:
        print(f"crawl_queue rows ({scope}): {before} (dry-run, nothing deleted)")
    else:
        print(f"crawl_queue rows ({scope}) before: {before}")
        print(f"crawl_queue rows deleted: {deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_clean_crawl_queue.py -v -o addopts=""`
Expected: PASS (3 passed).

- [ ] **Step 5: Verify both invocation forms manually**

Run (module form): `.venv/bin/python -m scripts.clean_crawl_queue --db /tmp/nonexistent-clean-test.db --dry-run`
Expected: prints `crawl_queue rows (all stores): 0 (dry-run, nothing deleted)` and exits 0 (a missing DB path is created empty by the repository).

Run (plain-script form — exercises the `sys.path` injection branch): `.venv/bin/python scripts/clean_crawl_queue.py --db /tmp/nonexistent-clean-test.db --dry-run`
Expected: same output as the module form, exit 0.

Run: `rm -f /tmp/nonexistent-clean-test.db`
Expected: cleans up the throwaway DB.

- [ ] **Step 6: Commit**

```bash
git add scripts/clean_crawl_queue.py tests/test_clean_crawl_queue.py
git commit -m "feat(scripts): add reusable crawl_queue purge maintenance script"
```

---

## Task 6: usage documentation

**Files:**
- Create: `docs/scripts/clean-crawl-queue.md`

- [ ] **Step 1: Write the doc**

Create `docs/scripts/clean-crawl-queue.md`:

```markdown
# clean_crawl_queue

清空 `crawl_queue` 待辦佇列的維護腳本。

## 用途

`crawl_queue` 是「待抓取」工作佇列，不是權威狀態。清空它**不會遺失資料**：product
狀態列保存在 `products` 表，下次正常 crawl 會重新從 sitemap 填回佇列，並讓
`product_url` 等欄位自然 self-heal。

典型使用時機：

- 佇列被異常灌爆（例如 sitemap 語系過濾 bug，把大量非預設語系 URL 塞進佇列）。
- 修正 sitemap 語系過濾後，要清掉殘留的舊語系 URL，避免它們在下次 drain 被抓取。

## 前置條件

**先停止 bot**。資料庫為單一寫入者（WAL，但並發寫入會 `database is locked`），
腳本執行期間不能有其他程序在寫 DB。

## 用法

```bash
.venv/bin/python -m scripts.clean_crawl_queue [--db PATH] [--store STORE_ID] [--dry-run]
# 或
.venv/bin/python scripts/clean_crawl_queue.py [--db PATH] [--store STORE_ID] [--dry-run]
```

### 參數

| 參數 | 說明 |
| --- | --- |
| `--db PATH` | SQLite 路徑。省略時依序取 `$DATABASE_PATH`、再 fallback `./estimator_king.db`。 |
| `--store STORE_ID` | 只清指定 store 的佇列列。省略則清空所有 store。 |
| `--dry-run` | 只回報將被刪除的列數，不實際刪除。 |

## 範例

預覽（不刪除）：

```bash
.venv/bin/python -m scripts.clean_crawl_queue --dry-run
# crawl_queue rows (all stores): 1234 (dry-run, nothing deleted)
```

全部清空：

```bash
.venv/bin/python -m scripts.clean_crawl_queue
# crawl_queue rows (all stores) before: 1234
# crawl_queue rows deleted: 1234
```

只清單一 store：

```bash
.venv/bin/python -m scripts.clean_crawl_queue --store vspo
# crawl_queue rows (store=vspo) before: 1200
# crawl_queue rows deleted: 1200
```

## 與 sitemap 語系過濾的關係

此腳本只負責清佇列，**不會**改變 sitemap 過濾行為。正確順序是：先部署單一語系過濾
修正（`stores_config.yaml` 的 `locale` 設定 + enumerator 過濾），再執行本腳本清掉
舊佇列；否則下次 crawl 會用舊邏輯把佇列再次灌爆。
```

- [ ] **Step 2: Commit**

```bash
git add docs/scripts/clean-crawl-queue.md
git commit -m "docs(scripts): document clean_crawl_queue maintenance script"
```

---

## Task 7: full verification

**Files:** none (verification only)

- [ ] **Step 1: Type check**

Run: `.venv/bin/basedpyright estimator_king/ scripts/`
Expected: 0 errors in production code. (Pre-existing test-file `reportArgumentType` noise from duck-typed fakes is existing convention, not introduced here — production paths `estimator_king/` and `scripts/` must be clean.)

- [ ] **Step 2: Lint**

Run: `uvx ruff check estimator_king/ scripts/ tests/`
Expected: no errors.

- [ ] **Step 3: Full test suite**

Run: `.venv/bin/python -m pytest`
Expected: all tests pass.

- [ ] **Step 4: Confirm vspo filter against the live sitemap (operational check)**

Run:
```bash
.venv/bin/python -c "
import asyncio
from estimator_king.config_schema import AppConfig
from estimator_king.crawler.async_http_client import AsyncHTTPClient
from estimator_king.crawler.sitemap import SitemapEnumerator, locale_of_url

cfg = AppConfig.from_yaml('stores_config.yaml')
vspo = next(s for s in cfg.stores if s.id == 'vspo')

async def main():
    async with AsyncHTTPClient(cfg.crawler, proxy=cfg.proxy) as c:
        urls = await SitemapEnumerator(c).enumerate_products(vspo.base_url, vspo.locale)
        print('count:', len(urls))
        print('all expected-locale:', all(locale_of_url(u) == vspo.locale.lower() for u in urls))
        print('sample:', urls[:3])

asyncio.run(main())
"
```
Expected: prints a non-zero `count`, `all expected-locale: True`, and sample URLs of the form `https://store.vspo.jp/products/...` (no locale prefix). The check uses the project's own `locale_of_url`, so it flags any leaked locale (`en`, `ja-al`, `ja-dz`, …), not just `/en/`. This is a real network call against the live store; if the network is unavailable, note that and rely on the unit tests instead.

---

## Self-Review notes (already applied)

- **Spec coverage:** §3.1 → Task 1; §3.2 → Task 2; §3.3 (config + YAML) → Task 3; data-flow wiring → Task 4; §3.4 script → Task 5; §3.5 docs → Task 6; §4.3 verification → Task 7. §2 non-goal (no products/ChromaDB cleanup, queue-only purge) is honored — no task touches `products`/ChromaDB.
- **Backward compatibility:** `enumerate_products` keeps a defaulted `locale`, and `Store.locale` defaults to `"default"`, so pre-existing tests and the cycle/integration tests (which mock `populate_queue_from_sitemap`) stay green.
- **Type/name consistency:** `DEFAULT_LOCALE`, `locale_of_url`, `enumerate_products(base_url, locale)`, `Store.locale`, `clean(db_path, *, store_id, dry_run)` are used identically across all tasks. `repo.queue_size(store_id)` and `repo.clear_queue(store_id)` match the existing repository signatures.
