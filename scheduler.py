"""Single-shot scheduler: on each invocation, checks whichever films are due and
reschedules them. Meant to be invoked repeatedly by an external scheduler (cron,
launchd, Modal, etc. - deliberately not decided yet) every ~15-30 min; each run
only touches films whose next_check_at has passed, so invoking it more often
than that just no-ops for everything not yet due.

Re-releases/revival screenings are explicitly out of scope for this project -
only a film's own original theatrical run is tracked. Two consequences of that:

1. A film whose Letterboxd year is more than TRACKING_CUTOFF_YEARS old is
   skipped entirely, before even attempting a Fandango match - any ticket
   activity for it at this point would be a revival screening, not its release.
   (Letterboxd's year can be a festival year rather than the theatrical one, so
   this is a coarse proxy, not a guarantee - accepted tradeoff for the films
   this excludes.)
2. A film that's more than RECENT_WINDOW_DAYS past its own release date drops
   into TIER_RETIRED (checked at most once every ~6 months) rather than being
   polled daily forever - any on-sale event that far out would itself be a
   revival, which is exactly what's being scoped out.

Tiering is based on proximity to a film's release date, since there's no
"tickets go on sale" field anywhere (Fandango, Letterboxd, and Box Office Mojo
were all checked). That date itself comes from whichever of those three sources
has it first - see ticket_checker.get_or_match. This is a proxy, not a
guarantee - a hyped tentpole can open sales months before release, which no
release-date-based tier catches early. A future `priority` override (manual
"must-watch" flag, or a corroborated on-sale-date hint from a web search) is
meant to force HOT-tier checking independent of this logic, but isn't built yet.
"""

import random
import sys
import time
from datetime import date, datetime, timedelta, timezone

import fandango
import letterboxd
import state
import ticket_checker
from models import Film

TIER_HOT = "hot"  # unreleased, <=90 days out - checked most frequently
TIER_RECENT = "recent"  # released within the last 2 weeks - less frequently
TIER_FAR_FUTURE = "far_future"  # unreleased, >90 days out - still less frequently
TIER_UNKNOWN = "unknown"  # no release date found from any source yet
TIER_RETIRED = "retired"  # released >2 weeks ago - out of scope (would be a revival)

INTERVALS = {
    TIER_HOT: timedelta(hours=3),
    TIER_RECENT: timedelta(hours=8),
    TIER_FAR_FUTURE: timedelta(hours=24),
    TIER_UNKNOWN: timedelta(hours=24),
    TIER_RETIRED: timedelta(days=180),
}

# Interval is jittered by up to this fraction so films in the same tier don't
# all become due at the same instant and burst-check together every cycle.
JITTER_FRACTION = 0.15

HOT_WINDOW_DAYS = 90
RECENT_WINDOW_DAYS = 14
TRACKING_CUTOFF_YEARS = 2

# Politeness delay between Fandango/Letterboxd/Box Office Mojo requests within a run.
REQUEST_DELAY_SECONDS = 0.3


def in_tracking_scope(film, today=None):
    """False for films whose Letterboxd year is old enough that any ticket
    activity now would be a revival screening, not the original release -
    out of scope by design. Films with no listed year are let through (can't
    judge them); they'll fall into TIER_UNKNOWN if no release date turns up."""
    today = today or date.today()
    if film.year is None:
        return True
    return (today.year - film.year) <= TRACKING_CUTOFF_YEARS


def compute_tier(release_date, today=None):
    today = today or date.today()
    if release_date is None:
        return TIER_UNKNOWN

    days_until = (release_date - today).days
    if days_until < -RECENT_WINDOW_DAYS:
        return TIER_RETIRED
    if -RECENT_WINDOW_DAYS <= days_until <= 0:
        return TIER_RECENT
    if 0 < days_until <= HOT_WINDOW_DAYS:
        return TIER_HOT
    return TIER_FAR_FUTURE


def next_check_time(tier, now):
    interval = INTERVALS[tier]
    jitter = interval * random.uniform(-JITTER_FRACTION, JITTER_FRACTION)
    return now + interval + jitter


def is_due(next_check_at, now):
    return next_check_at is None or datetime.fromisoformat(next_check_at) <= now


def run(username, zip_code, db_path=state.DB_PATH):
    conn = state.connect(db_path)
    session = fandango.new_session()
    now = datetime.now(timezone.utc)

    films = letterboxd.get_watchlist(username)
    added, removed = state.sync_watchlist(conn, films)

    checked = []
    alerts = []

    for row in conn.execute("SELECT * FROM films"):
        film = Film(slug=row["slug"], title=row["title"], year=row["year"], url=row["url"])

        if not in_tracking_scope(film, today=now.date()):
            continue  # too old to be its original release - would be a revival, out of scope

        status_row = state.get_ticket_status(conn, film.slug)
        if status_row and not is_due(status_row.next_check_at, now):
            continue  # not due - skip without even touching matching

        time.sleep(REQUEST_DELAY_SECONDS)

        match = ticket_checker.get_or_match(conn, session, film)
        if not match or not match.fandango_id:
            continue  # still unmatched (or not due for a match retry); nothing to check yet

        release_date = date.fromisoformat(match.release_date) if match.release_date else None
        tier = compute_tier(release_date, today=now.date())
        next_at = next_check_time(tier, now).isoformat()

        outcome = ticket_checker.check_film(conn, session, film, zip_code, tier=tier, next_check_at=next_at)
        if not outcome:
            continue

        checked.append(outcome)
        if outcome.should_alert:
            alerts.append(outcome)

    return {"added": added, "removed": removed, "checked": checked, "alerts": alerts}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: python3 {sys.argv[0]} <letterboxd_username> <zip_code>")
        sys.exit(1)

    username, zip_code = sys.argv[1], sys.argv[2]
    result = run(username, zip_code)

    print(f"Checked {len(result['checked'])} due films this run.")
    if result["alerts"]:
        print(f"\nTICKETS ON SALE ({len(result['alerts'])}):")
        for o in result["alerts"]:
            theaters = ", ".join(o.result.on_sale_theaters)
            print(f"  {o.film.title} ({o.film.year}) - {theaters} - {o.film.url}")
    else:
        print("No new on-sale alerts this run.")
