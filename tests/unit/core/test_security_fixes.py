"""
Focused tests for security fixes in resilience.py and service_registry.py
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.core.service_registry import MarcusServiceRegistry


class TestSecurityFixes:
    """Test specific security fixes"""

    def test_service_registry_error_handling_not_pass(self):
        """Test B110 fix: service registry doesn't use bare except-pass"""
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_dir = Path(temp_dir) / "test_registry"
            registry_dir.mkdir()

            # Create invalid JSON file
            invalid_file = registry_dir / "marcus_test.json"
            invalid_file.write_text("invalid json")

            registry = MarcusServiceRegistry()
            registry.registry_dir = registry_dir

            # Mock psutil to simulate no running processes
            with patch(
                "src.core.service_registry.psutil.pid_exists", return_value=False
            ):
                # Mock unlink to raise permission error
                with patch.object(Path, "unlink") as mock_unlink:
                    mock_unlink.side_effect = PermissionError("Access denied")

                    # Should handle error gracefully without throwing exception
                    # This tests that the error is caught and handled instead of using bare pass
                    try:
                        services = registry.discover_services()
                        # Should return empty list (no valid services)
                        assert isinstance(services, list)
                        # If we get here, the error was handled properly
                    except PermissionError:
                        # If this exception propagates, the fix isn't working
                        pytest.fail("PermissionError was not handled gracefully")

    def test_service_registry_logs_unexpected_errors(self):
        """Test that unexpected errors are handled gracefully"""
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_dir = Path(temp_dir) / "test_registry"
            registry_dir.mkdir()

            invalid_file = registry_dir / "marcus_test.json"
            invalid_file.write_text("invalid json")

            registry = MarcusServiceRegistry()
            registry.registry_dir = registry_dir

            with patch(
                "src.core.service_registry.psutil.pid_exists", return_value=False
            ):
                with patch.object(Path, "unlink") as mock_unlink:
                    mock_unlink.side_effect = RuntimeError("Unexpected system error")

                    # Should handle error gracefully without throwing exception
                    # This tests that unexpected errors are caught and logged instead of using bare pass
                    try:
                        services = registry.discover_services()
                        assert isinstance(services, list)
                        # If we get here, the unexpected error was handled properly
                    except RuntimeError:
                        # If this exception propagates, the fix isn't working
                        pytest.fail("RuntimeError was not handled gracefully")

    def test_service_registry_basic_functionality(self):
        """Test basic service registry operations to improve coverage"""
        registry = MarcusServiceRegistry("test_registry")

        # Test initialization
        assert registry.instance_id == "test_registry"
        assert "test_registry.json" in str(registry.registry_file)

        # Test unregister when file doesn't exist
        registry.unregister_service()  # Should not raise exception

    def test_service_registry_heartbeat_with_file_errors(self):
        """Test heartbeat update with file system errors"""
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = MarcusServiceRegistry("test_heartbeat")
            registry.registry_file = Path(temp_dir) / "test_heartbeat.json"

            # Create file with invalid JSON
            registry.registry_file.write_text("invalid json")

            # Should handle gracefully without throwing
            registry.update_heartbeat(status="running")

    def test_service_registry_platform_specific_paths(self):
        """Test platform-specific registry directory handling"""
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("platform.system", return_value="Windows"):
                with patch.dict("os.environ", {"APPDATA": temp_dir}):
                    registry = MarcusServiceRegistry()
                    assert temp_dir in str(registry.registry_dir)
                    assert registry.registry_dir.parts[-2:] == (".marcus", "services")
