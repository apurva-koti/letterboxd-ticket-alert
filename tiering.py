"""How often a film gets checked, based on proximity to its Letterboxd anchor date."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from domain import Tier

RECENT_WINDOW_DAYS = 30
HOT_WINDOW_DAYS = 90
JITTER_FRACTION = 0.15

INTERVALS: dict[Tier, timedelta] = {
    Tier.MUST_WATCH: timedelta(minutes=1),
    Tier.HOT: timedelta(hours=3),
    Tier.RECENT: timedelta(hours=8),
    Tier.FAR_FUTURE: timedelta(hours=24),
    Tier.UNKNOWN: timedelta(hours=24),
    Tier.RETIRED: timedelta(days=7),
}


def compute_tier(anchor: date | None, today: date, is_hype: bool = False) -> Tier:
    if is_hype:
        return Tier.MUST_WATCH
    if anchor is None:
        return Tier.UNKNOWN

    days_until = (anchor - today).days
    if days_until < -RECENT_WINDOW_DAYS:
        return Tier.RETIRED
    if days_until <= 0:
        return Tier.RECENT
    if days_until <= HOT_WINDOW_DAYS:
        return Tier.HOT
    return Tier.FAR_FUTURE


def next_check_at(tier: Tier, now: datetime) -> datetime:
    interval = INTERVALS[tier]
    jitter = interval * random.uniform(-JITTER_FRACTION, JITTER_FRACTION)
    return now + interval + jitter


def is_due(next_check: datetime | None, now: datetime) -> bool:
    return next_check is None or next_check <= now
