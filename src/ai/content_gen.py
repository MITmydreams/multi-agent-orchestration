"""Content generation engine – produces human-like messages driven by persona templates."""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic
import openai
import structlog

from src.ai.persona import PersonaTemplate
from src.config.settings import settings

logger = logging.getLogger(__name__)
slog = structlog.get_logger(__name__)


class ContentGenerator:
    """AI-powered content generator backed by Anthropic Claude (primary) or OpenAI (fallback).

    Supports automatic degradation to a template engine when no API keys are
    configured, or when ``ai_provider`` is set to ``"template"``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        provider: str | None = None,
    ) -> None:
        self.provider = provider or settings.ai_provider
        self.model = settings.ai_model

        # ------------------------------------------------------------------
        # Route: decide between API mode and template mode
        # ------------------------------------------------------------------
        self._mode: str = "template"  # safe default
        self._template: Any = None
        self._anthropic: anthropic.AsyncAnthropic | None = None
        self._openai: openai.AsyncOpenAI | None = None

        if self.provider == "template":
            # Explicitly requested template mode
            self._init_template_mode()
        elif self.provider == "anthropic":
            resolved_key = api_key or settings.anthropic_api_key
            if resolved_key:
                self._mode = "api"
                client_kwargs: dict[str, Any] = {"api_key": resolved_key}
                if settings.anthropic_base_url:
                    client_kwargs["base_url"] = settings.anthropic_base_url
                self._anthropic = anthropic.AsyncAnthropic(**client_kwargs)
                # Fallback client if OpenAI key is available
                if settings.openai_api_key:
                    self._openai = openai.AsyncOpenAI(api_key=settings.openai_api_key)
            else:
                slog.warning("content_gen.no_anthropic_key", msg="No Anthropic API key found, falling back to template mode")
                self._init_template_mode()
        elif self.provider == "openai":
            resolved_key = api_key or settings.openai_api_key
            if resolved_key:
                self._mode = "api"
                self._openai = openai.AsyncOpenAI(api_key=resolved_key)
            else:
                slog.warning("content_gen.no_openai_key", msg="No OpenAI API key found, falling back to template mode")
                self._init_template_mode()
        else:
            # Unknown provider – try template
            slog.warning("content_gen.unknown_provider", provider=self.provider, msg="Unknown provider, using template mode")
            self._init_template_mode()

        slog.info("content_gen.initialized", mode=self._mode, provider=self.provider)

    # ------------------------------------------------------------------
    # Template mode initialisation
    # ------------------------------------------------------------------

    def _init_template_mode(self) -> None:
        """Switch internal state to template mode."""
        self._mode = "template"
        from src.ai.template_engine import TemplateContentEngine
        self._template = TemplateContentEngine()

    # ------------------------------------------------------------------
    # Runtime mode switching
    # ------------------------------------------------------------------

    def switch_mode(self, mode: str, api_key: str = "") -> None:
        """Switch between ``"api"`` and ``"template"`` mode at runtime (hot-switch).

        Args:
            mode: Target mode – ``"api"`` or ``"template"``.
            api_key: When switching to ``"api"``, an API key can be provided.
                     If omitted the key from settings is used.
        """
        if mode == "template":
            self._init_template_mode()
            slog.info("content_gen.switch_mode", new_mode="template")
        elif mode == "api":
            key = api_key or settings.anthropic_api_key
            if not key:
                slog.error("content_gen.switch_mode_failed", msg="Cannot switch to API mode without an API key")
                raise ValueError("Cannot switch to API mode without an API key")
            self._mode = "api"
            switch_kwargs: dict[str, Any] = {"api_key": key}
            if settings.anthropic_base_url:
                switch_kwargs["base_url"] = settings.anthropic_base_url
            self._anthropic = anthropic.AsyncAnthropic(**switch_kwargs)
            if settings.openai_api_key:
                self._openai = openai.AsyncOpenAI(api_key=settings.openai_api_key)
            slog.info("content_gen.switch_mode", new_mode="api")
        else:
            raise ValueError(f"Unknown mode: {mode!r}. Use 'api' or 'template'.")

    # ------------------------------------------------------------------
    # Internal helpers (API mode)
    # ------------------------------------------------------------------

    async def _call_llm(
        self,
        system: str,
        user: str,
        temperature: float = 0.85,
        max_tokens: int = 512,
    ) -> str:
        """Call the configured LLM provider, with automatic fallback."""
        try:
            return await self._call_primary(system, user, temperature, max_tokens)
        except Exception as exc:
            logger.warning("Primary LLM call failed (%s), trying fallback: %s", self.provider, exc)
            return await self._call_fallback(system, user, temperature, max_tokens)

    async def _call_primary(
        self, system: str, user: str, temperature: float, max_tokens: int
    ) -> str:
        if self.provider == "anthropic":
            return await self._call_anthropic(system, user, temperature, max_tokens)
        return await self._call_openai(system, user, temperature, max_tokens)

    async def _call_fallback(
        self, system: str, user: str, temperature: float, max_tokens: int
    ) -> str:
        if self.provider == "anthropic" and self._openai is not None:
            return await self._call_openai(system, user, temperature, max_tokens)
        if self.provider == "openai" and self._anthropic is not None:
            return await self._call_anthropic(system, user, temperature, max_tokens)
        raise RuntimeError("No fallback LLM provider configured")

    async def _call_anthropic(
        self, system: str, user: str, temperature: float, max_tokens: int
    ) -> str:
        assert self._anthropic is not None
        # Models with extended thinking (e.g. MiniMax-M2.7) need extra tokens
        # for the thinking block before the text output
        effective_max = max(max_tokens * 3, 1024)
        response = await self._anthropic.messages.create(
            model=self.model if self.provider == "anthropic" else "claude-sonnet-4-20250514",
            max_tokens=effective_max,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        # Extract text block, skipping thinking blocks (from extended-thinking models)
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text:
            return text
        # Fallback: try first block's text attribute directly
        return getattr(response.content[0], "text", str(response.content[0]))

    async def _call_openai(
        self, system: str, user: str, temperature: float, max_tokens: int
    ) -> str:
        assert self._openai is not None
        response = await self._openai.chat.completions.create(
            model=self.model if self.provider == "openai" else "gpt-4o-mini",
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return response.choices[0].message.content or ""

    @staticmethod
    def _build_few_shot_block(persona: PersonaTemplate) -> str:
        """Format the persona's example messages as few-shot examples."""
        lines = ["Here are examples of how you normally talk in groups (use as style reference, don't copy verbatim):"]
        for i, msg in enumerate(persona.example_messages, 1):
            lines.append(f"{i}. {msg}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate_chat_response(
        self,
        persona: PersonaTemplate,
        context: str,
        recent_messages: list[str],
        language: str = "en",
    ) -> str:
        """Generate a group chat reply that fits the persona and conversation context."""
        if self._mode == "template":
            slog.debug("content_gen.template_mode", method="generate_chat_response")
            return self._template.generate_chat_response(persona.id, context, language)
        return await self._api_generate_chat_response(persona, context, recent_messages, language)

    async def _api_generate_chat_response(
        self,
        persona: PersonaTemplate,
        context: str,
        recent_messages: list[str],
        language: str = "en",
    ) -> str:
        few_shot = self._build_few_shot_block(persona)
        recent_block = "\n".join(f"- {m}" for m in recent_messages[-15:])

        system = (
            f"{persona.system_prompt}\n\n"
            f"{few_shot}\n\n"
            f"Language: English ONLY. Never reply in Chinese or any other language.\n"
            f"你的emoji风格: {persona.emoji_style}\n"
        )

        user_prompt = (
            f"## 当前场景\n{context}\n\n"
            f"## 最近的群聊消息\n{recent_block}\n\n"
            f"## 任务\n"
            f"请以你的人设，写一条自然的群聊回复。要求：\n"
            f"- 只写回复内容本身，不要加引号或前缀\n"
            f"- 控制在1-3句话，像真人打字一样\n"
            f"- 根据上下文决定是否提到 Atlas（大部分时候不需要提）\n"
            f"- 如果最近消息里有人@你或问你问题，优先回答\n"
        )

        return await self._call_llm(system, user_prompt, temperature=0.9, max_tokens=256)

    async def generate_battle_report(
        self,
        round_data: dict[str, Any],
        language: str = "en",
    ) -> str:
        """Generate a real-time battle report for a finished or ongoing round."""
        if self._mode == "template":
            slog.debug("content_gen.template_mode", method="generate_battle_report")
            return self._template.generate_battle_report(round_data, language)
        return await self._api_generate_battle_report(round_data, language)

    async def _api_generate_battle_report(
        self,
        round_data: dict[str, Any],
        language: str = "en",
    ) -> str:
        system = (
            "你是一个游戏社区的实况播报员。你的风格简洁、有感染力，像体育解说一样。\n"
            "不要使用官方广告语。用真实玩家的视角写，带一点激动和悬念感。\n"
            f"Language: English (always reply in English, never Chinese)"
        )

        user_prompt = (
            f"## 本轮数据\n```json\n{json.dumps(round_data, ensure_ascii=False, indent=2)}\n```\n\n"
            f"请写一段简短的战报（2-4句话），要点：\n"
            f"- 提到房间类型、总点击数、关键时刻\n"
            f"- 如果有赢家信息，突出戏剧性\n"
            f"- 像在直播间里实况一样\n"
            f"- 不要加标题或格式标记\n"
        )

        return await self._call_llm(system, user_prompt, temperature=0.85, max_tokens=200)

    async def generate_win_story(
        self,
        win_data: dict[str, Any],
        language: str = "en",
    ) -> str:
        """Generate a first-person winning story."""
        if self._mode == "template":
            slog.debug("content_gen.template_mode", method="generate_win_story")
            return self._template.generate_win_story(win_data, language)
        return await self._api_generate_win_story(win_data, language)

    async def _api_generate_win_story(
        self,
        win_data: dict[str, Any],
        language: str = "en",
    ) -> str:
        system = (
            "你要模拟一个刚在 Atlas 游戏中获奖的玩家，在群里分享自己的经历。\n"
            "写得自然、口语化，像真人在群里激动地分享。\n"
            "不要太夸张，但可以表达惊喜。不要包含任何链接或触达内容。\n"
            f"Language: English (always reply in English, never Chinese)"
        )

        user_prompt = (
            f"## 获奖信息\n```json\n{json.dumps(win_data, ensure_ascii=False, indent=2)}\n```\n\n"
            f"请以第一人称写一段获奖分享（2-4句话）：\n"
            f"- 描述你当时的心情和决策过程\n"
            f"- 提到具体的细节（第几次点击、差点错过等）\n"
            f"- 像在朋友群里炫耀但不过分\n"
        )

        return await self._call_llm(system, user_prompt, temperature=0.9, max_tokens=200)

    async def generate_content_variants(
        self,
        content_type: str,
        data: dict[str, Any],
        language: str = "en",
        count: int = 5,
    ) -> list[str]:
        """Generate multiple distinct variants of the same content for de-fingerprinting."""
        if self._mode == "template":
            slog.debug("content_gen.template_mode", method="generate_content_variants")
            return self._template.generate_content_variants(content_type, data, language, count)
        return await self._api_generate_content_variants(content_type, data, language, count)

    async def _api_generate_content_variants(
        self,
        content_type: str,
        data: dict[str, Any],
        language: str = "en",
        count: int = 5,
    ) -> list[str]:
        system = (
            "你是一个内容变体生成器。给定同一事件，你需要用完全不同的措辞、句式、"
            "视角写出多个版本。每个版本应该：\n"
            "- 表达相同的核心信息\n"
            "- 使用不同的词汇和句式\n"
            "- 长度可以有差异（有的长有的短）\n"
            "- 不能看起来像是模板生成的\n"
            f"Language: English (always reply in English, never Chinese)"
        )

        user_prompt = (
            f"## 内容类型: {content_type}\n"
            f"## 数据\n```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```\n\n"
            f"请生成 {count} 个完全不同的版本。\n"
            f"用 JSON 数组格式返回，例如: [\"版本1\", \"版本2\", ...]\n"
            f"只返回 JSON 数组，不要其他内容。\n"
        )

        raw = await self._call_llm(system, user_prompt, temperature=1.0, max_tokens=1024)

        # Parse the JSON array from the response
        try:
            # Strip markdown code fences if present
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1]
                cleaned = cleaned.rsplit("```", 1)[0]
            variants: list[str] = json.loads(cleaned)
            if isinstance(variants, list):
                return [str(v) for v in variants[:count]]
        except (json.JSONDecodeError, IndexError, TypeError):
            logger.warning("Failed to parse content variants as JSON, splitting by newline")

        # Fallback: split by newlines
        lines = [line.strip().lstrip("0123456789.-) ") for line in raw.strip().split("\n") if line.strip()]
        return lines[:count]

    async def generate_response(
        self,
        persona: PersonaTemplate,
        group_context: str,
        is_outreach: bool = False,
    ) -> str:
        """Generate a contextual group-chat response for use by the ExecutorAgent."""
        if self._mode == "template":
            slog.debug("content_gen.template_mode", method="generate_response")
            context_desc = "Group discussion" + (" (include a subtle mention of Atlas)" if is_outreach else "")
            lang = persona.languages[0] if persona.languages else "zh"
            return self._template.generate_chat_response(persona.id, context_desc, lang)

        recent_lines = [line.strip() for line in group_context.split("\n") if line.strip()]
        context_desc = "Group discussion" + (" (include a subtle mention of Atlas)" if is_outreach else "")
        return await self.generate_chat_response(
            persona=persona,
            context=context_desc,
            recent_messages=recent_lines,
            language=persona.languages[0] if persona.languages else "zh",
        )

    async def generate_outreach(
        self,
        persona: PersonaTemplate,
        approach: str,
        group_id: str,
    ) -> str:
        """Generate a soft outreach message using a specific approach."""
        if self._mode == "template":
            slog.debug("content_gen.template_mode", method="generate_outreach")
            lang = persona.languages[0] if persona.languages else "zh"
            return self._template.generate_outreach(persona.id, approach, lang)

        few_shot = self._build_few_shot_block(persona)
        game_link = settings.product_app_url
        system = (
            f"{persona.system_prompt}\n\n{few_shot}\n\n"
            "你现在需要自然地提到 Atlas 游戏。\n"
            f"游戏链接（只在被要求时分享）: {game_link}\n"
            "核心卖点: 极简社会实验，按按钮重置倒计时，最后按的赢奖池。"
            "金币房完全免费。30%奖池即时分红给所有参与者。\n"
            "规则: 不要像广告，像在群里随便聊天。约30%概率自然带上链接。"
        )
        user_prompt = (
            f"触达方式: {approach}\n"
            f"目标群组ID: {group_id}\n"
            f"游戏链接: {game_link}\n\n"
            "请写一条自然的群聊消息。1-3句话。\n"
            "如果触达方式是 casual_mention 或 ask_for_help，不要带链接。\n"
            "如果是 experience_share 或 data_analysis，约50%概率带上链接。\n"
            "如果是 screenshot_share，一定带链接。"
        )
        return await self._call_llm(system, user_prompt, temperature=0.9, max_tokens=256)

    async def soften_message(
        self,
        message: str,
        persona: PersonaTemplate,
    ) -> str | None:
        """Rewrite a message to sound less like advertising."""
        if self._mode == "template":
            slog.debug("content_gen.template_mode", method="soften_message")
            return self._template.soften_message(message, persona.id)

        system = (
            "你是一个编辑助手。把下面的消息改写得更自然、更口语化，去掉任何营销感。\n"
            f"目标风格: {persona.tone}\n"
            f"Emoji风格: {persona.emoji_style}\n"
            "如果消息中包含链接，保留链接不要删除。改写时让链接看起来是顺便分享的，不是刻意触达。\n"
        )
        user_prompt = f"请改写这条消息:\n\n{message}"
        try:
            return await self._call_llm(system, user_prompt, temperature=0.85, max_tokens=256)
        except Exception:
            return None

    async def generate_link_reply(
        self,
        persona: PersonaTemplate,
        language: str = "zh",
    ) -> str:
        """Generate a reply that includes the game link, for when someone asks about it."""
        game_link = settings.product_app_url
        if self._mode == "template":
            slog.debug("content_gen.template_mode", method="generate_link_reply")
            return self._template.generate_link_reply(language)

        system = (
            f"{persona.system_prompt}\n\n"
            "有人在群里问你推荐的游戏链接。自然地回复，包含链接。\n"
            "不要太热情，像真人随手发的。"
        )
        user_prompt = (
            f"游戏链接: {game_link}\n"
            f"语言: {language}\n\n"
            "请写一条简短的回复（1-2句话），自然地分享这个链接。"
        )
        return await self._call_llm(system, user_prompt, temperature=0.9, max_tokens=128)

    async def generate_meme(self, topic: str, language: str = "zh") -> str:
        """Generate a meme / joke about Atlas or online community culture."""
        if self._mode == "template":
            slog.debug("content_gen.template_mode", method="generate_meme")
            return self._template.generate_meme(topic, language)

        system = (
            "你是一个行业货币社区的段子手。写出真正好笑的、能引起共鸣的段子或梗。\n"
            "不要太长，1-3句话。不要包含链接或明显的触达。\n"
            f"Language: English (always reply in English, never Chinese)"
        )
        user_prompt = f"话题: {topic}\n\n请写一个有趣的段子或梗。"
        return await self._call_llm(system, user_prompt, temperature=1.0, max_tokens=200)

    async def generate_review(self, language: str = "zh") -> str:
        """Generate a data-driven project review / analysis."""
        if self._mode == "template":
            slog.debug("content_gen.template_mode", method="generate_review")
            return self._template.generate_review(language)

        system = (
            "你是一个客观的行业项目分析师。写一段简短的项目分析。\n"
            "要基于数据和逻辑，不要像广告。可以提到优缺点。\n"
            f"Language: English (always reply in English, never Chinese)"
        )
        user_prompt = (
            "请写一段关于 Atlas 项目的客观分析（3-5句话）。\n"
            "重点分析经济模型和机制设计。"
        )
        return await self._call_llm(system, user_prompt, temperature=0.8, max_tokens=400)

    async def generate_event_content(
        self,
        trigger: str,
        data: dict[str, Any],
    ) -> str:
        """Generate event-driven content for a triggered event."""
        if self._mode == "template":
            slog.debug("content_gen.template_mode", method="generate_event_content")
            return self._template.generate_event_content(trigger, data)

        system = (
            "你是一个游戏社区的实时播报员。根据触发事件生成有感染力的短消息。\n"
            "风格简洁、有紧迫感。不要包含链接。\n"
        )
        user_prompt = (
            f"事件类型: {trigger}\n"
            f"事件数据: {json.dumps(data, ensure_ascii=False)}\n\n"
            "请写一条有冲击力的社群消息（1-3句话）。"
        )
        return await self._call_llm(system, user_prompt, temperature=0.9, max_tokens=200)

    async def check_spam_score(self, content: str) -> float:
        """Evaluate how likely a message looks like spam (0.0 = human, 1.0 = obvious spam)."""
        if self._mode == "template":
            slog.debug("content_gen.template_mode", method="check_spam_score")
            return self._template.check_spam_score(content)

        system = (
            "你是一个 Telegram 群管理的反垃圾检测器。\n"
            "评估给定消息的垃圾信息倾向。考虑以下因素：\n"
            "- 是否包含产品链接或邀请码\n"
            "- 是否使用营销话术（财富密码、百倍、上车等）\n"
            "- 是否像模板化的广告\n"
            "- 是否过于夸张或不自然\n"
            "- 自然的对话式分享不算垃圾信息\n"
            "只返回一个 0.0 到 1.0 之间的数字，不要其他内容。"
        )

        user_prompt = f"评估以下消息的垃圾信息倾向分数：\n\n{content}"

        raw = await self._call_llm(system, user_prompt, temperature=0.1, max_tokens=16)

        try:
            score = float(raw.strip())
            return max(0.0, min(1.0, score))
        except ValueError:
            logger.warning("Could not parse spam score from LLM response: %r", raw)
            return 0.5  # Uncertain default
