from estimator_king.bot.estimator import Estimator, snap_to_tax_grid, _snap_estimate, _percentile, _anchor_floor
from estimator_king.config_schema import AnchorFloorConfig, AnchorTier
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


def test_estimate_products_snaps_output_to_grid():
    vs = RecordingVectorStore([_hit("a", "ぬいぐるみ", 500, 100, 0.1)])
    chat = FakeChat([ProductEstimate(
        product_name="もちもちぬいぐるみ", suggested_price_jpy=3800,
        price_range_jpy=PriceRange(min=3000, max=5000), confidence="high",
        rationale="r", reference_products=[])])
    est = _estimator(vs, chat, typing=FakeTypingProvider("ぬいぐるみ"))
    batch = est.estimate_products(["もちもちぬいぐるみ"], "u")
    out = batch.estimates[0]
    assert out.suggested_price_jpy == 3850
    assert out.suggested_price_jpy % 110 == 0
    assert out.price_range_jpy.min % 110 == 0
    assert out.price_range_jpy.max % 110 == 0


def test_percentile_linear_interpolation():
    assert _percentile([100, 200, 300, 400], 75) == 325.0
    assert _percentile([100, 200, 300, 400], 50) == 250.0
    assert _percentile([100, 200, 300, 400], 0) == 100.0
    assert _percentile([100, 200, 300, 400], 100) == 400.0


def test_percentile_single_and_empty():
    assert _percentile([500], 70) == 500.0
    assert _percentile([], 50) is None


def _est_full(name, suggested, lo, hi, conf="medium", rationale="r"):
    return ProductEstimate(
        product_name=name, suggested_price_jpy=suggested,
        price_range_jpy=PriceRange(min=lo, max=hi), confidence=conf,
        rationale=rationale, reference_products=[])


_CFG = AnchorFloorConfig(
    general_percentile=60, min_refs=3, full_percentile_min_refs=5, max_lift_ratio=1.6,
    premium_tiers=[AnchorTier(percentile=70, keywords=["温感", "もこもこ"])],
)
_REFS5 = [2000, 2500, 3000, 3500, 4000]  # linear: p50=3000, p60=3200, p70=3400


def test_anchor_floor_no_op_when_cfg_none():
    e = _est_full("x", 1000, 800, 1300)
    assert _anchor_floor("x", e, _REFS5, None) is e


def test_anchor_floor_no_op_sentinel():
    e = _est_full("x", 0, 0, 0, conf="low")
    assert _anchor_floor("x", e, _REFS5, _CFG) is e


def test_anchor_floor_no_op_sparse():
    e = _est_full("x", 1000, 800, 1300)
    assert _anchor_floor("x", e, [3000, 4000], _CFG) is e  # 2 refs < min_refs


def test_anchor_floor_no_op_empty_refs():
    e = _est_full("x", 1000, 800, 1300)
    assert _anchor_floor("x", e, [], _CFG) is e


def test_anchor_floor_raises_to_general_percentile():
    e = _est_full("ポーチ", 2200, 1800, 2900)
    out = _anchor_floor("ポーチ", e, _REFS5, _CFG)
    assert out.suggested_price_jpy == 3200
    assert out.rationale.startswith("[anchor floor:")


def test_anchor_floor_never_lowers():
    e = _est_full("x", 5000, 4000, 6000)
    assert _anchor_floor("x", e, _REFS5, _CFG) is e  # floor 3200 < suggested


def test_anchor_floor_premium_uses_higher_tier():
    e = _est_full("温感マグカップ", 2200, 1800, 2900)
    out = _anchor_floor("温感マグカップ", e, _REFS5, _CFG)
    assert out.suggested_price_jpy == 3400  # p70


def test_anchor_floor_premium_keyed_on_query_not_product_name():
    e = _est_full("rewritten name", 2200, 1800, 2900)
    out = _anchor_floor("温感マグカップ", e, _REFS5, _CFG)
    assert out.suggested_price_jpy == 3400


