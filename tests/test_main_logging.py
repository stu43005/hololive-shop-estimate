import argparse
import logging
from unittest.mock import patch

import pytest

import estimator_king.__main__ as cli


def test_log_format_includes_logger_name():
    assert "%(name)s" in cli._LOG_FORMAT
    assert "%(levelname)s" in cli._LOG_FORMAT
    assert "%(message)s" in cli._LOG_FORMAT


def test_run_crawl_config_failure_logs_under_module_logger(caplog):
    """錯誤須由模組 logger（estimator_king.__main__）發出，而非 root logger。"""
    args = argparse.Namespace(config="x.yaml", db=None, force_refetch=False)
    with caplog.at_level(logging.ERROR):
        with patch(
            "estimator_king.__main__.AppConfig.from_yaml",
            side_effect=RuntimeError("bad config"),
        ):
            with pytest.raises(SystemExit):
                cli.run_crawl(args)
    recs = [r for r in caplog.records if r.name == "estimator_king.__main__"]
    assert recs and recs[0].levelno == logging.ERROR
    assert "Failed to load config" in recs[0].getMessage()


def test_quiet_third_party_loggers_pins_httpx_to_warning_at_info():
    """非 DEBUG 模式下 httpx 的 per-request INFO 須被壓到 WARNING。"""
    httpx_logger = logging.getLogger("httpx")
    original = httpx_logger.level
    try:
        httpx_logger.setLevel(logging.NOTSET)
        cli._quiet_third_party_loggers(logging.INFO)
        assert httpx_logger.level == logging.WARNING
    finally:
        httpx_logger.setLevel(original)


def test_quiet_third_party_loggers_leaves_httpx_untouched_at_debug():
    """DEBUG 模式下不壓 httpx，讓其請求行與我們的 DEBUG 一起顯示。"""
    httpx_logger = logging.getLogger("httpx")
    original = httpx_logger.level
    try:
        httpx_logger.setLevel(logging.NOTSET)
        cli._quiet_third_party_loggers(logging.DEBUG)
        assert httpx_logger.level == logging.NOTSET
    finally:
        httpx_logger.setLevel(original)
