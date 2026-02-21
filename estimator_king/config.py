"""Configuration loading for Estimator King."""

import os
from typing import Optional

from .config_schema import AppConfig, load_config

__all__ = ["get_config", "load_config", "AppConfig"]


def get_config(key: str, default: Optional[str] = None) -> Optional[str]:
    """Get configuration value from environment."""
    return os.getenv(key, default)


def get_app_config(config_path: Optional[str] = None) -> AppConfig:
    """Load complete application configuration from YAML and environment.

    Args:
        config_path: Path to YAML config file. If None, uses CONFIG_PATH env var
                    or defaults to './stores_config.yaml'

    Returns:
        AppConfig: Loaded and validated configuration
    """
    return load_config(config_path)
