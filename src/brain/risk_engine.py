"""Risk Engine v2 - Multi-dimensional risk assessment for Telegram accounts.

Evaluates account behaviour across 8 dimensions and returns a composite
risk score in the range [0, 1].  The score drives automated actions:
slow-down, strict mode, hibernation, or account abandonment.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from src.brain.age_policy import AgePolicy
from src.config import settings


# ---------------------------------------------------------------------------
# High-risk virtual phone providers (commonly flagged by Telegram)
# ---------------------------------------------------------------------------
HIGH_RISK_PROVIDERS: set[str] = {
    "textnow",
    "google voice",
    "talkatone",
    "textplus",
    "dingtone",
    "textnow",
    "hushed",
    "burner",
    "2ndline",
}


# ---------------------------------------------------------------------------
# Public data models
# ---------------------------------------------------------------------------

class RiskLevel(Enum):
    """Discrete risk levels derived from a 0-1 continuous score."""

    NORMAL = "normal"          # < threshold_slow  (default 0.4)
    SLOW_DOWN = "slow_down"    # threshold_slow - threshold_strict  -> reduce rate 30 %
    STRICT = "strict"          # threshold_strict - threshold_hibernate -> reduce rate 50 %
    HIBERNATE = "hibernate"    # threshold_hibernate - threshold_abandon -> sleep 72 h
    ABANDON = "abandon"        # > threshold_abandon -> discard account


class RiskAssessment(BaseModel):
    """Immutable result of a single risk evaluation."""

    account_id: int
    risk_score: float = Field(ge=0.0, le=1.0)
    risk_level: RiskLevel
    factors: dict[str, float]        # per-dimension raw scores
    weighted_factors: dict[str, float]  # per-dimension weighted contributions
    recommendations: list[str]
    assessed_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Dimension weights (must sum to 1.0)
# ---------------------------------------------------------------------------
_WEIGHTS: dict[str, float] = {
    "frequency": 0.20,
    "group": 0.15,
    "content": 0.20,
    "report": 0.20,
    "dm": 0.10,
    "phone": 0.05,
    "pattern": 0.05,
    "similarity": 0.05,
}


class RiskEngine:
    """Stateless multi-dimensional risk evaluator.

    All thresholds are read from *settings* so they can be tuned via env
    vars without code changes.
    """

    def __init__(self, cfg: Any | None = None) -> None:
        self._cfg = cfg or settings
        self._thresholds = {
            RiskLevel.SLOW_DOWN: self._cfg.risk_threshold_slow,
            RiskLevel.STRICT: self._cfg.risk_threshold_strict,
            RiskLevel.HIBERNATE: self._cfg.risk_threshold_hibernate,
            RiskLevel.ABANDON: self._cfg.risk_threshold_abandon,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def evaluate(self, account_data: dict) -> RiskAssessment:
        """Run all 8 risk dimensions and return a weighted composite score.

        ``account_data`` is expected to contain the fields present on the
        ``Account`` ORM model plus optional supplementary keys:

        * ``message_timestamps`` – ``list[datetime]`` of recent send times
        * ``content_hashes``     – ``list[str]`` of recent content hashes
        * ``account_age_days``   – ``int`` account age in days (for age-aware policy)
        * ``age_tier``           – ``str`` pre-computed age tier (overrides age_days)
        """

        # Resolve age-tier policy for this account
        age_tier = account_data.get("age_tier") or AgePolicy.get_tier(
            account_data.get("account_age_days", 0),
        )
        policy = AgePolicy.get_policy(age_tier)

        raw_scores: dict[str, float] = {
            "frequency": self._assess_frequency_risk(account_data, policy),
            "group": self._assess_group_risk(account_data, policy),
            "content": self._assess_content_risk(account_data, policy),
            "report": self._assess_report_risk(account_data),
            "dm": self._assess_dm_risk(account_data, policy),
            "phone": self._assess_phone_risk(account_data, policy),
            "pattern": self._assess_pattern_risk(
                account_data.get("message_timestamps", []),
            ),
            "similarity": self._assess_similarity_risk(
                account_data.get("content_hashes", []),
            ),
        }

        # Clamp each raw score to [0, 1]
        raw_scores = {k: max(0.0, min(1.0, v)) for k, v in raw_scores.items()}

        weighted: dict[str, float] = {
            k: raw_scores[k] * _WEIGHTS[k] for k in raw_scores
        }

        composite = sum(weighted.values())
        # Clamp final composite just in case
        composite = max(0.0, min(1.0, composite))

        level = self.get_risk_level(composite)
        recs = self.get_recommendations(composite, raw_scores)

        return RiskAssessment(
            account_id=account_data.get("id", 0),
            risk_score=round(composite, 4),
            risk_level=level,
            factors=raw_scores,
            weighted_factors={k: round(v, 4) for k, v in weighted.items()},
            recommendations=recs,
        )

    # ------------------------------------------------------------------
    # Dimension assessors (each returns a raw 0-1 score)
    # ------------------------------------------------------------------

    def _assess_frequency_risk(self, data: dict, policy: dict) -> float:
        """Messages sent today, thresholds driven by age-tier policy."""
        sent = data.get("messages_sent_today", 0)
        limit = policy.get("max_messages_per_day", self._cfg.max_messages_per_day)
        if sent > limit:
            return 0.3
        if sent > limit * 0.66:
            return 0.15
        return 0.0

    def _assess_group_risk(self, data: dict, policy: dict) -> float:
        """Active/new groups today, thresholds driven by age-tier policy."""
        score = 0.0
        active = data.get("groups_active_today", 0)
        new = data.get("new_groups_today", 0)
        max_groups = policy.get("max_groups_per_day", self._cfg.max_groups_per_day)
        max_new = policy.get("max_new_groups_per_day", 3)

        if active > max_groups:
            score += 0.3
        elif active > max(max_groups - 2, 1):
            score += 0.1

        if new > max_new:
            score += 0.2

        return min(score, 1.0)

    def _assess_content_risk(self, data: dict, policy: dict) -> float:
        """Outreachal ratio and link volume, thresholds driven by age-tier policy."""
        score = 0.0

        total = data.get("messages_sent_today", 0)
        promo = data.get("outreach_messages_today", 0)
        outreach_ratio = promo / total if total > 0 else 0.0
        ratio_limit = policy.get("outreach_ratio_limit", self._cfg.outreach_ratio_limit)

        if outreach_ratio > ratio_limit * 1.5:
            score += 0.25
        elif outreach_ratio > ratio_limit:
            score += 0.1

        links = data.get("links_sent_today", 0)
        max_links = policy.get("max_links_per_day", self._cfg.max_links_per_day)
        if links > max_links:
            score += 0.3

        return min(score, 1.0)

    def _assess_report_risk(self, data: dict) -> float:
        """User reports and kicks -- strongest single signal."""
        score = 0.0

        if data.get("reported", False):
            score += 0.5

        kicked = data.get("kicked_count", 0)
        if kicked > 2:
            score += 0.25

        return min(score, 1.0)

    def _assess_dm_risk(self, data: dict, policy: dict) -> float:
        """Unsolicited DMs today, threshold driven by age-tier policy."""
        dms = data.get("dms_initiated_today", 0)
        max_dms = policy.get("max_dms_per_day", self._cfg.max_dms_per_day)
        if dms > max_dms:
            return 0.25
        return 0.0

    def _assess_phone_risk(self, data: dict, policy: dict) -> float:
        """Virtual numbers and high-risk VoIP providers, weight scaled by age tier."""
        score = 0.0
        weight = policy.get("risk_weight_phone", 0.05)
        # Scale base penalties by the age-tier phone weight (normalised to default 0.05)
        scale = weight / 0.05 if weight else 1.0
        if data.get("phone_type", "physical_sim") == "virtual":
            score += 0.1 * scale
        provider = (data.get("phone_provider") or "").lower().strip()
        if provider in HIGH_RISK_PROVIDERS:
            score += 0.15 * scale
        return min(score, 1.0)

    def _assess_pattern_risk(self, timestamps: list[datetime]) -> float:
        """Detect suspiciously regular message intervals.

        If the standard deviation of inter-message gaps is very small
        relative to the mean, the pattern looks bot-like.
        """
        if len(timestamps) < 5:
            return 0.0

        sorted_ts = sorted(timestamps)
        gaps = [
            (sorted_ts[i + 1] - sorted_ts[i]).total_seconds()
            for i in range(len(sorted_ts) - 1)
        ]

        if not gaps:
            return 0.0

        mean_gap = statistics.mean(gaps)
        if mean_gap == 0:
            return 0.2  # all messages at the same second -- clearly scripted

        stdev_gap = statistics.pstdev(gaps)
        # Coefficient of variation (CV).  A human typically has CV > 0.5.
        cv = stdev_gap / mean_gap if mean_gap > 0 else 0.0

        if cv < 0.15:
            return 0.2   # very regular -- almost certainly automated
        if cv < 0.3:
            return 0.1   # somewhat regular
        return 0.0

    def _assess_similarity_risk(self, content_hashes: list[str]) -> float:
        """Cross-account content similarity.

        If many hashes are identical (same message reused across accounts)
        the account cluster is at risk.
        """
        if len(content_hashes) < 3:
            return 0.0

        unique = set(content_hashes)
        similarity = 1.0 - (len(unique) / len(content_hashes))

        if similarity > 0.6:
            return 0.3
        if similarity > 0.4:
            return 0.15
        return 0.0

    # ------------------------------------------------------------------
    # Level / recommendation helpers
    # ------------------------------------------------------------------

    def get_risk_level(self, score: float) -> RiskLevel:
        """Map a continuous score to a discrete ``RiskLevel``."""
        if score >= self._thresholds[RiskLevel.ABANDON]:
            return RiskLevel.ABANDON
        if score >= self._thresholds[RiskLevel.HIBERNATE]:
            return RiskLevel.HIBERNATE
        if score >= self._thresholds[RiskLevel.STRICT]:
            return RiskLevel.STRICT
        if score >= self._thresholds[RiskLevel.SLOW_DOWN]:
            return RiskLevel.SLOW_DOWN
        return RiskLevel.NORMAL

    def get_recommendations(self, score: float, factors: dict[str, float]) -> list[str]:
        """Return a list of actionable recommendations given the score and raw factor breakdown."""
        recs: list[str] = []
        level = self.get_risk_level(score)

        # Level-driven recommendations
        if level == RiskLevel.ABANDON:
            recs.append("CRITICAL: Abandon this account immediately. Do NOT send further messages.")
            return recs

        if level == RiskLevel.HIBERNATE:
            recs.append("Hibernate account for 72 hours. Rotate proxy/IP before resuming.")

        if level in (RiskLevel.STRICT, RiskLevel.SLOW_DOWN):
            pct = "50%" if level == RiskLevel.STRICT else "30%"
            recs.append(f"Reduce messaging frequency by {pct}.")

        # Factor-specific recommendations
        if factors.get("frequency", 0) >= 0.15:
            recs.append("Message volume too high -- spread activity over more hours.")

        if factors.get("group", 0) >= 0.2:
            recs.append("Slow down group joins. Max 2 new groups per day recommended.")

        if factors.get("content", 0) >= 0.1:
            recs.append("Outreach ratio elevated -- increase organic/chat messages.")

        if factors.get("report", 0) >= 0.25:
            recs.append("Account flagged or kicked multiple times. Consider hibernation.")

        if factors.get("dm", 0) >= 0.1:
            recs.append("Reduce unsolicited DMs. Engage in group first before DMing.")

        if factors.get("phone", 0) >= 0.1:
            recs.append("Virtual/high-risk phone number. Prioritize physical SIM accounts.")

        if factors.get("pattern", 0) >= 0.1:
            recs.append("Message timing too regular -- add jitter/randomness to intervals.")

        if factors.get("similarity", 0) >= 0.15:
            recs.append("Content reuse detected across accounts -- generate unique variants.")

        if not recs:
            recs.append("Account health is good. Continue normal operations.")

        return recs