def test_anchor_floor_multi_tier_takes_max():
    cfg = AnchorFloorConfig(
        general_percentile=60, min_refs=3, full_percentile_min_refs=5, max_lift_ratio=1.6,
        premium_tiers=[AnchorTier(percentile=65, keywords=["温感"]),
                       AnchorTier(percentile=70, keywords=["もこもこ"])])
    e = _est_full("温感もこもこ", 2200, 1800, 2900)
    out = _anchor_floor("温感もこもこ", e, _REFS5, cfg)
    assert out.suggested_price_jpy == 3400  # max(65,70)=70 -> p70


def test_anchor_floor_keyword_nfkc_variants():
    # NFKC unifies half-width katakana ﾓｺﾓｺ -> モコモコ (matches katakana keyword),
    # and full-width latin ＢＩＧ -> big (with casefold). It does NOT convert
    # katakana to hiragana, so the keyword list must hold the katakana spelling.
    cfg = AnchorFloorConfig(
        general_percentile=60, min_refs=3, full_percentile_min_refs=5, max_lift_ratio=1.6,
        premium_tiers=[AnchorTier(percentile=70, keywords=["モコモコ", "big"])])
    e1 = _est_full("ﾓｺﾓｺぬいぐるみ", 2200, 1800, 2900)
    assert _anchor_floor("ﾓｺﾓｺぬいぐるみ", e1, _REFS5, cfg).suggested_price_jpy == 3400
    e2 = _est_full("ＢＩＧぬいぐるみ", 2200, 1800, 2900)
    assert _anchor_floor("ＢＩＧぬいぐるみ", e2, _REFS5, cfg).suggested_price_jpy == 3400
    e3 = _est_full("ふつうのぬいぐるみ", 2200, 1800, 2900)  # no keyword -> general p60
    assert _anchor_floor("ふつうのぬいぐるみ", e3, _REFS5, cfg).suggested_price_jpy == 3200


def test_anchor_floor_small_sample_clamped_to_median():
    # n=3 in [min_refs,5): even premium 温感 clamps to p50; median([2000,3000,5000])=3000
    e = _est_full("温感マグカップ", 2200, 1800, 2900)
    out = _anchor_floor("温感マグカップ", e, [2000, 3000, 5000], _CFG)
    assert out.suggested_price_jpy == 3000


def test_anchor_floor_max_lift_ratio_no_op():
    e = _est_full("x", 2200, 1800, 2900)  # 2200*1.6=3520 < floor 9000 -> skip
    assert _anchor_floor("x", e, [8000, 8500, 9000, 9500, 10000], _CFG) is e


def test_anchor_floor_recomputes_range_with_upward_skew():
    e = _est_full("ポーチ", 2200, 1800, 2900, conf="medium")
    out = _anchor_floor("ポーチ", e, _REFS5, _CFG)  # floor 3200, medium +45%
    assert out.price_range_jpy.max >= round(3200 * 1.45)
    assert out.price_range_jpy.min <= out.suggested_price_jpy


def test_anchor_floor_does_not_mutate_original():
    e = _est_full("ポーチ", 2200, 1800, 2900)
    _anchor_floor("ポーチ", e, _REFS5, _CFG)
    assert e.suggested_price_jpy == 2200 and e.rationale == "r"


def test_anchor_floor_then_snap_invariants():
    e = _est_full("ポーチ", 2200, 1800, 2900, conf="medium")
    out = _snap_estimate(_anchor_floor("ポーチ", e, _REFS5, _CFG))
    assert out.price_range_jpy.min <= out.suggested_price_jpy <= out.price_range_jpy.max
    for v in (out.suggested_price_jpy, out.price_range_jpy.min, out.price_range_jpy.max):
        assert v % 110 == 0


