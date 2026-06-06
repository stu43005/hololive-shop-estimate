from scripts.mine_item_types import (
    Candidate,
    is_noise_phrase,
    mine_candidates,
    trailing_token,
)

_NO_TALENTS: frozenset[str] = frozenset()


def test_trailing_token_returns_last_whitespace_token():
    assert trailing_token("ホロライブ0期生 ちびキャラ アクリルフィギュア") == "アクリルフィギュア"
    assert trailing_token("デスクライト") == "デスクライト"
    assert trailing_token("") == ""
    assert trailing_token("   ") == ""


def test_is_noise_phrase_drops_short_tokens():
    assert is_noise_phrase("CD", _NO_TALENTS) is False  # 2 chars survives
    assert is_noise_phrase("羽", _NO_TALENTS) is True


def test_is_noise_phrase_drops_talent_names():
    # is_noise_phrase expects an already-whitespace-stripped talent set (the
    # stripping is mine_candidates' job); see the end-to-end test below for the
    # spaced-talent-in-config case.
    assert is_noise_phrase("湊あくあ", frozenset({"湊あくあ"})) is True


def test_is_noise_phrase_drops_digits_and_version_markers():
    assert is_noise_phrase("誕生日記念2024", _NO_TALENTS) is True  # digit + 記念
    assert is_noise_phrase("vol.2", _NO_TALENTS) is True
    assert is_noise_phrase("Ver.2", _NO_TALENTS) is True
    assert is_noise_phrase("0期生", _NO_TALENTS) is True


def test_is_noise_phrase_drops_event_and_campaign_words():
    for word in ("先行通販", "オリジナル法被祭", "活動2周年", "クリスマスセット"):
        assert is_noise_phrase(word, _NO_TALENTS) is True


def test_is_noise_phrase_drops_bracketed_fragments():
    assert is_noise_phrase("「SuperNova」ボード", _NO_TALENTS) is True


def test_is_noise_phrase_keeps_genuine_type_words():
    for word in ("デスクライト", "お守り", "ゲーミングマウス", "オードトワレ"):
        assert is_noise_phrase(word, _NO_TALENTS) is False


def test_mine_candidates_aggregates_by_trailing_token_frequency():
    samples = [
        "推し デスクライト",
        "別の推し デスクライト",
        "三人目 デスクライト",
        "誰か お守り",
        "別人 お守り",
        "一回だけ ヘッドホン",  # below min_freq
    ]
    candidates = mine_candidates(samples, talents=_NO_TALENTS, min_freq=2)
    phrases = {c.phrase: c.frequency for c in candidates}
    assert phrases == {"デスクライト": 3, "お守り": 2}
    # sorted by frequency desc
    assert candidates[0].phrase == "デスクライト"


def test_mine_candidates_filters_noise_before_counting():
    samples = [
        "推しA 誕生日記念2024",
        "推しB 誕生日記念2024",
        "推しC リングライト",
        "推しD リングライト",
    ]
    candidates = mine_candidates(samples, talents=_NO_TALENTS, min_freq=2)
    phrases = {c.phrase for c in candidates}
    assert phrases == {"リングライト"}


def test_mine_candidates_caps_examples():
    samples = [f"推し{i} デスクライト" for i in range(10)]
    candidates = mine_candidates(samples, talents=_NO_TALENTS, min_freq=1, max_examples=3)
    assert len(candidates) == 1
    assert isinstance(candidates[0], Candidate)
    assert len(candidates[0].examples) == 3


def test_mine_candidates_respects_talent_filter():
    samples = ["グッズ 湊あくあ", "別グッズ 湊あくあ", "本物 タンブラー", "別物 タンブラー"]
    candidates = mine_candidates(
        samples, talents=frozenset({"湊あくあ"}), min_freq=2
    )
    assert {c.phrase for c in candidates} == {"タンブラー"}


def test_mine_candidates_talent_filter_is_whitespace_insensitive():
    # config stores a spaced talent name; the bare trailing token still matches
    samples = ["グッズ 湊あくあ", "別グッズ 湊あくあ"]
    candidates = mine_candidates(
        samples, talents=frozenset({"湊 あくあ"}), min_freq=1
    )
    assert candidates == []
