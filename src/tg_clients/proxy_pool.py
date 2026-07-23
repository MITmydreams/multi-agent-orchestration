"""Proxy pool management for distributing Telegram accounts across proxies."""

from __future__ import annotations

import asyncio
from datetime import datetime

import httpx
import structlog
from pydantic import BaseModel

from src.config import settings

logger = structlog.get_logger(__name__)

MAX_ACCOUNTS_PER_PROXY = 3


class ProxyConfig(BaseModel):
    """Configuration for a single proxy endpoint."""

    id: int
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    protocol: str = "socks5"  # "socks5" | "http"
    country: str = "US"  # ISO country code
    assigned_accounts: list[int] = []  # account IDs, max 2-3 per proxy
    is_banned: bool = False
    last_checked: datetime | None = None
    success_rate: float = 1.0


class ProxyPool:
    """Manages a pool of proxies, assigning them to Telegram accounts.

    Each proxy is shared by at most ``MAX_ACCOUNTS_PER_PROXY`` accounts to
    reduce the fingerprinting risk of many sessions behind one IP.
    """

    def __init__(self) -> None:
        self._proxies: dict[int, ProxyConfig] = {}
        # account_id -> proxy_id fast lookup
        self._account_proxy_map: dict[int, int] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    async def load_proxies(self) -> None:
        """Load proxies from a local JSON file or provider API.

        Checks ``config/proxies.json`` first (preferred for static IPs),
        then falls back to provider API if configured.
        """
        import json
        from pathlib import Path

        # Try local config file first
        config_path = Path("config/proxies.json")
        if config_path.exists():
            try:
                data = json.loads(config_path.read_text())
                for item in data:
                    proxy = ProxyConfig(
                        id=item["id"],
                        host=item["host"],
                        port=int(item["port"]),
                        username=item.get("username"),
                        password=item.get("password"),
                        protocol=item.get("protocol", "socks5"),
                        country=item.get("country", "US"),
                    )
                    self._proxies[proxy.id] = proxy

                logger.info(
                    "proxy_pool.loaded_from_file",
                    count=len(self._proxies),
                    path=str(config_path),
                )
                return
            except Exception:
                logger.exception("proxy_pool.file_load_failed")

        # Fall back to provider API
        if not settings.proxy_api_key:
            logger.warning("proxy_pool.no_api_key", msg="No proxy API key set; pool will be empty")
            return

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"https://api.{settings.proxy_provider}.com/proxies",
                    headers={"Authorization": f"Bearer {settings.proxy_api_key}"},
                    params={"limit": settings.proxy_pool_size},
                )
                resp.raise_for_status()
                data = resp.json()

            for idx, item in enumerate(data.get("proxies", data) if isinstance(data, dict) else data):
                proxy = ProxyConfig(
                    id=idx,
                    host=item["host"],
                    port=int(item["port"]),
                    username=item.get("username"),
                    password=item.get("password"),
                    protocol=item.get("protocol", "socks5"),
                    country=item.get("country", "US"),
                )
                self._proxies[proxy.id] = proxy

            logger.info("proxy_pool.loaded_from_api", count=len(self._proxies))
        except Exception:
            logger.exception("proxy_pool.api_load_failed")

    def load_proxies_from_list(self, proxies: list[ProxyConfig]) -> None:
        """Bulk-load proxies from an in-memory list (useful for tests / seeds)."""
        for p in proxies:
            self._proxies[p.id] = p
            for aid in p.assigned_accounts:
                self._account_proxy_map[aid] = p.id
        logger.info("proxy_pool.loaded_from_list", count=len(proxies))

    # ------------------------------------------------------------------
    # Assignment
    # ------------------------------------------------------------------

    def get_proxy_for_account(self, account_id: int) -> ProxyConfig | None:
        """Return the proxy currently assigned to *account_id*, or ``None``."""
        proxy_id = self._account_proxy_map.get(account_id)
        if proxy_id is None:
            return None
        proxy = self._proxies.get(proxy_id)
        if proxy is None or proxy.is_banned:
            return None
        return proxy

    def assign_proxy(self, account_id: int) -> ProxyConfig | None:
        """Assign an available proxy to *account_id*.

        Selection prefers proxies with the fewest assigned accounts and the
        highest success rate.  Returns ``None`` when no proxy is available.
        """
        # Already assigned?
        existing = self.get_proxy_for_account(account_id)
        if existing is not None:
            return existing

        available = self.get_available_proxies()
        if not available:
            logger.warning("proxy_pool.no_available_proxy", account_id=account_id)
            return None

        # Pick the proxy with the fewest assigned accounts, then highest success_rate
        best = min(available, key=lambda p: (len(p.assigned_accounts), -p.success_rate))
        best.assigned_accounts.append(account_id)
        self._account_proxy_map[account_id] = best.id
        logger.info(
            "proxy_pool.assigned",
            account_id=account_id,
            proxy_id=best.id,
            host=best.host,
            accounts_on_proxy=len(best.assigned_accounts),
        )
        return best

    def unassign_proxy(self, account_id: int) -> None:
        """Remove the proxy assignment for *account_id*."""
        proxy_id = self._account_proxy_map.pop(account_id, None)
        if proxy_id is not None:
            proxy = self._proxies.get(proxy_id)
            if proxy and account_id in proxy.assigned_accounts:
                proxy.assigned_accounts.remove(account_id)

    # ------------------------------------------------------------------
    # Health checking
    # ------------------------------------------------------------------

    async def check_proxy(self, proxy: ProxyConfig) -> bool:
        """Verify that *proxy* can reach the internet.

        Updates ``last_checked`` and ``success_rate`` accordingly.
        """
        try:
            proxy_url = self._build_httpx_proxy_url(proxy)
            async with httpx.AsyncClient(
                proxy=proxy_url,
                timeout=10,
            ) as client:
                resp = await client.get("https://api.telegram.org")
                ok = resp.status_code < 500
        except Exception:
            ok = False

        proxy.last_checked = datetime.utcnow()
        # Exponential moving average
        alpha = 0.3
        proxy.success_rate = alpha * (1.0 if ok else 0.0) + (1 - alpha) * proxy.success_rate
        logger.debug(
            "proxy_pool.check",
            proxy_id=proxy.id,
            ok=ok,
            success_rate=round(proxy.success_rate, 3),
        )
        return ok

    async def check_all(self) -> dict[int, bool]:
        """Run health checks on every proxy concurrently."""
        results: dict[int, bool] = {}
        tasks = {pid: self.check_proxy(p) for pid, p in self._proxies.items() if not p.is_banned}
        for pid, coro in tasks.items():
            results[pid] = await coro
        return results

    # ------------------------------------------------------------------
    # Ban / availability
    # ------------------------------------------------------------------

    def ban_proxy(self, proxy_id: int) -> None:
        """Mark a proxy as banned and re-assign its accounts."""
        proxy = self._proxies.get(proxy_id)
        if proxy is None:
            return
        proxy.is_banned = True
        logger.warning("proxy_pool.banned", proxy_id=proxy_id, host=proxy.host)
        # Detach accounts so they can be re-assigned
        for aid in list(proxy.assigned_accounts):
            self.unassign_proxy(aid)

    def unban_proxy(self, proxy_id: int) -> None:
        proxy = self._proxies.get(proxy_id)
        if proxy:
            proxy.is_banned = False

    def get_available_proxies(self) -> list[ProxyConfig]:
        """Return proxies that are not banned and have capacity."""
        return [
            p
            for p in self._proxies.values()
            if not p.is_banned and len(p.assigned_accounts) < MAX_ACCOUNTS_PER_PROXY
        ]

    # ------------------------------------------------------------------
    # Format converters
    # ------------------------------------------------------------------

    def to_tdlib_proxy(self, proxy: ProxyConfig) -> dict | None:
        """Convert a ``ProxyConfig`` into the dict format expected by TDLib.

        TDLib proxy format::

            {"server": "host", "port": 1080, "type": {"@type": "proxyTypeSocks5", ...}}
        """
        proto = proxy.protocol.lower()
        if proto.startswith("socks"):
            ptype: dict[str, str] = {"@type": "proxyTypeSocks5"}
        elif proto == "http":
            ptype = {"@type": "proxyTypeHttp"}
        else:
            logger.error("proxy_pool.unknown_protocol", protocol=proxy.protocol)
            return None

        if proxy.username:
            ptype["username"] = proxy.username
        if proxy.password:
            ptype["password"] = proxy.password

        return {
            "server": proxy.host,
            "port": proxy.port,
            "type": ptype,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_httpx_proxy_url(proxy: ProxyConfig) -> str:
        scheme = "socks5" if proxy.protocol.lower().startswith("socks") else "http"
        auth = ""
        if proxy.username and proxy.password:
            auth = f"{proxy.username}:{proxy.password}@"
        return f"{scheme}://{auth}{proxy.host}:{proxy.port}"

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def size(self) -> int:
        return len(self._proxies)

    @property
    def available_count(self) -> int:
        return len(self.get_available_proxies())

    def __repr__(self) -> str:
        return f"<ProxyPool total={self.size} available={self.available_count}>"