def test_anchor_floor_logs_apply_and_skip(caplog):
    import logging
    with caplog.at_level(logging.INFO):
        _anchor_floor("ポーチ", _est_full("ポーチ", 2200, 1800, 2900), _REFS5, _CFG)
    assert any("anchor_floor applied" in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        _anchor_floor("x", _est_full("x", 2200, 1800, 2900),
                      [8000, 8500, 9000, 9500, 10000], _CFG)
    assert any("anchor_floor skip" in r.message for r in caplog.records)


def _estimator_af(vs, chat, anchor_floor, typing=None, item_type="ぬいぐるみ"):
    return Estimator(FakeEmbedder(), chat, vs,
                     typing_provider=(typing or FakeTypingProvider(item_type)),
                     item_types=[item_type], item_types_version=1,
                     top_k=10, recency_weight=0.05, diversity_weight=0.0, fetch_multiplier=1,
                     anchor_floor=anchor_floor)


def _nui_hits():
    return [_hit(f"r{i}", "ぬいぐるみ", p, 0, 0.1)
            for i, p in enumerate([2000, 2500, 3000, 3500, 4000])]


def test_pipeline_floor_raises_low_estimate():
    vs = RecordingVectorStore(_nui_hits())
    chat = FakeChat([_est_full("ぬいぐるみ", 2200, 1800, 2900)])
    est = _estimator_af(vs, chat, _CFG, typing=FakeTypingProvider("ぬいぐるみ"))
    out = est.estimate_products(["ぬいぐるみ"], "u").estimates[0]
    assert out.suggested_price_jpy == 3190  # 3200 floor snapped to ¥110 grid
    assert "anchor floor" in out.rationale


def test_pipeline_floor_disabled_when_no_config():
    vs = RecordingVectorStore(_nui_hits())
    chat = FakeChat([_est_full("ぬいぐるみ", 2200, 1800, 2900)])
    est = _estimator_af(vs, chat, None, typing=FakeTypingProvider("ぬいぐるみ"))
    out = est.estimate_products(["ぬいぐるみ"], "u").estimates[0]
    assert out.suggested_price_jpy == 2200  # snapped only
    assert "anchor floor" not in out.rationale


def test_pipeline_floor_noop_for_sonota_query():
    # classify_query returns [] for その他 -> empty same-type set -> no floor
    hits = [_hit(f"r{i}", "その他", p, 0, 0.2) for i, p in enumerate([2000, 2500, 3000, 3500, 4000])]
    vs = RecordingVectorStore(hits)
    chat = FakeChat([_est_full("謎の物体", 2200, 1800, 2900)])
    est = _estimator_af(vs, chat, _CFG, typing=FakeTypingProvider("その他"))
    out = est.estimate_products(["謎の物体"], "u").estimates[0]
    assert out.suggested_price_jpy == 2200
    assert "anchor floor" not in out.rationale


def test_pipeline_floor_skipped_on_length_mismatch_short(caplog):
    import logging
    vs = RecordingVectorStore(_nui_hits())
    chat = FakeChat([_est_full("ぬいぐるみ", 2200, 1800, 2900)])
    est = _estimator_af(vs, chat, _CFG, typing=FakeTypingProvider("ぬいぐるみ"))
    est._reconcile = lambda names, ests: []  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        batch = est.estimate_products(["ぬいぐるみ"], "u")
    assert any("anchor_floor skipped" in r.message for r in caplog.records)
    assert batch.estimates == []


def test_pipeline_floor_skipped_on_length_mismatch_long(caplog):
    import logging
    vs = RecordingVectorStore(_nui_hits())
    chat = FakeChat([_est_full("ぬいぐるみ", 2200, 1800, 2900)])
    est = _estimator_af(vs, chat, _CFG, typing=FakeTypingProvider("ぬいぐるみ"))
    # reconcile returns TWO rows for ONE input line -> length mismatch (too many)
    est._reconcile = lambda names, ests: [_est_full("ぬいぐるみ", 2200, 1800, 2900),
                                          _est_full("extra", 2200, 1800, 2900)]  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        batch = est.estimate_products(["ぬいぐるみ"], "u")
    assert any("anchor_floor skipped" in r.message for r in caplog.records)
    # no estimate was floored (both stay at their snapped model value)
    assert all("anchor floor" not in e.rationale for e in batch.estimates)
