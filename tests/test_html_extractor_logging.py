import logging

from estimator_king.crawler.html_extractor import extract_detail_sections


def test_no_blocks_debug_logs_under_module_logger(caplog):
    with caplog.at_level(logging.DEBUG):
        out = extract_detail_sections("")

    assert out == {}
    recs = [
        r for r in caplog.records
        if r.name == "estimator_king.crawler.html_extractor"
    ]
    assert recs and recs[0].levelno == logging.DEBUG
    assert "No blocks found" in recs[0].getMessage()
