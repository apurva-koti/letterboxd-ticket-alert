from datetime import date, datetime, timedelta, timezone

import pytest

import tiering
from domain import Tier

TODAY = date(2026, 9, 19)


@pytest.mark.parametrize(
    "anchor,expected",
    [
        (None, Tier.UNKNOWN),
        (TODAY + timedelta(days=1), Tier.HOT),
        (TODAY + timedelta(days=90), Tier.HOT),
        (TODAY + timedelta(days=91), Tier.FAR_FUTURE),
        (TODAY, Tier.RECENT),
        (TODAY - timedelta(days=30), Tier.RECENT),
        (TODAY - timedelta(days=31), Tier.RETIRED),
        (TODAY - timedelta(days=3650), Tier.RETIRED),
    ],
)
def test_compute_tier_boundaries(anchor, expected):
    assert tiering.compute_tier(anchor, TODAY) == expected


@pytest.mark.parametrize("anchor", [None, TODAY - timedelta(days=3650), TODAY + timedelta(days=500)])
def test_compute_tier_hype_overrides_everything(anchor):
    assert tiering.compute_tier(anchor, TODAY, is_hype=True) == Tier.MUST_WATCH


def test_next_check_at_stays_within_jitter_bounds():
    now = datetime(2026, 9, 19, tzinfo=timezone.utc)
    interval = tiering.INTERVALS[Tier.HOT]
    low = now + interval * (1 - tiering.JITTER_FRACTION)
    high = now + interval * (1 + tiering.JITTER_FRACTION)
    for _ in range(50):
        assert low <= tiering.next_check_at(Tier.HOT, now) <= high


def test_is_due_true_when_never_checked():
    assert tiering.is_due(None, datetime.now(timezone.utc)) is True


def test_is_due_false_before_scheduled_time():
    now = datetime.now(timezone.utc)
    assert tiering.is_due(now + timedelta(hours=1), now) is False


def test_is_due_true_after_scheduled_time():
    now = datetime.now(timezone.utc)
    assert tiering.is_due(now - timedelta(hours=1), now) is True


def test_past_release_window_but_on_sale_is_recent_not_retired():
    old = TODAY - timedelta(days=400)
    assert tiering.compute_tier(old, TODAY, on_sale=True) == Tier.RECENT
    assert tiering.compute_tier(old, TODAY, on_sale=False) == Tier.RETIRED
