from estimator_king.bot.estimator import Estimator, snap_to_tax_grid, _snap_estimate
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
        self.n_results_calls = []

    def query(self, embedding, n_results, where=None):
        self.where_calls.append(where)
        self.n_results_calls.append(n_results)
        return list(self._hits)


def _hit(id, item_type, price, pub, dist, product_title="P"):
    return QueryHit(id=id, document="", distance=dist, metadata={
        "item_name": id, "item_type": item_type, "price_jpy": price,
        "published_at": pub, "store_id": "s", "detail_snippet": "",
        "product_title": product_title})


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


def _estimator(vs, chat, typing=None, top_k=10, recency=0.05, diversity=0.0, fetch_mult=1):
    return Estimator(FakeEmbedder(), chat, vs, typing_provider=(typing or FakeTypingProvider()),
                     item_types=["ぬいぐるみ"], item_types_version=1,
                     top_k=top_k, recency_weight=recency,
                     diversity_weight=diversity, fetch_multiplier=fetch_mult)


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
    assert "- itemX | ぬいぐるみ | P | ¥500 | ? | s" in chat.last_user_prompt


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


def test_diversity_promotes_distinct_keys_into_top_k():
    # 3 筆同 (ぬいぐるみ,500)（相似度遞減）+ 1 筆不同價 + 1 筆不同類型
    hits = [_hit("dup1", "ぬいぐるみ", 500, 0, 0.10),
            _hit("dup2", "ぬいぐるみ", 500, 0, 0.11),
            _hit("dup3", "ぬいぐるみ", 500, 0, 0.12),
            _hit("diffprice", "ぬいぐるみ", 900, 0, 0.13),
            _hit("difftype", "タオル", 500, 0, 0.14)]

    base_chat = FakeChat([_est("x")])
    _estimator(RecordingVectorStore(hits), base_chat, top_k=3, diversity=0.0).estimate_products(["x"], "u")
    p0 = base_chat.last_user_prompt
    # 無多樣性：純相似度序，top3 全是同鍵 dup1/dup2/dup3
    assert "dup2" in p0 and "dup3" in p0
    assert "diffprice" not in p0 and "difftype" not in p0

    div_chat = FakeChat([_est("x")])
    _estimator(RecordingVectorStore(hits), div_chat, top_k=3, diversity=0.05).estimate_products(["x"], "u")
    p1 = div_chat.last_user_prompt
    # 有多樣性：同鍵第 2/3 筆被推後，diffprice/difftype 進入 top3
    assert "diffprice" in p1 and "difftype" in p1
    assert "dup2" not in p1 and "dup3" not in p1


def test_same_type_different_price_not_penalized():
    # 同 item_type、價格各異 → 鍵不同 → 不互相懲罰，順序純由 base（相似度）決定
    hits = [_hit("hi", "ぬいぐるみ", 500, 0, 0.10),
            _hit("lo", "ぬいぐるみ", 900, 0, 0.20)]
    chat = FakeChat([_est("x")])
    _estimator(RecordingVectorStore(hits), chat, top_k=2, diversity=0.5).estimate_products(["x"], "u")
    p = chat.last_user_prompt
    assert p.index("hi") < p.index("lo")


def test_diversity_zero_degenerates_to_base_sort():
    # diversity=0 → 等同現有純 base 降冪排序（相似度序）
    # 用多字元 id（"a"/"b"/"c" 會與 prompt header 子字串碰撞，str.index 會誤抓 header）
    hits = [_hit("aaa", "ぬいぐるみ", 500, 0, 0.30),
            _hit("bbb", "ぬいぐるみ", 500, 0, 0.10),
            _hit("ccc", "ぬいぐるみ", 500, 0, 0.20)]
    chat = FakeChat([_est("x")])
    _estimator(RecordingVectorStore(hits), chat, top_k=3, diversity=0.0).estimate_products(["x"], "u")
    p = chat.last_user_prompt
    assert p.index("bbb") < p.index("ccc") < p.index("aaa")


def test_diversity_tie_breaks_by_pool_order():
    # base 相同、鍵不同 → 取候選池順序在前者（決定性）
    hits = [_hit("first", "ぬいぐるみ", 500, 0, 0.10),
            _hit("second", "タオル", 500, 0, 0.10)]
    chat = FakeChat([_est("x")])
    _estimator(RecordingVectorStore(hits), chat, top_k=2, diversity=0.5).estimate_products(["x"], "u")
    p = chat.last_user_prompt
    assert p.index("first") < p.index("second")


