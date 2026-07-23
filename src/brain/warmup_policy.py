"""Warmup Policy Engine - 7-tier day-by-day nurture schedule.

Complementary to :mod:`age_policy` (which groups accounts into 4 coarse
trust buckets by absolute age).  This module describes the *warmup
progression* a single account walks through over its first couple of
months: from "nursery" (passive lurking only) all the way to "adult"
(fully active).  The scheduler will later consult this policy to decide,
on any given day, how many new groups an account may join and how many
messages it may send.

Two tables coexist on purpose - ``AgePolicy`` answers "how much do we
trust this account overall", while ``WarmupPolicy`` answers "what is
this account allowed to *do today* given its age in days".  The
integration into the scheduler is deferred to a follow-up task.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class WarmupStage:
    """A single tier in the warmup schedule.

    Attributes:
        name: Short identifier (e.g. ``"nursery"``).
        min_age_days: Inclusive lower bound of the age range, in days.
        max_age_days: Inclusive upper bound, or ``None`` for the final
            open-ended tier.
        max_new_groups_per_day: Max new group joins allowed per day.
        max_messages_per_day: Max outgoing messages allowed per day.
        allow_promo: Whether soft outreach content is permitted.
        allow_dm: Whether direct messaging other users is permitted.
        description: Human-readable behavioural guideline.
    """

    name: str
    min_age_days: int
    max_age_days: Optional[int]  # None = unbounded
    max_new_groups_per_day: int
    max_messages_per_day: int
    allow_promo: bool
    allow_dm: bool
    description: str


class WarmupPolicy:
    """Return day-by-day warmup parameters for an account by its age."""

    # Ordered from youngest to oldest; the final tier has an open upper bound.
    STAGES: list[WarmupStage] = [
        WarmupStage("nursery", 0, 2, 0, 0, False, False, "只在线浏览，不动手"),
        WarmupStage("kindergarten", 3, 5, 1, 2, False, False, "只发表情/转发"),
        WarmupStage("primary", 6, 10, 2, 5, False, False, "偶尔回复，不主动起话题"),
        WarmupStage("middle", 11, 20, 3, 10, False, True, "原创内容、加好友"),
        WarmupStage("high", 21, 30, 3, 15, False, True, "进入 trust 阶段"),
        WarmupStage("college", 31, 60, 4, 20, True, True, "可以做软触达"),
        WarmupStage("adult", 61, None, 5, 30, True, True, "完全活跃"),
    ]

    @classmethod
    def get_stage(cls, age_days: int) -> WarmupStage:
        """Return the :class:`WarmupStage` matching *age_days*.

        Negative ages are clamped to 0.  Ages beyond every finite upper
        bound fall into the last tier (``adult``).
        """
        if age_days < 0:
            age_days = 0
        for stage in cls.STAGES:
            if stage.max_age_days is None:
                # Final open-ended tier - any age at or above min belongs here.
                if age_days >= stage.min_age_days:
                    return stage
            elif stage.min_age_days <= age_days <= stage.max_age_days:
                return stage
        # Unreachable given the table covers [0, +inf), but fall back safely.
        return cls.STAGES[-1]

    @classmethod
    def max_new_groups(cls, age_days: int) -> int:
        """Max new group joins allowed today for an account aged *age_days*."""
        return cls.get_stage(age_days).max_new_groups_per_day

    @classmethod
    def max_messages(cls, age_days: int) -> int:
        """Max outgoing messages allowed today for an account aged *age_days*."""
        return cls.get_stage(age_days).max_messages_per_day

    @classmethod
    def can_promote(cls, age_days: int) -> bool:
        """Whether soft outreach is allowed at this age."""
        return cls.get_stage(age_days).allow_promo

    @classmethod
    def can_dm(cls, age_days: int) -> bool:
        """Whether direct messaging is allowed at this age."""
        return cls.get_stage(age_days).allow_dm


if __name__ == "__main__":
    for age in [0, 2, 3, 5, 10, 11, 20, 30, 60, 61, 100, 365]:
        s = WarmupPolicy.get_stage(age)
        print(
            f"age={age:4d} -> {s.name:12s} "
            f"groups/day={s.max_new_groups_per_day} "
            f"msgs/day={s.max_messages_per_day} "
            f"promo={s.allow_promo}"
        )
