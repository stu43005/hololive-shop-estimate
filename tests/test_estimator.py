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
    chat = FakeChat([_est("line B")])
    est = _estimator(vs, chat)
    batch = est.estimate_products(["line A", "line B"], "u")
    assert [e.product_name for e in batch.estimates] == ["line A", "line B"]
    assert batch.estimates[0].confidence == "low"
    assert batch.estimates[0].suggested_price_jpy == 0


def test_recency_rerank_prefers_newer_when_similar():
    hits = [_hit("old", "ぬいぐるみ", 500, 1000, 0.1),
            _hit("new", "ぬいぐるみ", 900, 2000, 0.1)]
    vs = RecordingVectorStore(hits)
    chat = FakeChat([_est("もちもちぬいぐるみ")])
    est = _estimator(vs, chat, top_k=2, recency=0.5)
    est.estimate_products(["もちもちぬいぐるみ"], "u")
    prompt = chat.last_user_prompt
    assert prompt.index("new") < prompt.index("old")


def test_recency_boundary_zero_pub_excluded_and_single_pub_degenerates():
    hits = [_hit("nodate", "ぬいぐるみ", 500, 0, 0.30),
            _hit("dated", "ぬいぐるみ", 900, 2000, 0.10)]
    vs = RecordingVectorStore(hits)
    chat = FakeChat([_est("もちもちぬいぐるみ")])
    est = _estimator(vs, chat, top_k=2, recency=0.9)
    est.estimate_products(["もちもちぬいぐるみ"], "u")
    prompt = chat.last_user_prompt
    assert prompt.index("dated") < prompt.index("nodate")
    assert "| ? |" in prompt


def test_context_line_format_shape():
    vs = RecordingVectorStore([_hit("itemX", "ぬいぐるみ", 500, 0, 0.1)])
    chat = FakeChat([_est("もちもちぬいぐるみ")])
    est = _estimator(vs, chat)
    est.estimate_products(["もちもちぬいぐるみ"], "u")
    assert "- itemX | ぬいぐるみ | ¥500 | ? | s" in chat.last_user_prompt


def test_merge_keeps_minimum_distance_for_same_id():
    class TwoPhaseStore:
        def __init__(self):
            self._calls = 0
            self.where_calls = []

        def query(self, embedding, n_results, where=None):
            self.where_calls.append(where)
            self._calls += 1
            dist = 0.4 if where is not None else 0.1
            return [_hit("a", "ぬいぐるみ", 500, 0, dist)]

    vs = TwoPhaseStore()
    chat = FakeChat([_est("もちもちぬいぐるみ")])
    est = _estimator(vs, chat)
    est.estimate_products(["もちもちぬいぐるみ"], "u")
    assert chat.last_user_prompt.count("- a | ") == 1


def test_surplus_estimate_dropped_with_warning(caplog):
    import logging
    vs = RecordingVectorStore([_hit("a", "ぬいぐるみ", 500, 0, 0.1)])
    chat = FakeChat([_est("totally unrelated")])
    est = _estimator(vs, chat)
    with caplog.at_level(logging.WARNING):
        batch = est.estimate_products(["line A"], "u")
    assert [e.product_name for e in batch.estimates] == ["line A"]
    assert batch.estimates[0].confidence == "low"
    assert any("dropped" in r.message for r in caplog.records)
