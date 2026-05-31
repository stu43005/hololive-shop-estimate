import textwrap

from estimator_king.config_schema import load_config


def _write_yaml(tmp_path, body: str) -> str:
    p = tmp_path / "stores.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(p)


def test_load_config_parses_typing_and_estimator_sections(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPING_MODEL", raising=False)
    monkeypatch.delenv("TYPING_API_KEY", raising=False)
    monkeypatch.delenv("TYPING_BASE_URL", raising=False)
    monkeypatch.delenv("CHAT_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    path = _write_yaml(tmp_path, """
        stores:
          - id: s
            base_url: https://x
            sitemap_url: https://x/sitemap.xml
        item_types: [ぬいぐるみ, タオル]
        item_types_version: 2
        talents: [博衣こより, 白銀ノエル]
        estimator:
          top_k: 7
          recency_weight: 0.1
    """)
    cfg = load_config(path)
    assert cfg.item_types == ["ぬいぐるみ", "タオル"]
    assert cfg.item_types_version == 2
    assert cfg.talents == frozenset({"博衣こより", "白銀ノエル"})
    assert cfg.estimator_top_k == 7
    assert cfg.estimator_recency_weight == 0.1
    pc = cfg.build_provider_config()
    assert pc.typing_api_key == "k"
    assert pc.typing_model == "gpt-4o-mini"


def test_load_config_defaults_when_sections_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    path = _write_yaml(tmp_path, """
        stores:
          - id: s
            base_url: https://x
            sitemap_url: https://x/sitemap.xml
    """)
    cfg = load_config(path)
    assert cfg.item_types == []
    assert cfg.item_types_version == 0
    assert cfg.talents == frozenset()
    assert cfg.estimator_top_k == 10
    assert cfg.estimator_recency_weight == 0.05
