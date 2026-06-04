from scripts.mine_talents import (
    extract_collection_handles,
    filter_handles,
    mine_talents,
)


def test_mine_talents_returns_high_frequency_single_diff_tokens():
    docs = [
        [("グッズ / さくらみこ 衣装ver.", 330.0), ("グッズ / 白上フブキ 衣装ver.", 330.0)],
        [("グッズ / さくらみこ 記念", 500.0), ("グッズ / 白上フブキ 記念", 500.0)],
    ]
    talents = mine_talents(docs, min_freq=2)
    assert "さくらみこ" in talents and "白上フブキ" in talents


def test_mine_talents_filters_version_noise():
    docs = [[("グッズ / A 数量限定ver.", 330.0), ("グッズ / B 数量限定ver.", 330.0)]]
    talents = mine_talents(docs, min_freq=1)
    assert "数量限定ver." not in talents


def test_extract_collection_handles_picks_anchors_and_skips_images():
    html = (
        '<a href="/collections/azki">AZKi</a>'
        '<a href="/collections/gawrgura">Gawr Gura</a>'
        '<img src="/collections/azki_thumb_abc.png">'
        '<a href="/collections/foo.jpg">x</a>'
    )
    handles = extract_collection_handles(html)
    assert handles == {"azki", "gawrgura"}


def test_filter_handles_drops_exact_and_prefix_matches():
    handles = {"azki", "hololive_gen0", "holostarsen", "all", "uproar"}
    kept = filter_handles(
        handles,
        frozenset({"all", "uproar"}),
        ("hololive", "holostars"),
    )
    assert kept == {"azki"}
