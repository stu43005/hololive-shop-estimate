import asyncio
import logging

import pytest

from estimator_king.config_schema import Store
from estimator_king.crawler.pipeline import populate_queue_from_sitemap
from estimator_king.database.repository import ProductStateRepository


@pytest.fixture
def repo():
    with ProductStateRepository(":memory:") as r:
        yield r


def _store():
    return Store(id="hololive", base_url="https://x", sitemap_url="https://x/sitemap.xml")


class FakeEnumerator:
    def __init__(self, urls):
        self._urls = urls

    async def enumerate_products(self, base_url):
        return self._urls


def test_sitemap_summary_info_logged(repo, caplog):
    enum = FakeEnumerator(["https://x/products/1", "https://x/products/2"])
    with caplog.at_level(logging.INFO, logger="estimator_king.crawler.pipeline"):
        asyncio.run(populate_queue_from_sitemap(_store(), repo, enum))

    recs = [
        r for r in caplog.records
        if r.name == "estimator_king.crawler.pipeline" and r.levelno == logging.INFO
    ]
    assert any(
        "store=hololive" in r.getMessage()
        and "2 total" in r.getMessage()
        and "2 new enqueued" in r.getMessage()
        for r in recs
    )


def test_empty_sitemap_warns_and_skips_summary(repo, caplog):
    enum = FakeEnumerator([])
    with caplog.at_level(logging.INFO, logger="estimator_king.crawler.pipeline"):
        result = asyncio.run(populate_queue_from_sitemap(_store(), repo, enum))

    assert result == 0
    msgs = [r.getMessage() for r in caplog.records]
    assert any("returned 0 URLs" in m for m in msgs)
    assert not any("new enqueued" in m for m in msgs)
