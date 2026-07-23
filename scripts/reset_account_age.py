"""Reset account_age_days for all virtual accounts to 0 (fresh tier).

Keeps 10000000001 = 365 (user-confirmed real veteran account).

Usage::

    # From the project root (with venv activated)
    PYTHONPATH=. python scripts/reset_account_age.py
"""

from __future__ import annotations

import asyncio
import sys

import structlog
from sqlalchemy import text

from src.config.settings import settings
from src.models.base import engine

logger = structlog.get_logger(__name__)

VETERAN_PHONE = "10000000001"


async def reset() -> None:
    """Reset all account ages to 0, keep veteran at 365."""

    logger.info("reset.start", url=settings.database_url)

    async with engine.begin() as conn:
        # 1. Zero out every non-veteran account
        result = await conn.execute(
            text(
                "UPDATE accounts SET account_age_days = 0 "
                "WHERE phone <> :veteran"
            ),
            {"veteran": VETERAN_PHONE},
        )
        logger.info("reset.zeroed", rows_updated=result.rowcount)

        # 2. Re-assert veteran = 365 (idempotent safety net)
        result = await conn.execute(
            text(
                "UPDATE accounts SET account_age_days = 365 "
                "WHERE phone = :veteran"
            ),
            {"veteran": VETERAN_PHONE},
        )
        logger.info(
            "reset.veteran_set",
            phone=VETERAN_PHONE,
            rows_updated=result.rowcount,
        )

        # 3. Print final state
        rows = await conn.execute(
            text("SELECT phone, account_age_days FROM accounts ORDER BY id")
        )
        for phone, age in rows:
            print(f"{phone}: {age} days")

    await engine.dispose()
    logger.info("reset.done")


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
        asyncio.run(reset())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.exception("reset.failed", error=str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
