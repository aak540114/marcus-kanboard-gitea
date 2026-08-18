"""
Unit tests for configuration settings module.
"""


import pytest

from src.config.settings import Settings


class TestSettings:
    """Test suite for Settings configuration."""

    @pytest.fixture
    def settings(self):
        """Create Settings instance for testing."""
        return Settings()

    def test_settings_initialization(self, settings):
        """Test settings can be initialized."""
        assert settings is not None
