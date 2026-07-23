"""Migration: add account_age_days column to accounts table.

Usage::

    # From the project root (with venv activated)
    PYTHONPATH=. python scripts/migrate_age_field.py
"""

from __future__ import annotations

import asyncio
import sys

import structlog
from sqlalchemy import text

from src.config.settings import settings
from src.models.base import engine

logger = structlog.get_logger(__name__)


async def migrate() -> None:
    """Add account_age_days column and seed existing account."""

    logger.info("migrate.start", url=settings.database_url)

    async with engine.begin() as conn:
        # 1. Add column (idempotent via IF NOT EXISTS)
        await conn.execute(
            text(
                "ALTER TABLE accounts "
                "ADD COLUMN IF NOT EXISTS account_age_days INTEGER NOT NULL DEFAULT 0"
            )
        )
        logger.info("migrate.column_added", column="account_age_days")

        # 2. Seed known account as veteran (365 days)
        result = await conn.execute(
            text(
                "UPDATE accounts SET account_age_days = 365 "
                "WHERE phone = '10000000001'"
            )
        )
        logger.info(
            "migrate.seed_account",
            phone="10000000001",
            rows_updated=result.rowcount,
        )

    await engine.dispose()
    logger.info("migrate.done")


def main() -> None:
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
        asyncio.run(migrate())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.exception("migrate.failed", error=str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
