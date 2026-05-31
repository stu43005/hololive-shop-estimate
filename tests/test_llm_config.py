from estimator_king.llm.config import ProviderConfig


def test_typing_fields_have_defaults():
    cfg = ProviderConfig(embedding_api_key="e", chat_api_key="c")
    assert cfg.typing_model == "gpt-4o-mini"
    assert cfg.typing_base_url is None
    assert cfg.typing_api_key == ""
