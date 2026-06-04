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


def test_talent_variants_merge_to_product_title():
    snap = _snap("3Dアクリルスタンド Blue Journey衣装ver.", [
        ("グッズ / さくらみこ Blue Journey衣装ver.", "330"),
        ("グッズ / 白上フブキ Blue Journey衣装ver.", "330"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    items = result.items
    assert len(items) == 1
    assert items[0].item_name == "3Dアクリルスタンド Blue Journey衣装ver."
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


def test_short_option_value_prepends_product_title():
    snap = _snap("ぶいすぽっ！オリジナルTシャツ", [
        ("バリエーション / 黒　M", "5500"),
        ("バリエーション / 白　L", "5500"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    names = sorted(i.item_name for i in result.items)
    assert names == ["ぶいすぽっ！オリジナルTシャツ 白 L", "ぶいすぽっ！オリジナルTシャツ 黒 M"]


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
    # Residual strips to "" but no talent removed -> removed_any False -> must NOT merge.
    # Assert on item COUNT, not item_name: both empty-residual items get name == product
    # title via _is_option_value("") (len("") < 4) -> f"{title} ".strip().
    snap = _snap("グッズセット", [
        ("グッズ / ", "500"),
        ("グッズ / ", "500"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    assert len(result.items) == 2
    assert all(len(i.source_variant_ids) == 1 for i in result.items)
    assert all(i.item_name == "グッズセット" for i in result.items)


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
