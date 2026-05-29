from estimator_king.bot.estimator import Estimator
from estimator_king.llm.chat import EstimateBatch, PriceRange, ProductEstimate
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
