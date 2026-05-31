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
    assert tp.calls == 1  # zero-hit path must reach the LLM


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
