"""Smoke tests for Estimator King package."""

import estimator_king
import estimator_king.crawler
import estimator_king.sync
import estimator_king.bot
import estimator_king.database


def test_package_version():
    """Test package version is defined."""
    assert hasattr(estimator_king, "__version__")
    assert estimator_king.__version__ == "0.1.0"


def test_crawler_module():
    """Test crawler module is importable."""
    assert estimator_king.crawler is not None


def test_sync_module():
    """Test sync module is importable."""
    assert estimator_king.sync is not None


def test_bot_module():
    """Test bot module is importable."""
    assert estimator_king.bot is not None


def test_database_module():
    """Test database module is importable."""
    assert estimator_king.database is not None
