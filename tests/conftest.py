"""Pytest configuration and fixtures."""

# pyright: reportUnknownMemberType=false
# pyright: reportUntypedFunctionDecorator=false

import os
import sys

import pytest  # pyright: ignore[reportMissingImports]


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.fixture
def sample_data():
    """Provide sample test data."""
    return {"test": "data"}


