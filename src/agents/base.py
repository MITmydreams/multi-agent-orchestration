"""Base agent class – abstract foundation for all five agent layers."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

import structlog

from src.brain.circuit_breaker import CircuitBreaker
from src.brain.risk_engine import RiskEngine, RiskAssessment, RiskLevel
from src.config import settings
from src.models import get_session, AgentTask
from src.tg_clients.user_client import UserClientManager

logger = structlog.get_logger(__name__)


class BaseAgent(ABC):
    """Abstract base class for all agent types.

    Provides shared lifecycle hooks: risk checking, circuit-breaker
    integration, structured logging, and graceful shutdown.
    """

    name: str = "base"
    agent_type: str = "base"

    def __init__(
        self,
        user_client: UserClientManager,
        risk_engine: RiskEngine,
        circuit_breaker: CircuitBreaker,
    ) -> None:
        self.user_client = user_client
        self.risk_engine = risk_engine
        self.circuit_breaker = circuit_breaker
        self._running = False
        self._log = logger.bind(agent=self.name, agent_type=self.agent_type)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    async def run(self) -> None:
        """Agent main loop.  Subclasses must implement this."""
        ...

    async def start(self) -> None:
        """Start the agent (sets running flag, then delegates to ``run``)."""
        self._running = True
        self._log.info("agent.starting")
        try:
            await self.run()
        except asyncio.CancelledError:
            self._log.info("agent.cancelled")
        except Exception:
            self._log.exception("agent.fatal_error")
            raise
        finally:
            self._running = False
            self._log.info("agent.stopped")

    async def stop(self) -> None:
        """Request graceful shutdown."""
        self._log.info("agent.stop_requested")
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Risk / circuit-breaker helpers
    # ------------------------------------------------------------------

    async def should_proceed(self) -> bool:
        """Check circuit breaker — returns *True* if the agent may continue.

        When the circuit breaker is open the agent should back off.
        """
        if not self._running:
            return False

        is_open = await self.circuit_breaker.is_open(self.agent_type)
        if is_open:
            self._log.warning("agent.circuit_breaker_open", agent_type=self.agent_type)
            return False
        return True

    async def check_account_risk(self, account_data: dict) -> RiskAssessment:
        """Evaluate an account's risk and log the result."""
        assessment = await self.risk_engine.evaluate(account_data)
        self._log.info(
            "agent.risk_assessed",
            account_id=assessment.account_id,
            risk_score=assessment.risk_score,
            risk_level=assessment.risk_level.value,
        )
        return assessment

    async def is_account_safe(self, account_data: dict) -> bool:
        """Return *True* if the account's risk level allows normal operations."""
        assessment = await self.check_account_risk(account_data)
        if assessment.risk_level in (RiskLevel.HIBERNATE, RiskLevel.ABANDON):
            self._log.warning(
                "agent.account_unsafe",
                account_id=assessment.account_id,
                risk_level=assessment.risk_level.value,
                recommendations=assessment.recommendations,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Logging / telemetry
    # ------------------------------------------------------------------

    async def log_activity(self, action: str, details: dict[str, Any]) -> None:
        """Persist an activity log entry to the database and emit a structured log."""
        self._log.info("agent.activity", action=action, **details)
        try:
            async with get_session() as session:
                task = AgentTask(
                    agent_type=self.agent_type,
                    account_id=details.get("account_id"),
                    group_id=details.get("group_id"),
                    task_type=action,
                    payload=details,
                    status="completed",
                    completed_at=datetime.utcnow(),
                )
                session.add(task)
        except Exception:
            self._log.exception("agent.log_activity_failed", action=action)

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _jittered_sleep(base_seconds: float, jitter_ratio: float = 0.3) -> None:
        """Sleep for *base_seconds* +/- jitter to appear human-like."""
        import random

        jitter = base_seconds * jitter_ratio
        actual = base_seconds + random.uniform(-jitter, jitter)
        await asyncio.sleep(max(0.5, actual))

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r} running={self._running}>"
