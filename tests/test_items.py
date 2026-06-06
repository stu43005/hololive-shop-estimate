from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.sync.items import DecomposeResult, decompose_items

TALENTS = frozenset({"さくらみこ", "白上フブキ", "博衣こより"})


def _snap(title, variants, html_details=None, pid=1):
    return ProductSnapshot(
        product_id=pid, title=title, description="",
        variants=[ProductVariant(variant_id=i + 1, title=t, price=p)
                  for i, (t, p) in enumerate(variants)],
        html_details=html_details or {},
    )


def test_excludes_set_and_zero_price():
    snap = _snap("P", [
        ("セット / フルセット", "2000"),
        ("グッズ / 特典ステッカー", "0"),
        ("グッズ / アクリルスタンド", "500"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    assert isinstance(result, DecomposeResult)
    assert [i.item_name for i in result.items] == ["アクリルスタンド"]
    assert result.items[0].price_jpy == 500
    assert result.excluded_set == 1
    assert result.excluded_zero == 1


def test_unparseable_price_counts_as_excluded_zero():
    snap = _snap("P", [
        ("グッズ / 謎の値段", "N/A"),
        ("グッズ / アクリルスタンド", "500"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    assert [i.item_name for i in result.items] == ["アクリルスタンド"]
    assert result.excluded_set == 0
    assert result.excluded_zero == 1  # "N/A" parses to None -> counted as ¥0


def test_talent_variants_merge_named_by_common_part():
    snap = _snap("3Dアクリルスタンド Blue Journey衣装ver.", [
        ("グッズ / さくらみこ Blue Journey衣装ver.", "330"),
        ("グッズ / 白上フブキ Blue Journey衣装ver.", "330"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    items = result.items
    assert len(items) == 1
    assert items[0].item_name == "Blue Journey衣装ver."  # common part, not product title
    assert items[0].price_jpy == 330
    assert len(items[0].source_variant_ids) == 2
    assert set(items[0].talents) == {"さくらみこ", "白上フブキ"}
    assert result.excluded_set == 0
    assert result.excluded_zero == 0


def test_themed_series_not_merged_even_at_same_price():
    snap = _snap("生日記念", [
        ("グッズ / Start your Journey ポーチ", "440"),
        ("グッズ / Start your Journey プレート", "440"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    names = sorted(i.item_name for i in result.items)
    # Codepoint sort: プ (U+30D7) < ポ (U+30DD).
    assert names == ["Start your Journey プレート", "Start your Journey ポーチ"]
    assert result.excluded_set == 0
    assert result.excluded_zero == 0


def test_short_option_value_named_by_residual():
    snap = _snap("ぶいすぽっ！オリジナルTシャツ", [
        ("バリエーション / 黒　M", "5500"),
        ("バリエーション / 白　L", "5500"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    names = sorted(i.item_name for i in result.items)
    # No product-title prefix; normalize_text collapses the full-width space (U+3000).
    assert names == ["白 L", "黒 M"]


def test_detail_snippet_substring_match():
    snap = _snap("誕生日記念", [
        ("グッズ / Eternity アクリルジオラマスタンド", "995"),
        ("グッズ / イオフィカラー ショルダーバッグ", "600"),
    ], html_details={
        "グッズ詳細": (
            "◇記念グッズ ・Eternity アクリルジオラマスタンド サイズ：約H250×W150×D60mm 素材：アクリル"
            " ・イオフィカラー ショルダーバッグ サイズ：約H18.5×W13×D5cm 素材：ポリエステル"
        )
    })
    items = {i.item_name: i for i in decompose_items(snap, talents=TALENTS).items}
    assert "H250" in items["Eternity アクリルジオラマスタンド"].detail_snippet
    assert "ポリエステル" in items["イオフィカラー ショルダーバッグ"].detail_snippet


def test_voice_item_has_no_snippet():
    snap = _snap("誕生日記念", [
        ("デジタルコンテンツ / シチュエーションボイス「君となら」", "140"),
    ], html_details={"グッズ詳細": "◇記念グッズ ・アクリルスタンド サイズ：H100"})
    result = decompose_items(snap, talents=TALENTS)
    assert result.items[0].detail_snippet == ""


def test_pure_talent_enumeration_merges_to_product_title():
    # Each variant residual is a bare talent name (empty canonical key) at one price.
    snap = _snap("隣人ボイス2026", [
        ("ボイス / さくらみこ", "140"),
        ("ボイス / 白上フブキ", "140"),
        ("ボイス / 博衣こより", "140"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    assert len(result.items) == 1
    item = result.items[0]
    assert item.item_name == "隣人ボイス2026"  # named by product title (residual=None branch)
    assert item.price_jpy == 140
    assert len(item.source_variant_ids) == 3
    assert set(item.talents) == {"さくらみこ", "白上フブキ", "博衣こより"}


def test_empty_residual_without_talent_not_merged():
    # Residual strips to "" and no talent removed -> removed_any False -> must NOT merge.
    # With _is_option_value removed, the non-merged name is normalize_text("") == "".
    snap = _snap("グッズセット", [
        ("グッズ / ", "500"),
        ("グッズ / ", "500"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    assert len(result.items) == 2
    assert all(len(i.source_variant_ids) == 1 for i in result.items)
    assert all(i.item_name == "" for i in result.items)


def test_pure_talent_enumeration_coexists_with_distinct_item():
    # Pure-talent voices (¥140) merge to product title; a non-talent item (¥500,
    # non-empty key) stays separate and untouched.
    snap = _snap("誕生日記念", [
        ("ボイス / さくらみこ", "140"),
        ("ボイス / 白上フブキ", "140"),
        ("グッズ / アクリルスタンド", "500"),
    ])
    items = {i.item_name: i for i in decompose_items(snap, talents=TALENTS).items}
    assert set(items) == {"誕生日記念", "アクリルスタンド"}
    assert len(items["誕生日記念"].source_variant_ids) == 2
    assert set(items["誕生日記念"].talents) == {"さくらみこ", "白上フブキ"}
    assert items["アクリルスタンド"].price_jpy == 500
    assert len(items["アクリルスタンド"].source_variant_ids) == 1


def test_glued_language_suffix_merges_by_language():
    # Talent glued to a bracketed language tag: split on parens, remove talent, key = language.
    snap = _snap("秘密ボイス", [
        ("ボイス / さくらみこ（日本語）", "1000"),
        ("ボイス / 白上フブキ（日本語）", "1000"),
        ("ボイス / さくらみこ（英語）", "1000"),
        ("ボイス / 白上フブキ（英語）", "1000"),
    ])
    items = {i.item_name: i for i in decompose_items(snap, talents=TALENTS).items}
    assert set(items) == {"日本語", "英語"}  # two language groups, distinct names at one price
    assert len(items["日本語"].source_variant_ids) == 2
    assert len(items["英語"].source_variant_ids) == 2


def test_sp_voice_merges_by_common_part():
    # Talent + space + 'SPボイス' + glued language -> key = "SPボイス 日本語".
    snap = _snap("秘密ボイス", [
        ("ボイス / さくらみこ SPボイス（日本語）", "500"),
        ("ボイス / 白上フブキ SPボイス（日本語）", "500"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    assert len(result.items) == 1
    assert result.items[0].item_name == "SPボイス 日本語"
    assert len(result.items[0].source_variant_ids) == 2


def test_internal_space_talent_merges():
    # Talent written with an internal space (姓 名) still matches the no-space dict entry
    # via whitespace-insensitive greedy n-gram matching.
    snap = _snap("ぶいすぽっ！ジャージ", [
        ("バリエーション / さくら みこ", "7500"),
        ("バリエーション / 白上 フブキ", "7500"),
        ("バリエーション / 博衣こより", "7500"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    assert len(result.items) == 1
    item = result.items[0]
    assert item.item_name == "ぶいすぽっ！ジャージ"  # key empty -> product-title fallback
    assert len(item.source_variant_ids) == 3
    assert set(item.talents) == {"さくらみこ", "白上フブキ", "博衣こより"}  # original dict forms


BUNDLE_KW = frozenset({"グッズセット", "フルセット", "応援セット", "語セット"})
BUNDLE_KEEP = frozenset({"クリアファイルセット", "缶バッジセット", "ボイスセット"})


def test_bundle_keyword_excluded_regardless_of_price():
    # "バースデーグッズセット" matches keyword "グッズセット"; excluded even though its
    # price (1500) is below the peer median (2 peers at 3000) -> ratio 0.5 < 5.
    snap = _snap("誕生日", [
        ("グッズ / バースデーグッズセット", "1500"),
        ("グッズ / アクリルスタンド", "3000"),
        ("グッズ / タペストリー", "3000"),
    ])
    result = decompose_items(
        snap, talents=TALENTS,
        bundle_keywords=BUNDLE_KW, bundle_price_ratio=5.0, bundle_keep_keywords=BUNDLE_KEEP)
    names = [i.item_name for i in result.items]
    assert "バースデーグッズセット" not in names
    assert set(names) == {"アクリルスタンド", "タペストリー"}
    assert result.excluded_bundle == 1


def test_keep_keyword_protects_high_ratio_set():
    # "クリアファイルセット" is a single-product type: high ratio (3600 vs median 660)
    # but on the keep whitelist -> kept.
    snap = _snap("クリアファイル", [
        ("グッズ / クリアファイルセット", "3600"),
        ("グッズ / ステッカー", "660"),
        ("グッズ / ポストカード", "660"),
    ])
    result = decompose_items(
        snap, talents=TALENTS,
        bundle_keywords=BUNDLE_KW, bundle_price_ratio=5.0, bundle_keep_keywords=BUNDLE_KEEP)
    assert "クリアファイルセット" in [i.item_name for i in result.items]
    assert result.excluded_bundle == 0


def test_price_tiebreaker_excludes_non_keyword_set():
    # "stage セット" has no bundle keyword, not on keep list, ratio 30000/1000 = 30 >= 5
    # and name contains セット -> excluded by (B).
    snap = _snap("アクリルスタンド", [
        ("グッズ / hololive stageセット", "30000"),
        ("グッズ / 単品A", "1000"),
        ("グッズ / 単品B", "1000"),
    ])
    result = decompose_items(
        snap, talents=TALENTS,
        bundle_keywords=BUNDLE_KW, bundle_price_ratio=5.0, bundle_keep_keywords=BUNDLE_KEEP)
    names = [i.item_name for i in result.items]
    assert "hololive stageセット" not in names
    assert result.excluded_bundle == 1


def test_bundle_keyword_excluded_with_no_peers():
    # Single variant whose name matches a keyword: (A) fires with no peers needed.
    snap = _snap("フルセット商品", [
        ("グッズ / フルセット", "20000"),
    ])
    result = decompose_items(
        snap, talents=TALENTS,
        bundle_keywords=BUNDLE_KW, bundle_price_ratio=5.0, bundle_keep_keywords=BUNDLE_KEEP)
    assert result.items == []
    assert result.excluded_bundle == 1


def test_low_ratio_non_keyword_set_kept():
    # "缶バッジセット" non-keyword, on keep list, and ratio low -> kept; excluded_bundle 0.
    snap = _snap("缶バッジ", [
        ("グッズ / 缶バッジセット", "1000"),
        ("グッズ / タペストリー", "5000"),
        ("グッズ / アクリルスタンド", "5000"),
    ])
    result = decompose_items(
        snap, talents=TALENTS,
        bundle_keywords=BUNDLE_KW, bundle_price_ratio=5.0, bundle_keep_keywords=BUNDLE_KEEP)
    assert "缶バッジセット" in [i.item_name for i in result.items]
    assert result.excluded_bundle == 0


def test_bundle_filter_default_params_noop():
    # Default (no bundle params): no exclusion, excluded_bundle 0, field present.
    snap = _snap("P", [
        ("グッズ / アクリルスタンド", "500"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    assert result.excluded_bundle == 0
    assert [i.item_name for i in result.items] == ["アクリルスタンド"]
