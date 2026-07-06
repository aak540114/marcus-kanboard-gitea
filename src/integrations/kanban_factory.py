"""Factory for creating kanban provider instances.

Simplifies the process of creating the right kanban provider
based on configuration.
"""

import os
from typing import Any, Dict, Optional

from src.config.marcus_config import get_config
from src.integrations.kanban_interface import KanbanInterface, KanbanProvider
from src.integrations.providers import (
    KanboardKanban,
    SQLiteKanban,
)


class KanbanFactory:
    """Factory for creating kanban provider instances."""

    @staticmethod
    def create(
        provider: str, config: Optional[Dict[str, Any]] = None
    ) -> KanbanInterface:
        """
        Create a kanban provider instance.

        Parameters
        ----------
        provider : str
            Provider name ('kanboard', 'sqlite')
        config : Optional[Dict[str, Any]]
            Optional configuration override

        Returns
        -------
        KanbanInterface
            KanbanInterface implementation

        Raises
        ------
        ValueError
            If provider is not supported
        """
        # Get centralized configuration
        marcus_config = get_config()

        provider_lower = provider.lower()

        if provider_lower == KanbanProvider.SQLITE.value:
            if not config:
                config = {
                    "db_path": (
                        marcus_config.kanban.sqlite_db_path
                        or os.getenv(
                            "SQLITE_KANBAN_DB_PATH",
                            "./data/kanban.db",
                        )
                    ),
                    "project_name": (
                        marcus_config.kanban.board_name
                        or os.getenv(
                            "MARCUS_PROJECT_NAME",
                            "Marcus Project",
                        )
                    ),
                    "attachments_dir": (
                        marcus_config.kanban.sqlite_attachments_dir
                        or os.getenv(
                            "SQLITE_KANBAN_ATTACHMENTS_DIR",
                            "./data/attachments",
                        )
                    ),
                }
            return SQLiteKanban(config)

        elif provider_lower == KanbanProvider.KANBOARD.value:
            if not config:
                config = {
                    "kanboard_url": (
                        getattr(marcus_config.kanban, "kanboard_url", None)
                        or os.getenv(
                            "KANBOARD_URL", "http://localhost:8080/jsonrpc.php"
                        )
                    ),
                    "kanboard_api_token": (
                        getattr(marcus_config.kanban, "kanboard_api_token", None)
                        or os.getenv("KANBOARD_API_TOKEN", "")
                    ),
                    "kanboard_project_id": int(
                        str(
                            getattr(marcus_config.kanban, "kanboard_project_id", None)
                            or os.getenv("KANBOARD_PROJECT_ID", "1")
                        )
                    ),
                }
            return KanboardKanban(config)

        else:
            raise ValueError(f"Unsupported kanban provider: {provider}")

    @staticmethod
    def get_default_provider() -> str:
        """Get the default provider from configuration."""
        config = get_config()
        return config.kanban.provider or os.getenv("KANBAN_PROVIDER", "sqlite")

    @staticmethod
    def create_default(config: Optional[Dict[str, Any]] = None) -> KanbanInterface:
        """Create the default kanban provider."""
        provider = KanbanFactory.get_default_provider()
        return KanbanFactory.create(provider, config)
