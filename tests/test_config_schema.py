import textwrap

import pytest
from estimator_king.config_schema import AnchorFloorConfig, AnchorTier, load_config


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
          diversity_weight: 0.2
          fetch_multiplier: 3
    """)
    cfg = load_config(path)
    assert cfg.item_types == ["ぬいぐるみ", "タオル"]
    assert cfg.item_types_version == 2
    assert cfg.talents == frozenset({"博衣こより", "白銀ノエル"})
    assert cfg.estimator_top_k == 7
    assert cfg.estimator_recency_weight == 0.1
    assert cfg.estimator_diversity_weight == 0.2
    assert cfg.estimator_fetch_multiplier == 3
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
    assert cfg.estimator_diversity_weight == 0.05
    assert cfg.estimator_fetch_multiplier == 2


def test_load_config_parses_bundle_set_section(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    path = _write_yaml(tmp_path, """
        stores:
          - id: s
            base_url: https://x
            sitemap_url: https://x/sitemap.xml
        bundle_set:
          keywords: [グッズセット, フルセット]
          price_ratio: 4.0
          keep_keywords: [ステッカーセット]
    """)
    cfg = load_config(path)
    assert cfg.bundle_set.keywords == frozenset({"グッズセット", "フルセット"})
    assert cfg.bundle_set.price_ratio == 4.0
    assert cfg.bundle_set.keep_keywords == frozenset({"ステッカーセット"})


def test_load_config_bundle_set_defaults_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    path = _write_yaml(tmp_path, """
        stores:
          - id: s
            base_url: https://x
            sitemap_url: https://x/sitemap.xml
    """)
    cfg = load_config(path)
    assert cfg.bundle_set.keywords == frozenset()
    assert cfg.bundle_set.keep_keywords == frozenset()
    assert cfg.bundle_set.price_ratio == 5.0


def test_bundle_set_policy_rejects_non_positive_ratio():
    from estimator_king.config_schema import BundleSetPolicy
    with pytest.raises(ValueError, match="price_ratio"):
        BundleSetPolicy(price_ratio=0.0).validate()


_STORE = """
        stores:
          - id: s
            base_url: https://x
            sitemap_url: https://x/sitemap.xml
"""


def _load(tmp_path, monkeypatch, body):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    return load_config(_write_yaml(tmp_path, _STORE + body))


def test_anchor_floor_absent_is_none(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, "")
    assert cfg.estimator_anchor_floor is None


def test_anchor_floor_parsed(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, """
        estimator:
          anchor_floor:
            general_percentile: 60
            min_refs: 3
            full_percentile_min_refs: 5
            max_lift_ratio: 1.6
            premium_tiers:
              - percentile: 70
                keywords: ["温感", "もこもこ"]
""")
    af = cfg.estimator_anchor_floor
    assert isinstance(af, AnchorFloorConfig)
    assert af.general_percentile == 60
    assert af.min_refs == 3
    assert af.full_percentile_min_refs == 5
    assert af.max_lift_ratio == 1.6
    assert isinstance(af.premium_tiers[0], AnchorTier)
    assert af.premium_tiers[0].percentile == 70
    assert af.premium_tiers[0].keywords == ["温感", "もこもこ"]


def test_anchor_floor_defaults(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, """
        estimator:
          anchor_floor:
            general_percentile: 60
""")
    af = cfg.estimator_anchor_floor
    assert af.min_refs == 3 and af.full_percentile_min_refs == 5
    assert af.max_lift_ratio == 1.6 and af.premium_tiers == []


@pytest.mark.parametrize("block", [
    "anchor_floor: {}",  # present but missing required general_percentile
    "anchor_floor: 123",  # not a mapping
    "anchor_floor:\n            general_percentile: 60\n            premium_tiers: 温感",  # not a list
    "anchor_floor:\n            general_percentile: 60\n            premium_tiers:\n              - keywords: [\"温感\"]",  # tier missing percentile
    "anchor_floor:\n            general_percentile: 120",
    "anchor_floor:\n            general_percentile: 60.5",  # non-integer rejected, not truncated
    "anchor_floor:\n            general_percentile: 60\n            min_refs: 0",
    "anchor_floor:\n            general_percentile: 60\n            full_percentile_min_refs: 2",
    "anchor_floor:\n            general_percentile: 60\n            max_lift_ratio: 0.5",
    "anchor_floor:\n            general_percentile: 60\n            premium_tiers:\n              - percentile: 70\n                keywords: []",
    "anchor_floor:\n            general_percentile: 60\n            premium_tiers:\n              - percentile: 70\n                keywords: 温感",  # scalar string, not a list
])
def test_anchor_floor_validate_rejects(tmp_path, monkeypatch, block):
    with pytest.raises(ValueError):
        _load(tmp_path, monkeypatch, f"        estimator:\n          {block}\n")
