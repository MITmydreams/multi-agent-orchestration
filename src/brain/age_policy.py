"""Age Policy Engine - differentiated risk parameters based on account age tier.

Older accounts enjoy more lenient thresholds (shorter nurture, higher daily
limits) because Telegram trusts them more.  Fresh accounts are treated
conservatively to avoid early bans.
"""

from __future__ import annotations


class AgePolicy:
    """Return differentiated risk-control parameters by account age tier."""

    # Per-tier parameter tables
    TIERS: dict[str, dict] = {
        "veteran": {  # 365+ days
            "nurture_days": 3,
            "first_message_day": 2,
            "start_infiltration_day": 5,
            "max_messages_per_day": 40,
            "max_groups_per_day": 8,
            "max_new_groups_per_day": 8,
            "max_dms_per_day": 5,
            "promo_ratio_limit": 0.20,
            "max_links_per_day": 3,
            "report_tolerance": 3,
            "hibernate_hours": 48,
            "trust_score_initial": 0.7,
            "risk_weight_phone": 0.03,
        },
        "mature": {  # 180-364 days
            "nurture_days": 7,
            "first_message_day": 4,
            "start_infiltration_day": 8,
            "max_messages_per_day": 30,
            "max_groups_per_day": 5,
            "max_new_groups_per_day": 3,
            "max_dms_per_day": 3,
            "promo_ratio_limit": 0.15,
            "max_links_per_day": 2,
            "report_tolerance": 2,
            "hibernate_hours": 72,
            "trust_score_initial": 0.5,
            "risk_weight_phone": 0.05,
        },
        "young": {  # 90-179 days
            "nurture_days": 14,
            "first_message_day": 8,
            "start_infiltration_day": 15,
            "max_messages_per_day": 20,
            "max_groups_per_day": 3,
            "max_new_groups_per_day": 2,
            "max_dms_per_day": 2,
            "promo_ratio_limit": 0.10,
            "max_links_per_day": 1,
            "report_tolerance": 1,
            "hibernate_hours": 168,  # 1 week
            "trust_score_initial": 0.3,
            "risk_weight_phone": 0.08,
        },
        "fresh": {  # <90 days
            "nurture_days": 45,
            "first_message_day": 14,
            "start_infiltration_day": 30,
            "max_messages_per_day": 10,
            "max_groups_per_day": 2,
            "max_new_groups_per_day": 1,
            "max_dms_per_day": 1,
            "promo_ratio_limit": 0.05,
            "max_links_per_day": 1,
            "report_tolerance": 1,
            "hibernate_hours": 336,  # 2 weeks
            "trust_score_initial": 0.1,
            "risk_weight_phone": 0.10,
        },
    }

    @classmethod
    def get_policy(cls, age_tier: str) -> dict:
        """Return the full parameter dict for *age_tier*, defaulting to 'fresh'."""
        return cls.TIERS.get(age_tier, cls.TIERS["fresh"])

    @classmethod
    def get_tier(cls, age_days: int) -> str:
        """Derive age tier from raw day count."""
        if age_days >= 365:
            return "veteran"
        if age_days >= 180:
            return "mature"
        if age_days >= 90:
            return "young"
        return "fresh"

    @classmethod
    def can_send_message(cls, age_tier: str, days_since_nurture_start: int) -> bool:
        """Check whether the account has waited long enough to send its first message."""
        policy = cls.get_policy(age_tier)
        return days_since_nurture_start >= policy["first_message_day"]

    @classmethod
    def can_start_infiltration(cls, age_tier: str, days_since_nurture_start: int) -> bool:
        """Check whether the account is ready for infiltration tasks."""
        policy = cls.get_policy(age_tier)
        return days_since_nurture_start >= policy["start_infiltration_day"]

    @classmethod
    def is_nurturing_complete(cls, age_tier: str, days_since_nurture_start: int) -> bool:
        """Check whether the nurturing phase is complete."""
        policy = cls.get_policy(age_tier)
        return days_since_nurture_start >= policy["nurture_days"]
