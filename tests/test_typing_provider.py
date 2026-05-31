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
