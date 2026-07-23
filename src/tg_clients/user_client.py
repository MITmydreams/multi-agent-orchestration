"""User account client manager — Telethon-based.

Replaces the previous TDLib implementation. Reuses the existing Telethon
``.session`` files produced by ``scripts/smart_onboard.py`` so that no
re-authentication is required.

Public interface (signatures + return-dict shapes) is preserved verbatim
to keep scout / executor / scheduler / dashboard call sites unchanged.
Where the old TDLib path returned bare keys like ``sender_id``, the new
returns now also include the field names scout was already reading
(``from_id``, ``from_username``, ``from_name``, ``has_link``, ...) so
both contract worlds are satisfied.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from pathlib import Path
from typing import Any

import time as _time_mod

import httpx
import structlog
from sqlalchemy import select
from telethon import TelegramClient, errors, functions, types
from telethon.tl.types import PeerChannel
from telethon.errors import (
    AuthKeyUnregisteredError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)

from src.config import settings
from src.models.account import Account
from src.models.base import async_session_factory
from src.tg_clients.proxy_pool import ProxyConfig, ProxyPool

logger = structlog.get_logger(__name__)

# Permanent ban errors — account is irrecoverably dead on Telegram's side.
PERMANENT_BAN_ERRORS = (
    UserDeactivatedError,
    UserDeactivatedBanError,
    AuthKeyUnregisteredError,
    SessionRevokedError,
)

# Default frozen duration: 2 hours (seconds)
_FROZEN_DURATION = 7200

# Typing simulation parameters
_TYPING_CHAR_DELAY = 0.045
_TYPING_MIN = 1.0
_TYPING_MAX = 6.0

# Where Telethon .session files live (matches scripts/smart_onboard.py)
_SESSIONS_DIR = Path("tdlib_sessions")

# Realistic device models for rotation
_DEVICE_MODELS = [
    {"model": "Samsung Galaxy S24 Ultra", "system": "Android 14", "app": "11.7.2"},
    {"model": "Samsung Galaxy S23", "system": "Android 14", "app": "11.6.5"},
    {"model": "Google Pixel 8 Pro", "system": "Android 14", "app": "11.7.2"},
    {"model": "Google Pixel 7", "system": "Android 13", "app": "11.5.4"},
    {"model": "iPhone 15 Pro Max", "system": "iOS 17.4", "app": "11.7.0"},
    {"model": "iPhone 15", "system": "iOS 17.3", "app": "11.6.3"},
    {"model": "iPhone 14 Pro", "system": "iOS 17.2", "app": "11.5.1"},
    {"model": "Xiaomi 14 Pro", "system": "Android 14", "app": "11.7.2"},
    {"model": "OnePlus 12", "system": "Android 14", "app": "11.6.5"},
    {"model": "Huawei P60 Pro", "system": "HarmonyOS 4.0", "app": "11.4.2"},
]


class TDLibClient:
    """Wrapper around a single Telethon TelegramClient.

    The class name is kept as ``TDLibClient`` so that callers (and
    isinstance checks scattered around the codebase) continue to compile.
    Internally it now wraps a Telethon client.

    The ``_frozen`` flag is set when the account is restricted by
    Telegram from doing read/discovery operations (FrozenMethodInvalidError
    on contacts.SearchRequest, UsernameNotOccupiedError on get_entity for
    a public username, etc). Once flagged, ``_pick_any_account_id`` and
    ``_pick_any_wrapper`` skip the wrapper for non-pinned operations so
    we automatically fall over to a healthy account.
    """

    def __init__(
        self,
        client: TelegramClient,
        account_id: int,
        device: dict[str, str],
        proxy: ProxyConfig | None,
        phone: str,
    ) -> None:
        self.client = client
        self.account_id = account_id
        self.device = device
        self.proxy = proxy
        self.phone = phone
        self._authorized = False
        self._frozen = False
        self._frozen_until: float = 0  # frozen expiry timestamp (0 = no expiry / permanent)
        self._flood_until: float = 0  # timestamp until which this account is flood-waited
        # Search-specific frozen: SearchRequest blocked but other ops still work
        self._search_frozen = False
        self._search_frozen_until: float = 0

    @property
    def is_authorized(self) -> bool:
        return self._authorized and self.client.is_connected()

    @property
    def is_available(self) -> bool:
        """True if authorized, not frozen, and not flood-waited."""
        now = _time_mod.time()
        # Auto-recover: if frozen period expired, clear the flag
        if self._frozen and self._frozen_until > 0 and now > self._frozen_until:
            logger.info(
                "user_client.frozen_auto_recovered",
                account_id=self.account_id,
            )
            self._frozen = False
            self._frozen_until = 0
        if self._flood_until > 0 and now > self._flood_until:
            self._flood_until = 0
        if not self.is_authorized:
            return False
        if self._frozen:
            return False
        if self._flood_until > now:
            return False
        return True

    @property
    def session(self) -> TelegramClient:
        """Backward-compat alias — some old code reads ``wrapper.session``."""
        return self.client


class UserClientManager:
    """Manages multiple Telethon user clients.

    Each managed account uses its own ``.session`` file under
    ``tdlib_sessions/account_<proxy_id>/<phone>.session``, with a
    dedicated socks5 proxy and a stable device fingerprint.
    """

    MAX_DIRECT_CONNECTS: int = 0  # disabled: never expose real IP

    def __init__(self, proxy_pool: ProxyPool | None = None) -> None:
        self._proxy_pool = proxy_pool or ProxyPool()
        self._api_id = settings.tg_api_id
        self._api_hash = settings.tg_api_hash
        self._clients: dict[int, TDLibClient] = {}
        self._lock = asyncio.Lock()
        self._entity_cache: dict[str, Any] = {}  # "account_id:group_id" → resolved entity
        self._connect_failed: set[int] = set()  # account_ids that failed to connect recently
        self._direct_connect_count: int = 0  # how many accounts are using direct (no proxy)
        # Per-account locks: prevents concurrent Telethon operations on the
        # same .session SQLite file, which causes "database is locked" errors
        # and corrupts the entity cache.
        self._account_locks: dict[int, asyncio.Lock] = {}

        _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    def _get_account_lock(self, account_id: int) -> asyncio.Lock:
        """Get or create a per-account lock to serialize Telethon operations."""
        if account_id not in self._account_locks:
            self._account_locks[account_id] = asyncio.Lock()
        return self._account_locks[account_id]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def create_client(
        self,
        account_id: int,
        phone: str,
        proxy: ProxyConfig | None = None,
    ) -> TDLibClient:
        """Connect a Telethon client for *account_id* using its existing
        .session file. If the session is missing or unauthorized, raises.
        """
        async with self._lock:
            if account_id in self._clients and self._clients[account_id].is_authorized:
                return self._clients[account_id]

            # Resolve proxy: if DB says proxy_id=NULL, use local VPN
            if proxy is None:
                proxy = self._proxy_pool.get_proxy_for_account(account_id)
                if proxy is None:
                    proxy = self._proxy_pool.assign_proxy(account_id)

            # Resolve session path: prefer the path stored in the DB, fall
            # back to a deterministic guess.
            session_path = await self._resolve_session_path(account_id, phone, proxy)
            if session_path is None:
                raise RuntimeError(
                    f"No .session file for account_id={account_id} (phone={phone})"
                )

            device = _DEVICE_MODELS[account_id % len(_DEVICE_MODELS)]
            proxy_tuple = self._make_proxy_tuple(proxy)

            client = TelegramClient(
                str(session_path),
                self._api_id,
                self._api_hash,
                device_model=device["model"],
                system_version=device["system"],
                app_version=device["app"],
                lang_code="en",
                system_lang_code="en-US",
                proxy=proxy_tuple,
            )

            try:
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    raise RuntimeError(
                        f"Account {account_id} session is not authorized "
                        f"(re-run smart_onboard.py for {phone})"
                    )
            except PERMANENT_BAN_ERRORS as exc:
                logger.critical(
                    "user_client.account_banned",
                    account_id=account_id,
                    phone=phone[-4:] if len(phone) >= 4 else phone,
                    error=type(exc).__name__,
                )
                await self._mark_account_banned(account_id)
                try:
                    await client.disconnect()
                except Exception:
                    pass
                raise
            except Exception as proxy_exc:
                # Proxy failed — try direct connection (no proxy) if under limit
                if proxy_tuple is not None and self._direct_connect_count < self.MAX_DIRECT_CONNECTS:
                    logger.warning(
                        "user_client.proxy_failed_trying_direct",
                        account_id=account_id,
                        proxy_error=type(proxy_exc).__name__,
                    )
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    client = TelegramClient(
                        str(session_path),
                        self._api_id,
                        self._api_hash,
                        device_model=device["model"],
                        system_version=device["system"],
                        app_version=device["app"],
                        lang_code="en",
                        system_lang_code="en-US",
                        proxy=None,  # direct connection
                    )
                    try:
                        await client.connect()
                        if not await client.is_user_authorized():
                            await client.disconnect()
                            raise RuntimeError(f"Account {account_id} not authorized (direct)")
                        self._direct_connect_count += 1
                        proxy = None  # mark as direct
                        logger.info(
                            "user_client.direct_connect_ok",
                            account_id=account_id,
                            direct_count=self._direct_connect_count,
                            max_direct=self.MAX_DIRECT_CONNECTS,
                        )
                    except Exception:
                        logger.exception("user_client.direct_connect_failed", account_id=account_id)
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
                        raise
                else:
                    logger.exception("user_client.create_failed", account_id=account_id)
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    raise

            wrapper = TDLibClient(client, account_id, device, proxy, phone)
            wrapper._authorized = True
            self._clients[account_id] = wrapper

            logger.info(
                "user_client.connected",
                account_id=account_id,
                phone=phone[-4:] if len(phone) >= 4 else phone,
                device=device["model"],
                proxy_id=proxy.id if proxy else None,
            )
            return wrapper

    async def get_client(self, account_id: int) -> TDLibClient | None:
        wrapper = self._clients.get(account_id)
        if wrapper is not None and wrapper.is_authorized:
            return wrapper
        return None

    async def disconnect_client(self, account_id: int) -> None:
        wrapper = self._clients.pop(account_id, None)
        if wrapper is not None:
            await self._safe_stop(wrapper)
            logger.info("user_client.disconnected", account_id=account_id)

    async def disconnect_all(self) -> None:
        ids = list(self._clients.keys())
        for aid in ids:
            await self.disconnect_client(aid)
        logger.info("user_client.all_disconnected", count=len(ids))

    # ------------------------------------------------------------------
    # Group operations
    # ------------------------------------------------------------------

    async def join_group(self, account_id: int, group_link: str) -> bool:
        """Join a group by public username, invite link, or numeric chat id.

        Handles FloodWait with one retry, classifies errors via Telethon's
        exception hierarchy, and is a fail-soft no-op when the link cannot
        be parsed. Signature preserved for backward compatibility.
        """
        wrapper = await self._require_client(account_id)
        client = wrapper.client

        await asyncio.sleep(random.uniform(2, 5))

        link_raw = (group_link or "").strip()
        if not link_raw:
            logger.warning("user_client.join_empty_link", account_id=account_id)
            return False

        invite_hash, username, direct_chat_id = self._parse_group_link(link_raw)

        async def _do_join() -> None:
            if invite_hash is not None:
                await client(
                    functions.messages.ImportChatInviteRequest(hash=invite_hash)
                )
                return
            if direct_chat_id is not None:
                entity = await client.get_entity(direct_chat_id)
                await client(functions.channels.JoinChannelRequest(channel=entity))
                return
            if username is not None:
                entity = await client.get_entity(username)
                await client(functions.channels.JoinChannelRequest(channel=entity))
                return
            raise ValueError("could not parse link")

        attempts = 0
        while attempts < 2:
            attempts += 1
            try:
                await _do_join()
                logger.info(
                    "user_client.joined_group",
                    account_id=account_id,
                    group=group_link,
                )
                return True
            except errors.UserAlreadyParticipantError:
                logger.info(
                    "user_client.join_already_member",
                    account_id=account_id,
                    group=group_link,
                )
                return True
            except errors.ChannelsTooMuchError:
                logger.warning(
                    "user_client.groups_full",
                    account_id=account_id,
                    group=group_link,
                )
                return False
            except errors.FloodWaitError as exc:
                wait_sec = int(getattr(exc, "seconds", 60))
                self._mark_flood_wait(wrapper, wait_sec)
                logger.warning(
                    "user_client.join_flood_wait",
                    account_id=account_id,
                    group=group_link,
                    wait=wait_sec,
                    attempt=attempts,
                )
                if attempts >= 2:
                    return False
                await asyncio.sleep(min(wait_sec, 300))
                continue
            except (
                errors.InviteHashExpiredError,
                errors.InviteHashInvalidError,
                errors.ChannelPrivateError,
                errors.InviteRequestSentError,
                errors.UserPrivacyRestrictedError,
                errors.UsernameNotOccupiedError,
                errors.UsernameInvalidError,
            ) as exc:
                logger.warning(
                    "user_client.join_permanent_fail",
                    account_id=account_id,
                    group=group_link,
                    error=type(exc).__name__,
                )
                # Mark group readonly — these errors are permanent, retrying is wasteful
                await self._mark_group_readonly(group_link)
                return False
            except ValueError as exc:
                err_str = str(exc)
                logger.warning(
                    "user_client.join_bad_link",
                    account_id=account_id,
                    group=group_link,
                    error=err_str,
                )
                # "No user has X as username" — entity not found, mark readonly
                if "no user has" in err_str.lower() or "cannot find" in err_str.lower():
                    await self._mark_group_readonly(group_link)
                return False
            except Exception as exc:
                if isinstance(exc, PERMANENT_BAN_ERRORS):
                    await self._mark_account_banned(account_id)
                err_str = str(exc)
                # Catch entity-not-found errors that come as generic exceptions
                if "no user has" in err_str.lower() or "cannot find any entity" in err_str.lower():
                    logger.warning(
                        "user_client.join_entity_not_found",
                        account_id=account_id,
                        group=group_link,
                    )
                    await self._mark_group_readonly(group_link)
                    return False
                logger.exception(
                    "user_client.join_group_error",
                    account_id=account_id,
                    group=group_link,
                )
                return False

        return False

    async def leave_group(self, account_id: int, group_id: str) -> bool:
        wrapper = await self._require_client(account_id)
        try:
            entity = await self._resolve_entity(wrapper, group_id)
            await wrapper.client(functions.channels.LeaveChannelRequest(channel=entity))
            logger.info("user_client.left_group", account_id=account_id, group_id=group_id)
            return True
        except Exception as exc:
            if isinstance(exc, PERMANENT_BAN_ERRORS):
                await self._mark_account_banned(account_id)
            logger.exception(
                "user_client.leave_group_error",
                account_id=account_id,
                group_id=group_id,
            )
            return False

    async def get_group_info(
        self, account_id: int, group_id: str
    ) -> dict[str, Any] | None:
        wrapper = await self._require_client(account_id)
        try:
            return await self._get_group_info_one(wrapper, group_id)
        except errors.FrozenMethodInvalidError:
            wrapper._frozen = True
            wrapper._frozen_until = _time_mod.time() + _FROZEN_DURATION
            logger.warning(
                "user_client.info_frozen",
                account_id=account_id,
                group_id=group_id,
            )
            return None
        except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
            # The username genuinely doesn't exist — not an account problem.
            logger.info(
                "user_client.info_username_not_found",
                account_id=account_id,
                group_id=group_id,
            )
            return None
        except Exception:
            logger.exception(
                "user_client.get_group_info_error",
                account_id=account_id,
                group_id=group_id,
            )
            return None

    async def _get_group_info_one(
        self, wrapper: TDLibClient, group_id: str
    ) -> dict[str, Any] | None:
        """Internal: fetch group info using a specific wrapper. Lets
        UsernameNotOccupiedError / FrozenMethodInvalidError propagate so
        the caller can mark the wrapper frozen and retry on another."""
        entity = await self._resolve_entity(wrapper, group_id)
        title = getattr(entity, "title", None) or ""
        username = getattr(entity, "username", None)
        member_count = 0
        description = ""
        is_megagroup = bool(getattr(entity, "megagroup", False))

        if isinstance(entity, types.Channel):
            try:
                full = await wrapper.client(
                    functions.channels.GetFullChannelRequest(channel=entity)
                )
                full_chat = getattr(full, "full_chat", None)
                if full_chat is not None:
                    member_count = (
                        getattr(full_chat, "participants_count", 0) or 0
                    )
                    description = getattr(full_chat, "about", "") or ""
            except Exception:
                pass

        return {
            "id": str(getattr(entity, "id", "")),
            "title": title,
            "username": username,
            "member_count": member_count,
            "about": description,
            "description": description,  # scout reads this name
            "language": "en",  # Telegram doesn't expose this; default
            "is_megagroup": is_megagroup,
        }

    async def get_group_messages(
        self,
        account_id: int,
        group_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        wrapper = await self._require_client(account_id)
        async with self._get_account_lock(account_id):
            return await self._get_group_messages_impl(wrapper, account_id, group_id, limit)

    async def _get_group_messages_impl(
        self, wrapper: TDLibClient, account_id: int, group_id: str, limit: int,
    ) -> list[dict[str, Any]]:
        try:
            return await self._get_messages_one(wrapper, group_id, limit)
        except errors.FrozenMethodInvalidError:
            wrapper._frozen = True
            wrapper._frozen_until = _time_mod.time() + _FROZEN_DURATION
            logger.warning(
                "user_client.read_frozen",
                account_id=account_id,
                group_id=group_id,
            )
            return []
        except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
            logger.info(
                "user_client.read_username_not_found",
                account_id=account_id,
                group_id=group_id,
            )
            return []
        except errors.FloodWaitError as exc:
            wait_sec = int(getattr(exc, "seconds", 60))
            self._mark_flood_wait(wrapper, wait_sec)
            return []
        except Exception:
            logger.exception(
                "user_client.get_messages_error",
                account_id=account_id,
                group_id=group_id,
            )
            return []

    async def _get_messages_one(
        self, wrapper: TDLibClient, group_id: str, limit: int
    ) -> list[dict[str, Any]]:
        """Internal: fetch recent messages using a specific wrapper. Lets
        UsernameNotOccupiedError / FrozenMethodInvalidError / FloodWait
        propagate so the caller can decide whether to mark frozen, retry,
        or surrender."""
        entity = await self._resolve_entity(wrapper, group_id)
        messages: list[dict[str, Any]] = []
        link_re = re.compile(r"https?://|t\.me/", re.IGNORECASE)

        async for msg in wrapper.client.iter_messages(entity, limit=min(limit, 200)):
            text = msg.message or ""
            sender_id = msg.sender_id
            from_username = ""
            from_name = ""
            is_admin = False

            sender = msg.sender
            if sender is not None:
                from_username = getattr(sender, "username", "") or ""
                first = getattr(sender, "first_name", "") or ""
                last = getattr(sender, "last_name", "") or ""
                from_name = f"{first} {last}".strip() or from_username

            reply_to_id = None
            reply_to_user = None
            if msg.reply_to is not None:
                reply_to_id = getattr(msg.reply_to, "reply_to_msg_id", None)

            messages.append({
                "id": msg.id,
                "sender_id": sender_id,
                "text": text,
                "date": int(msg.date.timestamp()) if msg.date else 0,
                "reply_to_msg_id": reply_to_id,
                "from_id": sender_id,
                "from_username": from_username,
                "from_name": from_name,
                "is_admin": is_admin,
                "reply_to_user_id": reply_to_user,
                "is_deleted": False,
                "is_admin_action": False,
                "has_link": bool(link_re.search(text)),
            })

        return messages

    # ------------------------------------------------------------------
    # Message operations
    # ------------------------------------------------------------------

    async def send_message(
        self,
        account_id: int,
        group_id: str,
        text: str,
        delay: float = 0,
    ) -> bool:
        wrapper = await self._require_client(account_id)
        if delay > 0:
            await asyncio.sleep(delay)

        # Serialize all Telethon operations for this account to prevent
        # SQLite "database is locked" errors on the .session file.
        return await self._send_message_locked(wrapper, account_id, group_id, text)

    async def _send_message_locked(
        self, wrapper: TDLibClient, account_id: int, group_id: str, text: str,
    ) -> bool:
        async with self._get_account_lock(account_id):
            return await self._send_message_impl(wrapper, account_id, group_id, text)

    async def _send_message_impl(
        self, wrapper: TDLibClient, account_id: int, group_id: str, text: str,
    ) -> bool:
        # --- Phase 1: resolve entity (separate error handling) ---
        entity = None
        try:
            entity = await self._resolve_entity(wrapper, group_id)
        except errors.FloodWaitError as exc:
            # Temporary — don't mark readonly
            wait_sec = int(getattr(exc, "seconds", 60))
            self._mark_flood_wait(wrapper, wait_sec)
            return False
        except Exception:
            # Primary resolution failed — try DB fallback
            try:
                entity = await self._resolve_entity_via_db(wrapper, group_id)
            except errors.FloodWaitError as exc:
                wait_sec = int(getattr(exc, "seconds", 60))
                self._mark_flood_wait(wrapper, wait_sec)
                return False
            except Exception:
                # Both failed — this group is unreachable, mark readonly
                logger.warning(
                    "user_client.send_entity_resolve_failed",
                    account_id=account_id,
                    group_id=group_id,
                )
                await self._mark_group_readonly(group_id)
                return False

        # --- Phase 2: send the message ---
        try:
            await self._simulate_typing(wrapper, entity, text)
            await wrapper.client.send_message(entity, text)
            logger.info(
                "user_client.message_sent",
                account_id=account_id,
                group_id=group_id,
                length=len(text),
            )
            return True
        except errors.PeerIdInvalidError:
            # Entity cache stale — refresh dialogs and retry once
            logger.info(
                "user_client.peer_invalid_retry",
                account_id=account_id,
                group_id=group_id,
            )
            try:
                # get_dialogs() forces Telethon to sync all joined groups
                await wrapper.client.get_dialogs(limit=500)
                # Clear cached entity and re-resolve
                cache_key = f"{wrapper.account_id}:{group_id}"
                self._entity_cache.pop(cache_key, None)
                entity = await self._resolve_entity(wrapper, group_id)
                await wrapper.client.send_message(entity, text)
                logger.info(
                    "user_client.message_sent_after_retry",
                    account_id=account_id,
                    group_id=group_id,
                    length=len(text),
                )
                return True
            except Exception as retry_exc:
                logger.warning(
                    "user_client.peer_invalid_retry_failed",
                    account_id=account_id,
                    group_id=group_id,
                    error=type(retry_exc).__name__,
                )
                return False
        except (errors.ChatAdminRequiredError, errors.ChatWriteForbiddenError):
            logger.warning(
                "user_client.send_chat_readonly",
                account_id=account_id,
                group_id=group_id,
            )
            await self._mark_group_readonly(group_id)
            return False
        except errors.FloodWaitError as exc:
            wait_sec = int(getattr(exc, "seconds", 60))
            self._mark_flood_wait(wrapper, wait_sec)
            return False
        except Exception as exc:
            if isinstance(exc, PERMANENT_BAN_ERRORS):
                await self._mark_account_banned(account_id)
            logger.exception(
                "user_client.send_message_error",
                account_id=account_id,
                group_id=group_id,
            )
            return False

    async def send_reply(
        self,
        account_id: int,
        group_id: str,
        reply_to_id: int,
        text: str,
    ) -> bool:
        wrapper = await self._require_client(account_id)
        try:
            entity = await self._resolve_entity(wrapper, group_id)
            await self._simulate_typing(wrapper, entity, text)
            await wrapper.client.send_message(entity, text, reply_to=reply_to_id)
            logger.info(
                "user_client.reply_sent",
                account_id=account_id,
                group_id=group_id,
                reply_to=reply_to_id,
            )
            return True
        except (errors.ChatAdminRequiredError, errors.ChatWriteForbiddenError):
            logger.warning(
                "user_client.send_chat_readonly",
                account_id=account_id,
                group_id=group_id,
            )
            await self._mark_group_readonly(group_id)
            return False
        except errors.FloodWaitError as exc:
            wait_sec = int(getattr(exc, "seconds", 60))
            self._mark_flood_wait(wrapper, wait_sec)
            return False
        except Exception as exc:
            if isinstance(exc, PERMANENT_BAN_ERRORS):
                await self._mark_account_banned(account_id)
            logger.exception(
                "user_client.send_reply_error",
                account_id=account_id,
                group_id=group_id,
            )
            return False

    async def send_dm(
        self,
        account_id: int,
        user_id: str,
        text: str,
    ) -> bool:
        wrapper = await self._require_client(account_id)
        try:
            uid_int = int(user_id) if user_id.lstrip("-").isdigit() else None
            target: Any = uid_int if uid_int is not None else user_id
            entity = await wrapper.client.get_entity(target)
            await self._simulate_typing(wrapper, entity, text)
            await wrapper.client.send_message(entity, text)
            logger.info("user_client.dm_sent", account_id=account_id, user_id=user_id)
            return True
        except Exception as exc:
            if isinstance(exc, PERMANENT_BAN_ERRORS):
                await self._mark_account_banned(account_id)
            logger.exception(
                "user_client.send_dm_error", account_id=account_id, user_id=user_id
            )
            return False

    # ------------------------------------------------------------------
    # Search / discovery operations
    # ------------------------------------------------------------------

    async def search_groups(
        self, keyword: str, account_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Search Telegram for public groups/channels matching *keyword*.

        Some accounts (especially marketplace virtual numbers) are flagged
        as "frozen" by Telegram and the contacts.SearchRequest API will
        raise FrozenMethodInvalidError. We transparently retry on the next
        non-frozen account, caching the frozen flag on the wrapper so we
        don't keep poking the same dead account.

        Returns a list of dicts with at least ``id``, ``title``,
        ``username``, ``member_count``. Empty list on any failure.
        """
        if account_id is not None:
            w = await self.get_client(account_id)
            candidates = [w] if w is not None else []
        else:
            candidates = await self._ensure_capable_wrappers()
        if not candidates:
            return []

        for wrapper in candidates:
            # Skip accounts with search-specific frozen (don't block other ops)
            if wrapper._search_frozen:
                now = _time_mod.time()
                if wrapper._search_frozen_until > 0 and now > wrapper._search_frozen_until:
                    wrapper._search_frozen = False
                    wrapper._search_frozen_until = 0
                else:
                    logger.debug(
                        "user_client.search_still_frozen",
                        account_id=wrapper.account_id,
                    )
                    continue

            try:
                result = await wrapper.client(
                    functions.contacts.SearchRequest(q=keyword, limit=50)
                )
            except errors.FrozenMethodInvalidError:
                # Only mark search as frozen — account can still send/read/join
                wrapper._search_frozen = True
                wrapper._search_frozen_until = _time_mod.time() + _FROZEN_DURATION
                logger.warning(
                    "user_client.search_frozen",
                    account_id=wrapper.account_id,
                    keyword=keyword,
                )
                continue
            except errors.FloodWaitError as exc:
                wait_sec = int(getattr(exc, "seconds", 60))
                self._mark_flood_wait(wrapper, wait_sec)
                logger.warning(
                    "user_client.search_flood_wait",
                    account_id=wrapper.account_id,
                    keyword=keyword,
                    wait=wait_sec,
                )
                continue
            except Exception:
                logger.exception(
                    "user_client.search_groups_error",
                    account_id=wrapper.account_id,
                    keyword=keyword,
                )
                continue

            groups: list[dict[str, Any]] = []
            for chat in getattr(result, "chats", []) or []:
                if not isinstance(chat, types.Channel):
                    continue
                if not getattr(chat, "megagroup", False):
                    continue  # Skip broadcast-only channels (can't post)
                groups.append({
                    "id": str(chat.id),
                    "title": getattr(chat, "title", "") or "",
                    "username": getattr(chat, "username", None),
                    "member_count": getattr(chat, "participants_count", 0) or 0,
                    "is_megagroup": True,
                })
            return groups

        # All candidates were frozen
        logger.warning(
            "user_client.search_no_capable_account",
            keyword=keyword,
            tried=[w.account_id for w in candidates],
        )
        return []

    async def get_recent_messages(
        self,
        group_id: str,
        limit: int = 100,
        account_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Read-any version of get_group_messages with frozen-account
        fallback. If a wrapper raises UsernameNotOccupiedError or
        FrozenMethodInvalidError trying to resolve the group, it is
        marked _frozen=True and we transparently retry on the next
        non-frozen wrapper from the pool.
        """
        if account_id is not None:
            return await self.get_group_messages(account_id, group_id, limit)

        candidates = await self._ensure_capable_wrappers()
        if not candidates:
            return []

        for wrapper in candidates:
            try:
                return await self._get_messages_one(
                    wrapper, group_id, limit
                )
            except errors.FrozenMethodInvalidError:
                wrapper._frozen = True
                wrapper._frozen_until = _time_mod.time() + _FROZEN_DURATION
                logger.warning(
                    "user_client.read_frozen",
                    account_id=wrapper.account_id,
                    group_id=group_id,
                )
                continue
            except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
                logger.info(
                    "user_client.read_username_not_found",
                    account_id=wrapper.account_id,
                    group_id=group_id,
                )
                return []
            except errors.FloodWaitError as exc:
                wait_sec = int(getattr(exc, "seconds", 60))
                self._mark_flood_wait(wrapper, wait_sec)
                logger.warning(
                    "user_client.get_messages_flood",
                    account_id=wrapper.account_id,
                    group_id=group_id,
                    wait=wait_sec,
                )
                continue
            except Exception:
                logger.exception(
                    "user_client.get_messages_error",
                    account_id=wrapper.account_id,
                    group_id=group_id,
                )
                return []
        return []

    async def get_group_info_any(self, group_id: str) -> dict[str, Any] | None:
        candidates = await self._ensure_capable_wrappers()
        if not candidates:
            return None
        for wrapper in candidates:
            try:
                return await self._get_group_info_one(wrapper, group_id)
            except errors.FrozenMethodInvalidError:
                wrapper._frozen = True
                wrapper._frozen_until = _time_mod.time() + _FROZEN_DURATION
                logger.warning(
                    "user_client.info_frozen",
                    account_id=wrapper.account_id,
                    group_id=group_id,
                )
                continue
            except errors.FloodWaitError as exc:
                wait_sec = int(getattr(exc, "seconds", 60))
                self._mark_flood_wait(wrapper, wait_sec)
                logger.warning(
                    "user_client.info_flood_wait",
                    account_id=wrapper.account_id,
                    group_id=group_id,
                    wait=wait_sec,
                )
                continue
            except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
                logger.info(
                    "user_client.info_username_not_found",
                    account_id=wrapper.account_id,
                    group_id=group_id,
                )
                return None
            except Exception:
                logger.exception(
                    "user_client.get_group_info_error",
                    account_id=wrapper.account_id,
                    group_id=group_id,
                )
                return None
        return None

    async def get_linked_discussion_group(
        self, channel_id: str, account_id: int | None = None
    ) -> dict[str, Any] | None:
        """For a broadcast channel, return its linked discussion group (megagroup) if any.

        Many tech channels have a linked discussion group where members can chat.
        Returns dict with id, title, username, member_count, is_megagroup or None.
        """
        candidates = await self._ensure_capable_wrappers()
        if not candidates:
            return None

        for wrapper in candidates:
            try:
                entity = await self._resolve_entity(wrapper, channel_id)
                if not isinstance(entity, types.Channel):
                    return None

                full = await wrapper.client(functions.channels.GetFullChannelRequest(channel=entity))
                linked_id = getattr(full.full_chat, 'linked_chat_id', None)
                if not linked_id:
                    return None

                # Resolve the linked chat
                linked_entity = await wrapper.client.get_entity(linked_id)
                return {
                    "id": str(linked_entity.id),
                    "title": getattr(linked_entity, "title", "") or "",
                    "username": getattr(linked_entity, "username", None),
                    "member_count": getattr(linked_entity, "participants_count", 0) or 0,
                    "is_megagroup": bool(getattr(linked_entity, "megagroup", False)),
                }
            except errors.FrozenMethodInvalidError:
                wrapper._frozen = True
                wrapper._frozen_until = _time_mod.time() + _FROZEN_DURATION
                continue
            except Exception:
                logger.debug("user_client.get_linked_discussion.error", channel_id=channel_id)
                return None
        return None

    async def react_to_message(
        self,
        account_id: int,
        group_id: str,
        message_id: int,
        emoji: str,
    ) -> bool:
        wrapper = await self._require_client(account_id)
        try:
            entity = await self._resolve_entity(wrapper, group_id)
            await wrapper.client(
                functions.messages.SendReactionRequest(
                    peer=entity,
                    msg_id=message_id,
                    reaction=[types.ReactionEmoji(emoticon=emoji)],
                )
            )
            logger.info(
                "user_client.reaction_sent",
                account_id=account_id,
                group_id=group_id,
                emoji=emoji,
            )
            return True
        except Exception:
            logger.exception("user_client.react_error", account_id=account_id)
            return False

    async def get_game_events(self) -> list[dict[str, Any]]:
        try:
            async with httpx.AsyncClient(
                base_url=settings.product_api_url,
                headers={"Authorization": f"Bearer {settings.product_api_key}"},
                timeout=10,
            ) as http:
                resp = await http.get("/events/recent")
                resp.raise_for_status()
                data = resp.json()
                return data if isinstance(data, list) else data.get("events", [])
        except Exception:
            logger.debug("user_client.get_game_events.no_data")
            return []

    async def get_referral_chain(self, user_id: str) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(
                base_url=settings.product_api_url,
                headers={"Authorization": f"Bearer {settings.product_api_key}"},
                timeout=10,
            ) as http:
                resp = await http.get(f"/referrals/{user_id}/chain")
                resp.raise_for_status()
                return resp.json()
        except Exception:
            logger.debug("user_client.get_referral_chain.error", user_id=user_id)
            return None

    # ------------------------------------------------------------------
    # Status queries
    # ------------------------------------------------------------------

    async def get_me(self, account_id: int) -> dict[str, Any] | None:
        wrapper = await self._require_client(account_id)
        try:
            me = await wrapper.client.get_me()
            return {
                "id": me.id,
                "first_name": getattr(me, "first_name", "") or "",
                "last_name": getattr(me, "last_name", "") or "",
                "username": getattr(me, "username", None),
                "phone": getattr(me, "phone", None),
            }
        except Exception:
            logger.exception("user_client.get_me_error", account_id=account_id)
            return None

    async def is_connected(self, account_id: int) -> bool:
        wrapper = self._clients.get(account_id)
        return wrapper is not None and wrapper.is_authorized

    @property
    def connected_count(self) -> int:
        return sum(1 for w in self._clients.values() if w.is_authorized)

    # ------------------------------------------------------------------
    # Group status helpers
    # ------------------------------------------------------------------

    async def _mark_account_banned(self, account_id: int) -> None:
        """Persist account status='abandoned' after a permanent Telegram ban."""
        try:
            async with async_session_factory() as session:
                acct = await session.get(Account, account_id)
                if acct and acct.status != "abandoned":
                    acct.status = "abandoned"
                    await session.commit()
            logger.critical("user_client.account_status_abandoned", account_id=account_id)
        except Exception:
            logger.exception("user_client.mark_banned_failed", account_id=account_id)

    async def _mark_group_readonly(self, group_id: str) -> None:
        """Mark a group as readonly (can't post) by setting status='readonly'."""
        try:
            from src.models.group import Group

            async with async_session_factory() as session:
                bare = group_id.lstrip("@")
                stmt = select(Group).where(
                    (Group.tg_group_id == group_id)
                    | (Group.tg_group_id == f"@{bare}")
                    | (Group.username == bare)
                    | (Group.username == group_id)
                )
                result = await session.execute(stmt)
                group = result.scalar_one_or_none()
                if group and group.status != "readonly":
                    group.status = "readonly"
                    group.notes = (group.notes or "") + " [auto-readonly: ChatAdminRequired]"
                    await session.commit()
                    logger.info(
                        "user_client.group_marked_readonly",
                        group_id=group_id,
                        hint="scheduler Phase 0 will auto-leave accounts from this group",
                    )
        except Exception:
            pass  # best-effort

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _pick_any_account_id(self, include_frozen: bool = False) -> int | None:
        for aid, w in self._clients.items():
            if not w.is_authorized:
                continue
            if not include_frozen and not w.is_available:
                continue
            return aid
        return None

    async def _pick_any_wrapper(
        self, account_id: int | None = None, include_frozen: bool = False
    ) -> TDLibClient | None:
        if account_id is not None:
            return await self.get_client(account_id)
        aid = self._pick_any_account_id(include_frozen=include_frozen)
        if aid is None:
            return None
        return self._clients.get(aid)

    async def _ensure_capable_wrappers(self, max_extra: int = 3) -> list[TDLibClient]:
        """Return a list of connected, non-frozen wrappers, lazy-connecting
        up to ``max_extra`` additional accounts from the DB if the current
        pool is empty or fully frozen.
        """
        live = [
            w for w in self._clients.values()
            if w.is_available
        ]
        if live:
            return live

        connected_ids = set(self._clients.keys())
        try:
            async with async_session_factory() as session:
                result = await session.execute(
                    select(Account.id, Account.phone)
                    .where(Account.status == "active")
                    .order_by(Account.id.asc())
                )
                rows = list(result.all())
        except Exception:
            rows = []

        extras: list[TDLibClient] = []
        for row in rows:
            if row.id in connected_ids:
                continue
            if row.id in self._connect_failed:
                continue
            try:
                w = await self.create_client(account_id=row.id, phone=row.phone)
                if not w._frozen:
                    extras.append(w)
                if len(extras) >= max_extra:
                    break
            except Exception:
                self._connect_failed.add(row.id)
                continue
        return extras

    async def _require_client(self, account_id: int) -> TDLibClient:
        wrapper = await self.get_client(account_id)
        if wrapper is not None:
            return wrapper
        # Skip accounts that already failed to connect
        if account_id in self._connect_failed:
            raise RuntimeError(
                f"No connected client for account_id={account_id} "
                f"(previously failed to connect, skipping)"
            )
        # Lazy connect: look up the account in the DB and connect on demand.
        # This lets the scheduler hand work to any of the 15 accounts without
        # us having to pre-warm all of them at startup.
        try:
            async with async_session_factory() as session:
                result = await session.execute(
                    select(Account.phone).where(Account.id == account_id)
                )
                phone = result.scalar_one_or_none()
        except Exception:
            phone = None
        if not phone:
            raise RuntimeError(
                f"No connected client for account_id={account_id} "
                f"(and account not found in DB)"
            )
        try:
            return await self.create_client(account_id=account_id, phone=phone)
        except Exception as exc:
            self._connect_failed.add(account_id)
            raise RuntimeError(
                f"No connected client for account_id={account_id} "
                f"(lazy connect failed: {exc})"
            ) from exc

    async def _resolve_session_path(
        self, account_id: int, phone: str, proxy: ProxyConfig | None
    ) -> Path | None:
        """Find the .session file for this account.

        Looks up ``accounts.session_string`` first (where smart_onboard.py
        wrote the path), then falls back to a deterministic guess based on
        proxy id.
        """
        # 1. Check DB for the session_string field
        try:
            async with async_session_factory() as session:
                result = await session.execute(
                    select(Account.session_string).where(Account.id == account_id)
                )
                stored = result.scalar_one_or_none()
        except Exception:
            stored = None

        candidates: list[Path] = []
        if stored:
            p = Path(stored)
            if not p.is_absolute():
                p = Path.cwd() / p
            candidates.append(p)

        # 2. Deterministic guess: tdlib_sessions/account_<proxy_id>/<phone>.session
        if proxy is not None:
            phone_clean = phone.lstrip("+")
            candidates.append(
                _SESSIONS_DIR / f"account_{proxy.id}" / f"{phone_clean}.session"
            )

        # 3. Last-resort glob across all proxy buckets
        phone_clean = phone.lstrip("+")
        for d in sorted(_SESSIONS_DIR.glob("account_*")):
            candidates.append(d / f"{phone_clean}.session")

        for c in candidates:
            if c.exists():
                # Telethon wants the path WITHOUT the .session extension
                return c.with_suffix("")
            # Also accept the path without extension if Telethon already
            # canonicalised it
            if c.with_suffix("").with_suffix(".session").exists():
                return c.with_suffix("")

        logger.warning(
            "user_client.session_not_found",
            account_id=account_id,
            phone=phone,
            tried=[str(c) for c in candidates],
        )
        return None

    @staticmethod
    def _make_proxy_tuple(proxy: ProxyConfig | None) -> tuple | None:
        """Build a Telethon proxy tuple from ``ProxyConfig``.

        Telethon accepts ``(socks.SOCKS5, host, port, True, username, password)``
        but also a plain dict / tuple with the proxy type as a string. We use
        the python_socks-compatible tuple form.
        """
        if proxy is None:
            # No proxy configured — use local VPN (FlClash)
            from src.config import settings
            return (
                settings.local_vpn_protocol,
                settings.local_vpn_host,
                settings.local_vpn_port,
                True, "", "",
            )
        protocol = (proxy.protocol or "socks5").lower()
        if protocol.startswith("socks"):
            scheme = "socks5"
        elif protocol == "http":
            scheme = "http"
        else:
            scheme = "socks5"
        return (scheme, proxy.host, proxy.port, True, proxy.username, proxy.password)

    @staticmethod
    def _parse_group_link(link_raw: str) -> tuple[str | None, str | None, int | None]:
        """Return ``(invite_hash, username, direct_chat_id)``.

        Exactly one of the three is non-None on success; all three may be
        None if the link is unparseable.
        """
        s = link_raw.strip()
        lowered = s.lower()

        # Numeric chat id (with or without leading minus)
        stripped = s.lstrip("-")
        if stripped.isdigit():
            try:
                return (None, None, int(s))
            except ValueError:
                pass

        # Invite link forms
        invite_marker = None
        if "joinchat/" in lowered:
            invite_marker = lowered.split("joinchat/", 1)[1]
        elif "/+" in s:
            invite_marker = s.split("/+", 1)[1]
        elif s.startswith("+") and not stripped.isdigit():
            invite_marker = s[1:]
        if invite_marker is not None:
            invite_marker = invite_marker.split("?", 1)[0].rstrip("/")
            if invite_marker:
                return (invite_marker, None, None)

        # Public username forms: @xxx, t.me/xxx, https://t.me/xxx, plain xxx
        tail = s.rstrip("/").split("/")[-1].lstrip("@")
        if tail:
            return (None, tail, None)
        return (None, None, None)

    async def _resolve_entity(self, wrapper: TDLibClient, group_id: str) -> Any:
        """Resolve a group identifier to a Telethon entity, with caching.

        Avoids repeated ``get_entity`` calls for the same group from the same
        account, which is the root cause of PeerIdInvalid when using numeric
        group ids on accounts that haven't interacted with the group yet.
        Once resolved, the entity is cached per (account_id, group_id) pair.
        """
        cache_key = f"{wrapper.account_id}:{group_id}"
        if cache_key in self._entity_cache:
            return self._entity_cache[cache_key]

        entity = await wrapper.client.get_entity(self._coerce_entity_key(group_id))
        self._entity_cache[cache_key] = entity

        # Cap cache at 500 entries to bound memory
        if len(self._entity_cache) > 500:
            keys = list(self._entity_cache.keys())
            for k in keys[:250]:
                del self._entity_cache[k]

        return entity

    async def _resolve_entity_via_db(self, wrapper: TDLibClient, group_id: str) -> Any:
        """Fallback entity resolution: look up the group's username in the DB
        and resolve via ``@username``. Raises if no username found or resolution
        still fails.
        """
        from src.models.group import Group

        username: str | None = None
        try:
            async with async_session_factory() as session:
                gid_str = str(group_id).strip()
                clause = Group.tg_group_id == gid_str
                if gid_str.lstrip("-").isdigit():
                    clause = clause | (Group.tg_group_id == gid_str.lstrip("-"))
                row = await session.execute(
                    select(Group.username).where(clause)
                )
                username = row.scalar_one_or_none()
        except Exception:
            pass

        if not username:
            raise ValueError(f"No DB username for group_id={group_id}")

        entity = await self._resolve_entity(wrapper, f"@{username}")
        logger.info(
            "user_client.entity_resolved_via_db",
            account_id=wrapper.account_id,
            group_id=group_id,
            username=username,
        )
        return entity

    def _mark_flood_wait(self, wrapper: TDLibClient, seconds: int) -> None:
        """Mark a wrapper as flood-waited so _pick_any skips it."""
        import time as _time
        wrapper._flood_until = _time.time() + seconds
        logger.warning(
            "user_client.flood_marked",
            account_id=wrapper.account_id,
            wait_seconds=seconds,
            available_after=f"{seconds // 60}m",
        )

    @staticmethod
    def _coerce_entity_key(group_id: str) -> Any:
        """Convert a stored group identifier into something Telethon can resolve.

        Telegram channel/supergroup IDs are stored as bare positive integers
        (e.g. 1345310271) but Telethon needs the -100 prefix format
        (e.g. -1001345310271) to resolve them correctly.
        """
        if group_id is None:
            return ""
        s = str(group_id).strip()
        # Numeric chat id?
        if s.lstrip("-").isdigit():
            n = int(s)
            if n < 0:
                return n
            # Large positive = bare channel/supergroup id — add -100 prefix
            if n > 1_000_000_000:
                return int(f"-100{n}")
            return n
        # Strip @ for usernames
        return s.lstrip("@")

    async def _simulate_typing(
        self, wrapper: TDLibClient, entity: Any, text: str
    ) -> None:
        duration = min(max(len(text) * _TYPING_CHAR_DELAY, _TYPING_MIN), _TYPING_MAX)
        duration += random.uniform(0.2, 0.8)
        try:
            async with wrapper.client.action(entity, "typing"):
                await asyncio.sleep(duration)
        except Exception:
            pass  # typing simulation is best-effort

    @staticmethod
    async def _safe_stop(wrapper: TDLibClient) -> None:
        try:
            await wrapper.client.disconnect()
        except Exception:
            pass
        wrapper._authorized = False
