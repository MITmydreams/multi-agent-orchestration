"""Database initialisation script.

Creates all tables defined by SQLAlchemy ORM models.  Safe to re-run --
``create_all`` is a no-op for tables that already exist.

Usage::

    # From the project root
    python -m scripts.setup_db

    # Or via docker compose
    docker compose run --rm db-init
"""

from __future__ import annotations

import asyncio
import sys

import structlog
from sqlalchemy import text

from src.config import settings
from src.models.base import Base, engine

# Import all models so they register with ``Base.metadata``
from src.models import (  # noqa: F401
    Account,
    AgentTask,
    ContentPiece,
    DailyMetrics,
    Group,
    GroupAccount,
    MessageLog,
)

logger = structlog.get_logger(__name__)


async def create_tables() -> None:
    """Create all tables in the database."""
    logger.info("setup_db.connecting", url=settings.database_url)

    async with engine.begin() as conn:
        # Verify connectivity
        await conn.execute(text("SELECT 1"))
        logger.info("setup_db.connected")

        # Create all tables
        await conn.run_sync(Base.metadata.create_all)
        logger.info("setup_db.tables_created")

    # List created tables for confirmation
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT tablename FROM pg_catalog.pg_tables "
                "WHERE schemaname = 'public' ORDER BY tablename"
            )
        )
        tables = [row[0] for row in result]
        logger.info("setup_db.existing_tables", tables=tables)

    await engine.dispose()
    logger.info("setup_db.done")


def main() -> None:
    """Entry point for ``python -m scripts.setup_db``."""
    # Minimal structlog setup for the script
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.PrintLoggerFactory(),
    )

    try:
        asyncio.run(create_tables())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.exception("setup_db.failed", error=str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
