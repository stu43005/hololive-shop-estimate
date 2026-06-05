from scripts.update_talents_config import merge_talents, splice_talents_block


def test_merge_talents_is_add_only_and_sorted():
    existing = {"兎田ぺこら", "夜空メル"}  # 夜空メル graduated, not in mined
    mined = {"兎田ぺこら", "AZKi"}  # AZKi new, 兎田ぺこら already present
    union, added = merge_talents(existing, mined)
    assert union == ["AZKi", "兎田ぺこら", "夜空メル"]  # graduated name kept
    assert added == ["AZKi"]  # only the genuinely new name


def test_merge_talents_no_additions():
    existing = {"AZKi", "IRyS"}
    mined = {"AZKi"}  # subset of existing
    union, added = merge_talents(existing, mined)
    assert union == ["AZKi", "IRyS"]
    assert added == []


def test_splice_talents_block_preserves_surrounding_lines():
    lines = [
        "item_types_version: 3\n",
        "\n",
        "# Talent display names comment\n",
        "talents:\n",
        "  - AZKi\n",
        "  - 兎田ぺこら\n",
        "\n",
        "estimator:\n",
        "  top_k: 10\n",
    ]
    result = splice_talents_block(lines, ["AZKi", "IRyS", "兎田ぺこら"])
    assert result == [
        "item_types_version: 3\n",
        "\n",
        "# Talent display names comment\n",
        "talents:\n",
        "  - AZKi\n",
        "  - IRyS\n",
        "  - 兎田ぺこら\n",
        "\n",
        "estimator:\n",
        "  top_k: 10\n",
    ]


def test_splice_talents_block_roundtrip_is_identical():
    lines = [
        "talents:\n",
        "  - AZKi\n",
        "  - 兎田ぺこら\n",
        "estimator:\n",
    ]
    # Rewriting the same (already-sorted) entries must not change anything.
    assert splice_talents_block(lines, ["AZKi", "兎田ぺこら"]) == lines


def test_splice_talents_block_at_eof():
    # talents block is the final content (no trailing key after it)
    lines = ["x: 1\n", "talents:\n", "  - AZKi\n"]
    assert splice_talents_block(lines, ["AZKi", "IRyS"]) == [
        "x: 1\n",
        "talents:\n",
        "  - AZKi\n",
        "  - IRyS\n",
    ]
