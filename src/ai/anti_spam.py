"""Anti-spam & anti-detection engine – humanises messages and prevents fingerprinting."""

from __future__ import annotations

import hashlib
import math
import random
import re
import struct
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from datasketch import MinHash

if TYPE_CHECKING:
    from src.ai.persona import PersonaTemplate


# ---------------------------------------------------------------------------
# Chinese / English typo tables
# ---------------------------------------------------------------------------

# Common adjacent-key typos on QWERTY
_QWERTY_NEIGHBOURS: dict[str, str] = {
    "a": "sq", "b": "vn", "c": "xv", "d": "sf", "e": "wr", "f": "dg",
    "g": "fh", "h": "gj", "i": "uo", "j": "hk", "k": "jl", "l": "k;",
    "m": "n,", "n": "bm", "o": "ip", "p": "o[", "q": "wa", "r": "et",
    "s": "ad", "t": "ry", "u": "yi", "v": "cb", "w": "qe", "x": "zc",
    "y": "tu", "z": "x",
}

# Common Chinese character typo pairs (phonetically or visually similar)
_ZH_TYPO_PAIRS: list[tuple[str, str]] = [
    ("的", "地"), ("地", "得"), ("得", "的"),
    ("在", "再"), ("再", "在"),
    ("做", "作"), ("作", "做"),
    ("那", "哪"), ("哪", "那"),
    ("他", "她"), ("她", "他"),
    ("呢", "ne"), ("吧", "ba"),
    ("了", "啦"), ("啦", "了"),
    ("是", "事"), ("以", "已"),
]

# Casual / slang substitutions
_CASUAL_SUBS_ZH: list[tuple[str, str]] = [
    ("什么", "啥"), ("非常", "超级"), ("知道", "晓得"),
    ("没有", "没"), ("可以", "行"), ("为什么", "咋"),
    ("不知道", "不晓得"), ("然后", "然后嘛"),
    ("真的", "真的吗"), ("是的", "嗯嗯"),
]

_CASUAL_SUBS_EN: list[tuple[str, str]] = [
    ("going to", "gonna"), ("want to", "wanna"),
    ("don't know", "dunno"), ("because", "cuz"),
    ("though", "tho"), ("right", "rite"),
    ("really", "rly"), ("probably", "prolly"),
    ("something", "smth"), ("everyone", "evry1"),
]


# ---------------------------------------------------------------------------
# Emoji variation pools by style level
# ---------------------------------------------------------------------------

_EMOJI_POOL_MINIMAL = ["👀", "🤔", "😂", "👍", "🙏"]
_EMOJI_POOL_MODERATE = [
    "✅", "❌", "🔥", "💰", "📌", "👀", "🤔", "😂", "💡", "⚡",
    "📊", "🎯", "🚀", "💪", "🤝",
]
_EMOJI_POOL_HEAVY = [
    "😂", "🤣", "💀", "🔥", "❤️", "👏", "😱", "🫡", "😭", "💪",
    "🎉", "😤", "🥲", "🤯", "😈", "✨", "🙈", "👀", "🫠", "💅",
]


