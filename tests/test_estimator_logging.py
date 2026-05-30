import logging

from estimator_king.bot.estimator import Estimator
from estimator_king.llm.chat import EstimateBatch, PriceRange, ProductEstimate
from estimator_king.vectorstore.store import QueryHit


class FakeEmbedder:
    def embed_query(self, text):
        return [0.1, 0.2]


class FakeVectorStore:
    def query(self, embedding, n_results, where=None):
        return [QueryHit(
            id="hololive:1", document="doc",
            metadata={"title": "ref", "price_jpy": 2000, "store_id": "hololive"},
            distance=0.1,
        )]


class FakeChat:
    def estimate(self, system_prompt, user_prompt):
        return EstimateBatch(estimates=[ProductEstimate(
            product_name="p", suggested_price_jpy=2000,
            price_range_jpy=PriceRange(min=1800, max=2200),
            confidence="high", rationale="r", reference_products=[],
        )])


def test_chunk_debug_and_done_info(caplog):
    est = Estimator(FakeEmbedder(), FakeChat(), FakeVectorStore())
    est.CHUNK_SIZE = 1  # force two chunks

    with caplog.at_level(logging.DEBUG, logger="estimator_king.bot.estimator"):
        est.estimate_products(["a", "b"], "discord-1")

    recs = [r for r in caplog.records if r.name == "estimator_king.bot.estimator"]
    debug_msgs = [r.getMessage() for r in recs if r.levelno == logging.DEBUG]
    info_msgs = [r.getMessage() for r in recs if r.levelno == logging.INFO]

    assert any("chunk 1/2: 1 products" in m for m in debug_msgs)
    assert any("chunk 2/2: 1 products" in m for m in debug_msgs)
    assert any(
        "estimate done for discord-1" in m and "2 estimates" in m for m in info_msgs
    )
