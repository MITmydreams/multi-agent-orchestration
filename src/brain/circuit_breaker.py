"""Circuit Breaker - System-level ban-rate monitor and automatic throttle.

Tracks bans, warnings, and IP reputation in Redis and enforces global
and per-account safeguards:

* Single-account risk > 0.65 -> hibernate 72 h
* Single-account warning   -> hibernate 1 week + rotate IP
* Single-account ban        -> mark abandoned + batch-wide 24 h hibernate
* Same IP 2 bans            -> permanently blacklist that IP
* Daily ban rate > 5 %      -> YELLOW: system-wide 50 % speed for 1 week
* Daily ban rate > 10 %     -> RED: full stop + human review
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redis key templates
# ---------------------------------------------------------------------------
_KEY_BANS_TODAY = "cb:bans:daily:{date}"          # SET of account IDs
_KEY_ACTIVE_TODAY = "cb:active:daily:{date}"      # SET of account IDs
_KEY_BANNED_IPS = "cb:banned_ips"                  # SET of IPs
_KEY_IP_BAN_COUNT = "cb:ip_bans:{ip}"              # INT
_KEY_ACCOUNT_STATE = "cb:account:{account_id}"     # HASH
_KEY_SYSTEM_STATE = "cb:system_state"              # STRING
_KEY_SYSTEM_STATE_UNTIL = "cb:system_state_until"  # STRING (ISO datetime)
_KEY_BATCH_HIBERNATE = "cb:batch_hibernate:{batch_id}"  # STRING (ISO datetime)

# Durations
_HIBERNATE_DURATION = timedelta(hours=72)
_WARNING_HIBERNATE_DURATION = timedelta(weeks=1)
_BATCH_HIBERNATE_DURATION = timedelta(hours=24)
_YELLOW_DURATION = timedelta(weeks=1)
_KEY_TTL_SECONDS = 60 * 60 * 24 * 14  # 14 days for daily keys


class SystemState(Enum):
    """Global circuit-breaker state."""

    GREEN = "green"    # Normal
    YELLOW = "yellow"  # Daily ban rate 5-10 % -> 50 % speed for 1 week
    RED = "red"        # Daily ban rate > 10 % -> full stop


class AccountState(BaseModel):
    """Per-account state tracked in Redis."""

    account_id: int
    status: str = "active"  # active | hibernating | warned | abandoned
    hibernated_until: datetime | None = None
    reason: str | None = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CircuitBreaker:
    """System-wide and per-account circuit breaker backed by Redis.

    All public methods are async because they talk to Redis.
    """

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    # ------------------------------------------------------------------
    # Ban / warning reporters
    # ------------------------------------------------------------------

    async def report_ban(
        self,
        account_id: int,
        ip: str,
        batch_id: str | None = None,
    ) -> None:
        """Record an account ban and trigger cascading safeguards."""
        today = _today_str()
        pipe = self._redis.pipeline()

        # 1. Add to daily ban set
        ban_key = _KEY_BANS_TODAY.format(date=today)
        pipe.sadd(ban_key, str(account_id))
        pipe.expire(ban_key, _KEY_TTL_SECONDS)

        # 2. Mark account as abandoned
        acct_key = _KEY_ACCOUNT_STATE.format(account_id=account_id)
        pipe.hset(acct_key, mapping={
            "status": "abandoned",
            "reason": "banned",
            "updated_at": datetime.utcnow().isoformat(),
        })

        # 3. Increment IP ban counter
        ip_key = _KEY_IP_BAN_COUNT.format(ip=ip)
        pipe.incr(ip_key)
        pipe.expire(ip_key, _KEY_TTL_SECONDS)

        await pipe.execute()

        # 4. Check if IP should be permanently blacklisted (>= 2 bans)
        ip_ban_count = int(await self._redis.get(ip_key) or 0)
        if ip_ban_count >= 2:
            await self._redis.sadd(_KEY_BANNED_IPS, ip)
            logger.warning("IP %s permanently blacklisted (%d bans)", ip, ip_ban_count)

        # 5. Batch-wide hibernate if batch_id is given
        if batch_id:
            hibernate_until = datetime.utcnow() + _BATCH_HIBERNATE_DURATION
            batch_key = _KEY_BATCH_HIBERNATE.format(batch_id=batch_id)
            await self._redis.set(
                batch_key,
                hibernate_until.isoformat(),
                ex=int(_BATCH_HIBERNATE_DURATION.total_seconds()),
            )
            logger.warning(
                "Batch %s hibernated until %s due to ban of account %d",
                batch_id, hibernate_until.isoformat(), account_id,
            )

        # 6. Re-evaluate system state
        await self._update_system_state()

        logger.warning(
            "Account %d banned. IP=%s batch=%s", account_id, ip, batch_id,
        )

    async def report_warning(self, account_id: int) -> None:
        """Record a Telegram warning on an account -> hibernate 1 week."""
        hibernate_until = datetime.utcnow() + _WARNING_HIBERNATE_DURATION
        acct_key = _KEY_ACCOUNT_STATE.format(account_id=account_id)
        await self._redis.hset(acct_key, mapping={
            "status": "warned",
            "hibernated_until": hibernate_until.isoformat(),
            "reason": "telegram_warning",
            "updated_at": datetime.utcnow().isoformat(),
        })
        logger.warning(
            "Account %d warned. Hibernating until %s",
            account_id, hibernate_until.isoformat(),
        )

    async def hibernate_account(
        self,
        account_id: int,
        duration: timedelta = _HIBERNATE_DURATION,
        reason: str = "high_risk",
    ) -> None:
        """Manually hibernate an account for a given duration."""
        hibernate_until = datetime.utcnow() + duration
        acct_key = _KEY_ACCOUNT_STATE.format(account_id=account_id)
        await self._redis.hset(acct_key, mapping={
            "status": "hibernating",
            "hibernated_until": hibernate_until.isoformat(),
            "reason": reason,
            "updated_at": datetime.utcnow().isoformat(),
        })
        logger.info(
            "Account %d hibernated until %s (reason: %s)",
            account_id, hibernate_until.isoformat(), reason,
        )

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    async def get_system_state(self) -> SystemState:
        """Return the current global system state."""
        raw = await self._redis.get(_KEY_SYSTEM_STATE)
        if raw is None:
            return SystemState.GREEN

        state_until_raw = await self._redis.get(_KEY_SYSTEM_STATE_UNTIL)
        if state_until_raw:
            state_until = datetime.fromisoformat(state_until_raw)
            if datetime.utcnow() > state_until:
                # State has expired -> reset to green
                await self._redis.delete(_KEY_SYSTEM_STATE, _KEY_SYSTEM_STATE_UNTIL)
                return SystemState.GREEN

        try:
            return SystemState(raw)
        except ValueError:
            return SystemState.GREEN

    async def get_daily_ban_rate(self) -> float:
        """Return today's ban rate as a float in [0, 1]."""
        today = _today_str()
        ban_key = _KEY_BANS_TODAY.format(date=today)
        active_key = _KEY_ACTIVE_TODAY.format(date=today)

        banned_count = await self._redis.scard(ban_key)
        active_count = await self._redis.scard(active_key)

        if active_count == 0:
            return 0.0
        return banned_count / active_count

    async def get_banned_ips(self) -> set[str]:
        """Return the full set of permanently blacklisted IPs."""
        members = await self._redis.smembers(_KEY_BANNED_IPS)
        return {m if isinstance(m, str) else m.decode() for m in members}

    async def register_active_account(self, account_id: int) -> None:
        """Mark an account as active today (for ban-rate denominator)."""
        today = _today_str()
        active_key = _KEY_ACTIVE_TODAY.format(date=today)
        pipe = self._redis.pipeline()
        pipe.sadd(active_key, str(account_id))
        pipe.expire(active_key, _KEY_TTL_SECONDS)
        await pipe.execute()

    async def should_proceed(self, account_id: int) -> bool:
        """Check whether *account_id* is allowed to take action right now.

        Returns ``False`` if any of these hold:
        1. System state is RED (full stop).
        2. Account is abandoned.
        3. Account is hibernating and the window has not elapsed.
        4. Account's batch is under hibernate.
        """
        # System-level check
        state = await self.get_system_state()
        if state == SystemState.RED:
            return False

        # Per-account check
        acct_key = _KEY_ACCOUNT_STATE.format(account_id=account_id)
        acct_data = await self._redis.hgetall(acct_key)

        if not acct_data:
            return True  # No record -> new/clean account

        # Normalise bytes keys if needed
        acct = {
            (k if isinstance(k, str) else k.decode()): (
                v if isinstance(v, str) else v.decode()
            )
            for k, v in acct_data.items()
        }

        status = acct.get("status", "active")
        if status == "abandoned":
            return False

        if status in ("hibernating", "warned"):
            until_raw = acct.get("hibernated_until")
            if until_raw:
                until = datetime.fromisoformat(until_raw)
                if datetime.utcnow() < until:
                    return False
                # Hibernation expired -> clear state
                await self._redis.hset(acct_key, mapping={
                    "status": "active",
                    "reason": "",
                    "hibernated_until": "",
                    "updated_at": datetime.utcnow().isoformat(),
                })

        return True

    async def is_open(self, agent_type: str) -> bool:
        """Return ``True`` if the circuit breaker is tripped (RED state).

        The *agent_type* parameter is accepted for future per-agent-type
        granularity but currently only checks the global system state.
        """
        state = await self.get_system_state()
        return state == SystemState.RED

    async def get_speed_multiplier(self) -> float:
        """Return the global speed multiplier.

        * GREEN  -> 1.0
        * YELLOW -> 0.5
        * RED    -> 0.0
        """
        state = await self.get_system_state()
        return {
            SystemState.GREEN: 1.0,
            SystemState.YELLOW: 0.5,
            SystemState.RED: 0.0,
        }[state]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _update_system_state(self) -> None:
        """Re-evaluate global state based on today's ban rate."""
        rate = await self.get_daily_ban_rate()

        if rate > 0.10:
            new_state = SystemState.RED
            logger.critical(
                "RED ALERT: Daily ban rate %.1f%% > 10%%. System paused. Human review required.",
                rate * 100,
            )
            # RED has no automatic expiry -- requires manual reset
            await self._redis.set(_KEY_SYSTEM_STATE, new_state.value)
            await self._redis.delete(_KEY_SYSTEM_STATE_UNTIL)

        elif rate > 0.05:
            new_state = SystemState.YELLOW
            until = datetime.utcnow() + _YELLOW_DURATION
            logger.warning(
                "YELLOW WARNING: Daily ban rate %.1f%% > 5%%. 50%% speed until %s.",
                rate * 100, until.isoformat(),
            )
            await self._redis.set(_KEY_SYSTEM_STATE, new_state.value)
            await self._redis.set(
                _KEY_SYSTEM_STATE_UNTIL,
                until.isoformat(),
                ex=int(_YELLOW_DURATION.total_seconds()),
            )

        else:
            # Only reset to green if not already in a higher state with remaining duration
            current = await self._redis.get(_KEY_SYSTEM_STATE)
            if current in (SystemState.YELLOW.value, None, SystemState.GREEN.value):
                await self._redis.set(_KEY_SYSTEM_STATE, SystemState.GREEN.value)

    async def reset_system_state(self) -> None:
        """Manual reset by operator (e.g. after human review of a RED alert)."""
        await self._redis.set(_KEY_SYSTEM_STATE, SystemState.GREEN.value)
        await self._redis.delete(_KEY_SYSTEM_STATE_UNTIL)
        logger.info("System state manually reset to GREEN.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")
