"""Atlas - Multi-Account Messaging Orchestrator.

Application entry point.  Initialises all components, starts the
CentralBrain scheduler and OfficialBot in parallel, and waits for a
termination signal before performing a graceful shutdown.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog
from redis.asyncio import Redis
from sqlalchemy import select

from src.config import settings
from src.models.base import async_session_factory, engine
from src.models.account import Account

# Brain
from src.brain.risk_engine import RiskEngine
from src.brain.circuit_breaker import CircuitBreaker
from src.brain.analytics import Analytics
from src.brain.scheduler import CentralBrain

# Agents
from src.agents.scout.agent import ScoutAgent
from src.agents.executor.agent import ExecutorAgent
from src.agents.content.agent import ContentSeederAgent
from src.agents.events.agent import EventAgent
from src.agents.bot.official_bot import OfficialBot

# Telegram
from src.tg_clients.proxy_pool import ProxyPool
from src.tg_clients.user_client import UserClientManager

# AI
from src.ai.persona import PersonaManager
from src.ai.content_gen import ContentGenerator
from src.ai.anti_spam import AntiSpamEngine


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    """Set up structlog with human-readable or JSON output."""
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.environment == "production":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    import logging

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # Additionally, always write JSON logs to a rotating file so they can
    # be parsed by jq / log collectors regardless of environment.
    log_dir = Path("data/logs")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "ops-orchestrator.jsonl",
            maxBytes=50 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        json_formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
        file_handler.setFormatter(json_formatter)
        root.addHandler(file_handler)
    except Exception as exc:  # fail-soft: stdout logging must keep working
        print(f"WARN: failed to init file logger: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Application bootstrap
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)


async def main() -> None:
    """Application entry point."""
    _configure_logging()
    logger.info(
        "app.starting",
        environment=settings.environment,
        log_level=settings.log_level,
    )

    # -- Shutdown event ------------------------------------------------
    shutdown_event = asyncio.Event()

    def _signal_handler(sig: int, _frame: object = None) -> None:
        logger.info("app.signal_received", signal=sig)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _signal_handler)

    # -- Infrastructure ------------------------------------------------
    # 1. Redis
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.ping()
        logger.info("redis.connected", url=settings.redis_url)
    except Exception:
        logger.exception("redis.connection_failed")
        return

    # 2. Database - verify connectivity (tables created by setup_db.py)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                __import__("sqlalchemy").text("SELECT 1")
            )
        logger.info("database.connected", url=settings.database_url)
    except Exception:
        logger.exception("database.connection_failed")
        await redis.aclose()
        return

    # 3. Proxy pool
    proxy_pool = ProxyPool()
    await proxy_pool.load_proxies()

    # 4. User client manager
    user_client = UserClientManager(proxy_pool=proxy_pool)

    # 4b. Warm up one healthy user client so scout/search operations
    # have a session at the very first scheduler tick. Iterates through
    # accounts and probes each with get_entity('telegram') — a known
    # public username — to detect "frozen" marketplace accounts that
    # Telegram restricts from public lookups. Frozen accounts get
    # marked but stay connected (they can still join groups). Fail-soft:
    # never blocks startup; scout will lazy-connect on demand if needed.
    async def _warmup_user_client() -> None:
        try:
            from telethon import errors as tg_errors
        except Exception:
            tg_errors = None  # type: ignore[assignment]
        try:
            async with async_session_factory() as session:
                result = await session.execute(
                    select(Account)
                    .where(Account.status == "active")
                    .order_by(Account.id.asc())
                )
                accounts = list(result.scalars().all())
        except Exception:
            logger.warning("user_client.warmup_db_query_failed", exc_info=True)
            return

        if not accounts:
            logger.warning("user_client.warmup_skipped", reason="no_active_account")
            return

        probe = "telegram"
        connected = 0
        frozen_count = 0
        for account in accounts:
            try:
                wrapper = await user_client.create_client(
                    account_id=account.id,
                    phone=account.phone,
                )
            except Exception:
                logger.warning(
                    "user_client.warmup_connect_failed",
                    account_id=account.id,
                    exc_info=True,
                )
                continue
            try:
                await wrapper.client.get_entity(probe)
                connected += 1
                logger.info(
                    "user_client.warmed_up",
                    account_id=account.id,
                    probe=probe,
                )
            except Exception as exc:
                exc_name = type(exc).__name__
                frozen_types = ()
                if tg_errors is not None:
                    frozen_types = (
                        tg_errors.UsernameNotOccupiedError,
                        tg_errors.UsernameInvalidError,
                        tg_errors.FrozenMethodInvalidError,
                    )
                if frozen_types and isinstance(exc, frozen_types):
                    import time as _time_mod
                    wrapper._frozen = True
                    wrapper._frozen_until = _time_mod.time() + 7200  # 2h auto-recover
                    frozen_count += 1
                    logger.warning(
                        "user_client.warmup_account_frozen",
                        account_id=account.id,
                        error=exc_name,
                    )
                else:
                    connected += 1  # connected but probe failed, still usable
                    logger.warning(
                        "user_client.warmup_probe_failed",
                        account_id=account.id,
                        error=exc_name,
                    )

        logger.info(
            "user_client.warmup_complete",
            connected=connected,
            frozen=frozen_count,
            total=len(accounts),
        )

        # Pre-fetch dialogs for all connected accounts so Telethon caches
        # the entities of joined groups.  Without this, send_message fails
        # with PeerIdInvalidError for groups that were joined in a prior
        # process session.
        dialog_ok = 0
        for account in accounts:
            wrapper = user_client._clients.get(account.id)
            if wrapper is None or getattr(wrapper, "_frozen", False):
                continue
            try:
                await wrapper.client.get_dialogs(limit=500)
                dialog_ok += 1
            except Exception:
                logger.debug(
                    "user_client.warmup_dialogs_failed",
                    account_id=account.id,
                )
        logger.info(
            "user_client.warmup_dialogs_done",
            synced=dialog_ok,
            total=connected,
        )

    await _warmup_user_client()

    # 5. AI / Persona
    persona_manager = PersonaManager()
    content_gen = ContentGenerator()
    anti_spam = AntiSpamEngine()

    # -- Brain components ----------------------------------------------
    # 6. Risk engine + Circuit breaker + Analytics
    risk_engine = RiskEngine()
    circuit_breaker = CircuitBreaker(redis_client=redis)

    analytics = Analytics(session_factory=async_session_factory, redis_client=redis)

    if True:  # keep indentation compatible
        # -- Agents --------------------------------------------------------
        # 7. Five-layer agent stack
        scout = ScoutAgent(
            user_client=user_client,
            risk_engine=risk_engine,
            circuit_breaker=circuit_breaker,
        )
        executor = ExecutorAgent(
            user_client=user_client,
            risk_engine=risk_engine,
            circuit_breaker=circuit_breaker,
            persona_manager=persona_manager,
            content_gen=content_gen,
            anti_spam=anti_spam,
        )
        content_seeder = ContentSeederAgent(
            user_client=user_client,
            risk_engine=risk_engine,
            circuit_breaker=circuit_breaker,
            content_gen=content_gen,
        )
        event_agent = EventAgent(
            user_client=user_client,
            risk_engine=risk_engine,
            circuit_breaker=circuit_breaker,
            content_gen=content_gen,
        )
        official_bot = OfficialBot(token=settings.tg_bot_token)

        # -- Central Brain -------------------------------------------------
        # 8. Assemble the scheduler
        brain = CentralBrain(
            risk_engine=risk_engine,
            circuit_breaker=circuit_breaker,
            analytics=analytics,
            scout=scout,
            executor=executor,
            content_seeder=content_seeder,
            event_agent=event_agent,
            user_client=user_client,
        )

        # -- Start ---------------------------------------------------------
        logger.info("app.starting_services")

        await brain.start()

        # Official bot runs in parallel (non-blocking)
        bot_task = asyncio.create_task(official_bot.start(), name="official-bot")

        logger.info("app.ready", msg="All services started. Waiting for shutdown signal.")

        # -- Wait for shutdown ---------------------------------------------
        await shutdown_event.wait()

        # -- Graceful shutdown ---------------------------------------------
        logger.info("app.shutting_down")

        await brain.stop()
        await official_bot.stop()

        if not bot_task.done():
            bot_task.cancel()
            try:
                await bot_task
            except asyncio.CancelledError:
                pass

        await user_client.disconnect_all()

    # Close infrastructure
    await redis.aclose()
    await engine.dispose()

    logger.info("app.shutdown_complete")


if __name__ == "__main__":
    asyncio.run(main())
