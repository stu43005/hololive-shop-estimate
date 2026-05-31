from scripts.mine_talents import mine_talents


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