class AntiSpamEngine:
    """Utilities to make AI-generated messages look human and avoid detection."""

    # ------------------------------------------------------------------
    # Quick spam-score check (used by agents)
    # ------------------------------------------------------------------

    async def check(self, content: str) -> float:
        """Return a heuristic spam score in [0.0, 1.0] for *content*.

        This is a fast, rule-based check. For LLM-based scoring use
        ``ContentGenerator.check_spam_score`` instead.

        Signals checked:
        - Contains URLs / links
        - Marketing phrases
        - Too many emojis
        - Very short or empty
        """
        score = 0.0
        text_lower = content.lower()

        # Links
        if re.search(r"https?://|t\.me/|bit\.ly|tinyurl", text_lower):
            score += 0.4

        # Marketing phrases (zh + en)
        spam_phrases = [
            "百倍", "财富密码", "上车", "稳赚", "包赚", "注册链接",
            "100x", "guaranteed", "sign up now", "join now", "free money",
            "act now", "limited time", "don't miss",
        ]
        hits = sum(1 for phrase in spam_phrases if phrase in text_lower)
        score += min(hits * 0.15, 0.5)

        # Excessive emojis (>5 in a short message)
        emoji_count = len(re.findall(r"[\U0001f300-\U0001f9ff]", content))
        if emoji_count > 5 and len(content) < 100:
            score += 0.1

        # Very short messages are unlikely spam
        if len(content.strip()) < 5:
            score = max(score - 0.2, 0.0)

        return max(0.0, min(1.0, score))

    # ------------------------------------------------------------------
    # Message humanisation
    # ------------------------------------------------------------------

    def humanize_message(self, message: str, persona: PersonaTemplate) -> str:
        """Apply human-like imperfections to a message based on the persona profile.

        Transformations (applied probabilistically):
        - Inject typos at *persona.typo_rate*
        - Swap to casual / slang expressions
        - Vary emoji usage
        - Occasionally drop punctuation or add filler words
        """
        text = message

        # Determine language heuristic
        is_chinese = self._is_mostly_chinese(text)

        # 1. Casual substitutions (30 % chance per candidate)
        subs = _CASUAL_SUBS_ZH if is_chinese else _CASUAL_SUBS_EN
        for formal, casual in subs:
            if formal in text and random.random() < 0.30:
                text = text.replace(formal, casual, 1)

        # 2. Typos
        if persona.typo_rate > 0:
            text = self._inject_typos(text, persona.typo_rate, is_chinese)

        # 3. Emoji variation
        text = self._vary_emojis(text, persona.emoji_style)

        # 4. Punctuation casualness (20 % chance)
        if random.random() < 0.20:
            text = self._casualize_punctuation(text, is_chinese)

        # 5. Occasional filler words (15 % chance, Chinese only)
        if is_chinese and random.random() < 0.15:
            text = self._add_filler_zh(text)

        return text.strip()

    # ------------------------------------------------------------------
    # Typing delay
    # ------------------------------------------------------------------

    def calculate_typing_delay(self, message: str, persona: PersonaTemplate) -> float:
        """Return a realistic typing delay in seconds.

        Based on character count, persona speed, and random jitter.
        Average human typing: ~40 WPM English / ~30 chars/min Chinese.
        """
        char_count = len(message)
        is_chinese = self._is_mostly_chinese(message)

        # Base chars-per-second by persona speed
        speed_map = {
            "slow": (1.5, 2.5) if is_chinese else (3.0, 5.0),
            "normal": (2.5, 4.0) if is_chinese else (5.0, 8.0),
            "fast": (4.0, 6.0) if is_chinese else (8.0, 12.0),
        }
        low, high = speed_map.get(persona.typing_speed, speed_map["normal"])
        cps = random.uniform(low, high)

        base_delay = char_count / cps

        # Add "thinking" delay (0.5 – 3 seconds)
        thinking = random.uniform(0.5, 3.0)

        # Occasional pause mid-typing (10 % chance, adds 1-5 seconds)
        pause = random.uniform(1.0, 5.0) if random.random() < 0.10 else 0.0

        total = base_delay + thinking + pause

        # Clamp to reasonable bounds
        return max(1.0, min(total, 45.0))

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    @staticmethod
    def randomize_schedule(base_time: datetime, variance_minutes: int = 30) -> datetime:
        """Offset *base_time* by a normally-distributed random delta.

        The standard deviation is ``variance_minutes / 2`` so ~95 % of values
        fall within the given variance window.
        """
        sigma = variance_minutes / 2.0
        offset_minutes = random.gauss(0, sigma)
        # Clamp to [-3*sigma, +3*sigma] to avoid extreme outliers
        offset_minutes = max(-3 * sigma, min(3 * sigma, offset_minutes))
        return base_time + timedelta(minutes=offset_minutes)

    # ------------------------------------------------------------------
    # Content fingerprinting & similarity
    # ------------------------------------------------------------------

    @staticmethod
    def content_fingerprint(content: str) -> str:
        """Return a hex digest fingerprint for deduplication.

        Uses SHA-256 of the normalised (lowercased, whitespace-collapsed) content.
        """
        normalised = re.sub(r"\s+", " ", content.strip().lower())
        return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def similarity_score(content_a: str, content_b: str) -> float:
        """Estimate Jaccard similarity between two texts via MinHash.

        Returns a float in [0.0, 1.0] where 1.0 means identical token sets.
        """
        def _shingles(text: str, k: int = 3) -> set[str]:
            """Extract character-level k-shingles."""
            text = re.sub(r"\s+", " ", text.strip().lower())
            if len(text) < k:
                return {text}
            return {text[i : i + k] for i in range(len(text) - k + 1)}

        shingles_a = _shingles(content_a)
        shingles_b = _shingles(content_b)

        if not shingles_a and not shingles_b:
            return 1.0
        if not shingles_a or not shingles_b:
            return 0.0

        num_perm = 128

        mh_a = MinHash(num_perm=num_perm)
        for s in shingles_a:
            mh_a.update(s.encode("utf-8"))

        mh_b = MinHash(num_perm=num_perm)
        for s in shingles_b:
            mh_b.update(s.encode("utf-8"))

        return float(mh_a.jaccard(mh_b))

    # ------------------------------------------------------------------
    # SimHash (lightweight alternative – no external dep beyond stdlib)
    # ------------------------------------------------------------------

    @staticmethod
    def simhash(text: str, hashbits: int = 64) -> int:
        """Compute a SimHash fingerprint for fast near-duplicate detection.

        Two texts are near-duplicates if the Hamming distance of their
        SimHash values is small (e.g. < 3).
        """
        tokens = re.sub(r"\s+", " ", text.strip().lower()).split()
        v = [0] * hashbits
        for token in tokens:
            h = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)
            for i in range(hashbits):
                bitmask = 1 << i
                if h & bitmask:
                    v[i] += 1
                else:
                    v[i] -= 1
        fingerprint = 0
        for i in range(hashbits):
            if v[i] >= 0:
                fingerprint |= 1 << i
        return fingerprint

    @staticmethod
    def hamming_distance(hash_a: int, hash_b: int) -> int:
        """Count differing bits between two SimHash values."""
        xor = hash_a ^ hash_b
        return bin(xor).count("1")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_mostly_chinese(text: str) -> bool:
        """Return True if the majority of non-whitespace characters are CJK."""
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        alpha = sum(1 for ch in text if ch.isalpha())
        return cjk > alpha * 0.3 if alpha else cjk > 0

    @staticmethod
    def _inject_typos(text: str, rate: float, is_chinese: bool) -> str:
        """Randomly introduce typos at the given rate."""
        if is_chinese:
            for original, replacement in _ZH_TYPO_PAIRS:
                if original in text and random.random() < rate:
                    # Replace only the first occurrence
                    text = text.replace(original, replacement, 1)
                    break  # At most one Chinese typo per message
        else:
            chars = list(text)
            for i, ch in enumerate(chars):
                if ch.lower() in _QWERTY_NEIGHBOURS and random.random() < rate:
                    neighbour = random.choice(_QWERTY_NEIGHBOURS[ch.lower()])
                    chars[i] = neighbour if ch.islower() else neighbour.upper()
                    break  # At most one English typo per message
            text = "".join(chars)
        return text

    @staticmethod
    def _vary_emojis(text: str, style: str) -> str:
        """Occasionally swap or add/remove an emoji to reduce fingerprinting."""
        pool = {
            "minimal": _EMOJI_POOL_MINIMAL,
            "moderate": _EMOJI_POOL_MODERATE,
            "heavy": _EMOJI_POOL_HEAVY,
        }.get(style, _EMOJI_POOL_MODERATE)

        # 15 % chance: append a random emoji if none present
        has_emoji = bool(re.search(r"[\U0001f300-\U0001f9ff]", text))
        if not has_emoji and random.random() < 0.15 and style != "minimal":
            text = text.rstrip() + " " + random.choice(pool)
        elif has_emoji and random.random() < 0.20:
            # Swap an existing emoji for another from the pool
            emojis_in_text = re.findall(r"[\U0001f300-\U0001f9ff]", text)
            if emojis_in_text:
                target = random.choice(emojis_in_text)
                replacement = random.choice(pool)
                text = text.replace(target, replacement, 1)

        return text

    @staticmethod
    def _casualize_punctuation(text: str, is_chinese: bool) -> str:
        """Make punctuation less formal."""
        if is_chinese:
            # Drop trailing period (common in casual chat)
            text = re.sub(r"。$", "", text)
            # Occasionally double a question/exclamation mark
            if random.random() < 0.3:
                text = re.sub(r"？$", "？？", text)
                text = re.sub(r"！$", "！！", text)
        else:
            # Drop trailing period
            if text.endswith(".") and not text.endswith("..."):
                text = text[:-1]
            # Lower-case first char sometimes
            if random.random() < 0.25 and text and text[0].isupper():
                text = text[0].lower() + text[1:]
        return text

    @staticmethod
    def _add_filler_zh(text: str) -> str:
        """Prepend or insert a Chinese filler word."""
        fillers = ["emmm", "话说", "对了", "额", "嗯", "哦对", "诶"]
        filler = random.choice(fillers)
        # 50/50: prepend vs. insert after first clause
        if random.random() < 0.5:
            return f"{filler} {text}"
        # Insert after first comma / space
        for sep in ["，", " ", "、"]:
            if sep in text:
                idx = text.index(sep) + 1
                return text[:idx] + f" {filler} " + text[idx:]
        return f"{filler} {text}"
