"""Official Telegram Bot client for Atlas game.

Uses python-telegram-bot v21+ async Application builder pattern.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
)

from src.config import settings

logger = structlog.get_logger(__name__)

# ---- Constants --------------------------------------------------------

MINI_APP_URL = settings.product_api_url.replace("api.", "app.")  # e.g. https://app.atlas.game
ROOMS = {
    "coin": {"emoji": "\U0001fa99", "name": "Coin Room", "color": "#00c853"},
    "fast": {"emoji": "\u26a1", "name": "Fast Room", "color": "#ff6b00"},
    "standard": {"emoji": "\U0001f3af", "name": "Standard Room", "color": "#ff0040"},
    "premium": {"emoji": "\U0001f48e", "name": "Premium Room", "color": "#ff0080"},
}


class OfficialBot:
    """Atlas official Telegram Bot.

    Provides command handlers, inline queries, and notification push helpers
    that connect players to the Mini App front-end.
    """

    def __init__(self, token: str | None = None) -> None:
        self._token = token or settings.tg_bot_token
        self._app: Application | None = None
        self._http = httpx.AsyncClient(
            base_url=settings.product_api_url,
            headers={"Authorization": f"Bearer {settings.product_api_key}"},
            timeout=10,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build and start the bot application (long-polling mode)."""
        builder = Application.builder().token(self._token)
        self._app = builder.build()

        # Register handlers
        self._app.add_handler(CommandHandler("start", self.cmd_start))
        self._app.add_handler(CommandHandler("balance", self.cmd_balance))
        self._app.add_handler(CommandHandler("history", self.cmd_history))
        self._app.add_handler(CommandHandler("referral", self.cmd_referral))
        self._app.add_handler(CommandHandler("stats", self.cmd_stats))
        self._app.add_handler(CommandHandler("leaderboard", self.cmd_leaderboard))
        self._app.add_handler(CommandHandler("help", self.cmd_help))
        self._app.add_handler(InlineQueryHandler(self.inline_query))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("bot.started")

    async def stop(self) -> None:
        """Gracefully stop the bot."""
        if self._app is None:
            return
        if self._app.updater and self._app.updater.running:
            await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()
        await self._http.aclose()
        logger.info("bot.stopped")

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/start -- greet the user and show the Mini App button."""
        if update.effective_message is None:
            return

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "\U0001f534 Open Atlas",
                web_app=WebAppInfo(url=MINI_APP_URL),
            )],
            [
                InlineKeyboardButton("\U0001f4d6 Rules", callback_data="rules"),
                InlineKeyboardButton("\U0001f517 Invite Friends", callback_data="referral"),
            ],
        ])

        # Handle deep-link referral: /start ref_<referrer_id>
        referral_code = ""
        if context.args and context.args[0].startswith("ref_"):
            referral_code = context.args[0]
            try:
                await self._http.post("/referrals/track", json={
                    "referrer_code": referral_code,
                    "new_user_tg_id": update.effective_user.id,
                })
            except Exception:
                logger.exception("bot.referral_track_error", code=referral_code)

        await update.effective_message.reply_text(
            "\U0001f534 *Welcome to Atlas!*\n\n"
            "Press Atlas. Reset the timer. Win the prize.\n"
            "Zero learning curve \u2014 everyone understands *press Atlas*.\n\n"
            "\u26a1 Choose a room and start playing!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )
        logger.info("bot.cmd_start", user_id=update.effective_user.id, referral=referral_code)

    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/balance -- show the user's current balances."""
        if update.effective_message is None:
            return

        user_id = update.effective_user.id
        data = await self._api_get(f"/users/{user_id}/balance")
        if data is None:
            await update.effective_message.reply_text(
                "\u274c Could not fetch your balance. Are you registered?",
            )
            return

        u_balance = data.get("u_balance", 0)
        coin_balance = data.get("coin_balance", 0)
        await update.effective_message.reply_text(
            "\U0001f4b0 *Your Balance*\n\n"
            f"\U0001fa99 Coins: *{coin_balance:,.0f}*\n"
            f"\U0001f4b2 U Tokens: *{u_balance:,.2f} U*",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/history -- show recent game history."""
        if update.effective_message is None:
            return

        user_id = update.effective_user.id
        data = await self._api_get(f"/users/{user_id}/history", params={"limit": 5})
        if not data:
            await update.effective_message.reply_text("No game history found.")
            return

        rounds = data if isinstance(data, list) else data.get("rounds", [])
        lines = ["\U0001f4dc *Recent Games*\n"]
        for r in rounds[:5]:
            room_info = ROOMS.get(r.get("room", "standard"), ROOMS["standard"])
            result = "\U0001f3c6 Won!" if r.get("won") else "\u274c"
            lines.append(
                f"{room_info['emoji']} {room_info['name']} \u2014 "
                f"Round #{r.get('round_id', '?')} {result}"
            )
        await update.effective_message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_referral(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/referral -- generate and display the user's invite link."""
        if update.effective_message is None:
            return

        user_id = update.effective_user.id
        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start=ref_{user_id}"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f4e4 Share", switch_inline_query=f"Join Atlas! {link}")],
        ])

        await update.effective_message.reply_text(
            "\U0001f517 *Your Referral Link*\n\n"
            f"`{link}`\n\n"
            "\U0001f381 Rewards:\n"
            "\u2022 L1 referral: 5% commission\n"
            "\u2022 L2 referral: 2% commission\n"
            "\u2022 L3 referral: 1% commission\n"
            "\u2022 +10,000 coins per active referral",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/stats -- personal statistics."""
        if update.effective_message is None:
            return

        user_id = update.effective_user.id
        data = await self._api_get(f"/users/{user_id}/stats")
        if data is None:
            await update.effective_message.reply_text("\u274c Could not fetch stats.")
            return

        await update.effective_message.reply_text(
            "\U0001f4ca *Your Stats*\n\n"
            f"\U0001f3af Total clicks: *{data.get('total_clicks', 0):,}*\n"
            f"\U0001f3c6 Wins: *{data.get('wins', 0)}*\n"
            f"\U0001f4b0 Total earned: *{data.get('total_earned', 0):,.2f} U*\n"
            f"\U0001f4b8 Total dividends: *{data.get('total_dividends', 0):,.2f} U*\n"
            f"\u2b50 Level: *{data.get('level', 1)}*\n"
            f"\U0001f465 Referrals: *{data.get('referral_count', 0)}*",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_leaderboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/leaderboard -- show top players in the current chat."""
        if update.effective_message is None:
            return

        chat_id = update.effective_chat.id
        data = await self._api_get(f"/leaderboard", params={"chat_id": chat_id, "limit": 10})
        if not data:
            await update.effective_message.reply_text("No leaderboard data available yet.")
            return

        entries = data if isinstance(data, list) else data.get("entries", [])
        lines = ["\U0001f3c6 *Leaderboard*\n"]
        medals = ["\U0001f947", "\U0001f948", "\U0001f949"]
        for i, entry in enumerate(entries[:10]):
            prefix = medals[i] if i < 3 else f"  {i + 1}."
            name = entry.get("display_name", "Anonymous")
            earned = entry.get("total_earned", 0)
            lines.append(f"{prefix} {name} \u2014 *{earned:,.2f} U*")

        await update.effective_message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/help -- display available commands."""
        if update.effective_message is None:
            return

        await update.effective_message.reply_text(
            "\U0001f4ac *Atlas \u2014 Commands*\n\n"
            "/start \u2014 Open the game\n"
            "/balance \u2014 Check your balance\n"
            "/history \u2014 Recent games\n"
            "/referral \u2014 Get your invite link\n"
            "/stats \u2014 Personal statistics\n"
            "/leaderboard \u2014 Top players\n"
            "/help \u2014 This message\n\n"
            "\U0001f3ae *Rooms:*\n"
            "\U0001fa99 Coin Room \u2014 Free coins, learn the game\n"
            "\u26a1 Fast Room \u2014 Quick rounds, low stakes\n"
            "\U0001f3af Standard Room \u2014 Balanced gameplay\n"
            "\U0001f48e Premium Room \u2014 High stakes, big prizes",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ------------------------------------------------------------------
    # Inline mode
    # ------------------------------------------------------------------

    async def inline_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline queries -- show live room status cards."""
        if update.inline_query is None:
            return

        data = await self._api_get("/rooms/status")
        rooms_data = (data if isinstance(data, list) else data.get("rooms", [])) if data else []

        results: list[InlineQueryResultArticle] = []
        for room in rooms_data:
            room_key = room.get("type", "standard")
            info = ROOMS.get(room_key, ROOMS["standard"])
            stage = room.get("stage", 1)
            prize = room.get("prize_pool", 0)
            clicks = room.get("total_clicks", 0)
            countdown = room.get("countdown", "?")

            text = (
                f"{info['emoji']} *{info['name']}*\n\n"
                f"\u23f1 Countdown: *{countdown}s*\n"
                f"\U0001f3ad Stage: *{stage}/5*\n"
                f"\U0001f4b0 Prize Pool: *{prize:,.2f}*\n"
                f"\U0001f446 Clicks: *{clicks:,}*\n\n"
                f"[Play Now]({MINI_APP_URL}?room={room_key})"
            )

            results.append(InlineQueryResultArticle(
                id=room_key,
                title=f"{info['emoji']} {info['name']} \u2014 Stage {stage}",
                description=f"Prize: {prize:,.2f} | Clicks: {clicks:,}",
                input_message_content=InputTextMessageContent(
                    text, parse_mode=ParseMode.MARKDOWN,
                ),
            ))

        # Fallback when API is down
        if not results:
            for key, info in ROOMS.items():
                results.append(InlineQueryResultArticle(
                    id=key,
                    title=f"{info['emoji']} {info['name']}",
                    description="Tap to share room link",
                    input_message_content=InputTextMessageContent(
                        f"{info['emoji']} *{info['name']}* \u2014 "
                        f"[Play Now]({MINI_APP_URL}?room={key})",
                        parse_mode=ParseMode.MARKDOWN,
                    ),
                ))

        await update.inline_query.answer(results, cache_time=15)

    # ------------------------------------------------------------------
    # Notification push helpers
    # ------------------------------------------------------------------

    async def send_stage_alert(self, user_id: int, room: str, stage: int) -> None:
        """Push a stage 4/5 urgency alert to the user."""
        if self._app is None:
            logger.warning("bot.not_started")
            return

        info = ROOMS.get(room, ROOMS["standard"])
        urgency = "\U0001f6a8 FINAL STAGE!" if stage == 5 else "\u26a0\ufe0f Stage 4 \u2014 Almost Over!"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"{info['emoji']} Press NOW!",
                web_app=WebAppInfo(url=f"{MINI_APP_URL}?room={room}"),
            )],
        ])

        try:
            await self._app.bot.send_message(
                chat_id=user_id,
                text=(
                    f"{urgency}\n\n"
                    f"{info['emoji']} *{info['name']}* has entered Stage {stage}!\n"
                    "The timer is running out \u2014 press Atlas now or miss your chance!"
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
            )
            logger.info("bot.stage_alert_sent", user_id=user_id, room=room, stage=stage)
        except Exception:
            logger.exception("bot.stage_alert_error", user_id=user_id)

    async def send_win_notification(self, user_id: int, amount: float, room: str) -> None:
        """Notify a user that they won a prize."""
        if self._app is None:
            return

        info = ROOMS.get(room, ROOMS["standard"])
        try:
            await self._app.bot.send_message(
                chat_id=user_id,
                text=(
                    "\U0001f389\U0001f3c6 *Congratulations!*\n\n"
                    f"You won *{amount:,.2f} U* in {info['emoji']} {info['name']}!\n\n"
                    "Your winnings have been credited to your balance."
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
            logger.info("bot.win_notification_sent", user_id=user_id, amount=amount, room=room)
        except Exception:
            logger.exception("bot.win_notification_error", user_id=user_id)

    async def send_dividend_notification(self, user_id: int, amount: float) -> None:
        """Notify a user of dividend payout."""
        if self._app is None:
            return

        try:
            await self._app.bot.send_message(
                chat_id=user_id,
                text=(
                    "\U0001f4b8 *Dividend Received!*\n\n"
                    f"You earned *{amount:,.4f} U* in dividends.\n"
                    "Dividends are distributed equally by click count."
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
            logger.info("bot.dividend_notification_sent", user_id=user_id, amount=amount)
        except Exception:
            logger.exception("bot.dividend_notification_error", user_id=user_id)

    # ------------------------------------------------------------------
    # Internal API helpers
    # ------------------------------------------------------------------

    async def _api_get(self, path: str, params: dict[str, Any] | None = None) -> Any | None:
        """GET from the game API.  Returns parsed JSON or ``None`` on failure."""
        try:
            resp = await self._http.get(path, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            logger.exception("bot.api_get_error", path=path)
            return None

    async def _api_post(self, path: str, json: dict[str, Any] | None = None) -> Any | None:
        try:
            resp = await self._http.post(path, json=json)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            logger.exception("bot.api_post_error", path=path)
            return None
