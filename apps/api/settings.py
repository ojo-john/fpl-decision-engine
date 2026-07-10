"""Thin re-export of application settings for use within the API layer."""

from packages.shared.config import get_settings

__all__ = ["get_settings"]
