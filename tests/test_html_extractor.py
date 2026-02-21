import pathlib

# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false


from estimator_king.crawler.html_extractor import (  # pyright: ignore[reportMissingImports]
    extract_detail_sections,
)


FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_html_extract_hololive_jp_sections_present():
    html = _read_fixture("product_html_hololive_basic.html")
    sections = extract_detail_sections(html)

    assert set(sections.keys()) == {"セット詳細", "グッズ詳細"}
    assert "アクリルスタンド" in sections["セット詳細"]
    assert "缶バッジ" in sections["セット詳細"]
    assert "W50mm" in sections["グッズ詳細"]
    assert "アクリル" in sections["グッズ詳細"]


def test_html_extract_vspo_en_sections_present():
    html = _read_fixture("product_html_vspo_basic.html")
    sections = extract_detail_sections(html)

    assert set(sections.keys()) == {"Set Details", "Merch details"}
    assert "Sticker" in sections["Set Details"]
    assert "Keychain" in sections["Set Details"]
    assert "Material: PVC" in sections["Merch details"]
    assert "Size: 50mm" in sections["Merch details"]


def test_html_extract_nested_headings_included_until_higher_boundary():
    html = _read_fixture("product_html_nested_headings.html")
    sections = extract_detail_sections(html)

    assert set(sections.keys()) == {"Set Details"}
    assert "Top-level set content A." in sections["Set Details"]
    assert "Nested content that should still be included." in sections["Set Details"]
    assert "More nested content." in sections["Set Details"]
    assert "Should not be included." not in sections["Set Details"]


def test_html_extract_repeated_headings_combined_and_empty_section_kept():
    html = _read_fixture("product_html_repeated_headings.html")
    sections = extract_detail_sections(html)

    assert set(sections.keys()) == {"セット詳細", "グッズ詳細"}
    assert sections["セット詳細"] == "First block.\n\nSecond block."
    assert sections["グッズ詳細"] == "Accessory A"


def test_html_extract_none_returns_empty_dict():
    html = _read_fixture("product_html_none.html")
    assert extract_detail_sections(html) == {}


def test_html_extract_whitespace_normalized_deterministically():
    html = _read_fixture("product_html_whitespace.html")
    sections = extract_detail_sections(html)

    assert set(sections.keys()) == {"Set Details"}
    assert sections["Set Details"] == "Sticker x 1 Keychain x 1"


def test_html_extract_repeated_call_same_output():
    html = _read_fixture("product_html_hololive_basic.html")
    assert extract_detail_sections(html) == extract_detail_sections(html)
