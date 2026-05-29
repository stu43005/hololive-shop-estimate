from estimator_king.llm.config import ProviderConfig


def test_defaults_match_spec():
    cfg = ProviderConfig(embedding_api_key="k", chat_api_key="k")
    assert cfg.embedding_model == "text-embedding-3-large"
    assert cfg.embedding_dimensions == 1024
    assert cfg.chat_model == "gpt-4o"
    assert cfg.chat_structured_output is True
    assert cfg.embedding_base_url is None
    assert cfg.embedding_query_prefix == ""
    assert cfg.embedding_doc_prefix == ""


def test_overrides_apply():
    cfg = ProviderConfig(
        embedding_api_key="e",
        chat_api_key="c",
        embedding_base_url="http://ollama:11434/v1",
        embedding_model="bge-m3",
        embedding_dimensions=None,
        chat_model="qwen2",
        chat_structured_output=False,
    )
    assert cfg.embedding_base_url == "http://ollama:11434/v1"
    assert cfg.embedding_dimensions is None
    assert cfg.chat_structured_output is False