def test_fetch_multiplier_deepens_query_size():
    vs = RecordingVectorStore([_hit("a", "ぬいぐるみ", 500, 0, 0.1)])
    chat = FakeChat([_est("x")])
    _estimator(vs, chat, top_k=10, fetch_mult=2).estimate_products(["x"], "u")
    assert vs.n_results_calls and all(n == 20 for n in vs.n_results_calls)


def test_fetch_multiplier_one_matches_top_k():
    vs = RecordingVectorStore([_hit("a", "ぬいぐるみ", 500, 0, 0.1)])
    chat = FakeChat([_est("x")])
    _estimator(vs, chat, top_k=10, fetch_mult=1).estimate_products(["x"], "u")
    assert vs.n_results_calls and all(n == 10 for n in vs.n_results_calls)


def test_fetch_multiplier_still_sends_only_top_k_to_chat():
    # 20 筆相異價格（鍵全相異 → 多樣性不收斂），加深後送進 chat 仍限 top_k 筆
    hits = [_hit(f"h{i}", "ぬいぐるみ", 100 + i, 0, 0.10 + i * 0.01) for i in range(20)]
    vs = RecordingVectorStore(hits)
    chat = FakeChat([_est("x")])
    _estimator(vs, chat, top_k=5, fetch_mult=2).estimate_products(["x"], "u")
    ref_lines = [ln for ln in chat.last_user_prompt.splitlines() if ln.startswith("- h")]
    assert len(ref_lines) == 5


def test_reference_line_omits_product_when_equal_to_item_name():
    vs = RecordingVectorStore([_hit("P", "ぬいぐるみ", 500, 0, 0.1, product_title="P")])
    chat = FakeChat([_est("もちもちぬいぐるみ")])
    est = _estimator(vs, chat)
    est.estimate_products(["もちもちぬいぐるみ"], "u")
    prompt = chat.last_user_prompt
    assert "- P | ぬいぐるみ | ¥500 | ? | s" in prompt
    assert prompt.count("| P |") == 0  # product not repeated as its own column


def test_snap_to_tax_grid_on_grid_unchanged():
    assert snap_to_tax_grid(6600) == 6600
    assert snap_to_tax_grid(1100) == 1100
    assert snap_to_tax_grid(3850) == 3850


def test_snap_to_tax_grid_rounds_up_when_remainder_at_least_55():
    assert snap_to_tax_grid(3800) == 3850  # remainder 60


def test_snap_to_tax_grid_rounds_down_when_remainder_below_55():
    assert snap_to_tax_grid(3000) == 2970  # remainder 30


def test_snap_to_tax_grid_tie_rounds_up():
    assert snap_to_tax_grid(55) == 110  # remainder 55


def test_snap_to_tax_grid_non_positive_returns_zero():
    assert snap_to_tax_grid(0) == 0
    assert snap_to_tax_grid(-50) == 0


def test_snap_estimate_snaps_all_three_values():
    est = ProductEstimate(
        product_name="x", suggested_price_jpy=3800,
        price_range_jpy=PriceRange(min=3000, max=5000), confidence="high",
        rationale="r", reference_products=[])
    out = _snap_estimate(est)
    assert out.suggested_price_jpy == 3850
    assert out.price_range_jpy.min == 2970
    assert out.price_range_jpy.max == 4950


def test_snap_estimate_clamps_when_snapped_bounds_cross_suggested():
    # suggested 3800->3850; min 3960->3960 (> suggested); max 3700->3740 (< suggested)
    est = ProductEstimate(
        product_name="x", suggested_price_jpy=3800,
        price_range_jpy=PriceRange(min=3960, max=3700), confidence="medium",
        rationale="r", reference_products=[])
    out = _snap_estimate(est)
    assert out.price_range_jpy.min <= out.suggested_price_jpy <= out.price_range_jpy.max
    assert out.suggested_price_jpy == 3850
    assert out.price_range_jpy.min == 3850
    assert out.price_range_jpy.max == 3850


def test_snap_estimate_sentinel_stays_zero():
    est = ProductEstimate(
        product_name="x", suggested_price_jpy=0,
        price_range_jpy=PriceRange(min=0, max=0), confidence="low",
        rationale="r", reference_products=[])
    out = _snap_estimate(est)
    assert out.suggested_price_jpy == 0
    assert out.price_range_jpy.min == 0
    assert out.price_range_jpy.max == 0


def test_snap_estimate_does_not_mutate_input():
    est = ProductEstimate(
        product_name="x", suggested_price_jpy=3800,
        price_range_jpy=PriceRange(min=3000, max=5000), confidence="high",
        rationale="r", reference_products=[])
    _snap_estimate(est)
    assert est.suggested_price_jpy == 3800
    assert est.price_range_jpy.min == 3000
    assert est.price_range_jpy.max == 5000
