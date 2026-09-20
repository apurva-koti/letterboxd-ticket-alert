"""Single-shot scheduler: on each invocation, checks whichever films are due and
reschedules them. Meant to be invoked repeatedly by an external scheduler (cron,
launchd, Modal, etc. - deliberately not decided yet) every ~15-30 min; each run
only touches films whose next_check_at has passed, so invoking it more often
than that just no-ops for everything not yet due.

Every watchlist film is tracked, including old/legacy ones - Letterboxd lists
re-release dates for plenty of legacy films (verified on Top Gun, Sense and
Sensibility) alongside their original theatrical run, so a film being old is
not evidence there's nothing left to check. What changes for a film that's
well past its (currently known) release date is cadence, not whether it's
tracked at all: it drops into TIER_RETIRED, checked only once every ~30 days
(jittered - see JITTER_FRACTION) instead of daily. Each of those slow checks
re-fetches the film's release date (see ticket_checker.get_or_match), so a
newly-announced re-release is eventually noticed; once that happens,
compute_tier reclassifies the film into a faster tier on its own next check,
same as any other film - there's no separate mechanism needed for that, since
tier is recomputed fresh every check rather than being a stored label.

Tiering is based on proximity to a film's release date, since there's no
"tickets go on sale" field anywhere (Fandango, Letterboxd, and Box Office Mojo
were all checked). That date itself comes from whichever of those three sources
has it first - see ticket_checker.get_or_match. This is a proxy, not a
guarantee - a hyped tentpole can open sales months before release, which no
release-date-based tier catches early on its own.

The override for that: a Letterboxd list (e.g. "Hype") named by
config.HYPE_LIST_URL. Anything in that list is tracked regardless of the
watchlist, forced into TIER_MUST_WATCH (checked every single run, however
often that's invoked), and exempt from the documentary exclusion - an
explicit "I care about this" signal overrides that heuristic. Hype is meant
for urgent, upcoming pre-sales specifically, not a way to fast-track legacy
films - those are handled by the slow TIER_RETIRED refresh above instead. The
list can be private (a boxd.it share link); see README.
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

TIER_MUST_WATCH = "must_watch"  # in the Hype list - checked on every run, no matter what
TIER_HOT = "hot"  # unreleased, <=90 days out - checked most frequently
TIER_RECENT = "recent"  # released within the last 2 weeks - less frequently
TIER_FAR_FUTURE = "far_future"  # unreleased, >90 days out - still less frequently
TIER_UNKNOWN = "unknown"  # no release date found from any source yet
TIER_RETIRED = "retired"  # released (or last known to release) >2 weeks ago - checked slowly for a re-release

INTERVALS = {
    TIER_MUST_WATCH: timedelta(minutes=1),  # shorter than any realistic poll interval - always due
    TIER_HOT: timedelta(hours=3),
    TIER_RECENT: timedelta(hours=8),
    TIER_FAR_FUTURE: timedelta(hours=24),
    TIER_UNKNOWN: timedelta(hours=24),
    TIER_RETIRED: timedelta(days=30),
}

# Interval is jittered by up to this fraction so films in the same tier don't
# all become due at the same instant and burst-check together every cycle -
# for TIER_RETIRED specifically, this is what spreads legacy films' ~30-day
# release-date refreshes out across different days instead of all landing on
# the same cadence.
JITTER_FRACTION = 0.15

HOT_WINDOW_DAYS = 90
RECENT_WINDOW_DAYS = 14

# Politeness delay between Fandango/Letterboxd/Box Office Mojo requests within a run.
REQUEST_DELAY_SECONDS = 0.3


def compute_tier(release_date, today=None, is_hype=False):
    if is_hype:
        return TIER_MUST_WATCH

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


def run(username, zip_code, hype_list_url=None, db_path=state.DB_PATH):
    conn = state.connect(db_path)
    session = fandango.new_session()
    now = datetime.now(timezone.utc)

    watchlist_films = letterboxd.get_watchlist(username)

    hype_films = []
    if hype_list_url:
        try:
            hype_films = letterboxd.get_list(hype_list_url)
        except Exception:
            pass  # tracking still works off the watchlist alone this run

    hype_slugs = {f.slug for f in hype_films}

    # Tracked set is the union - a film only needs to be on *one* of the two
    # lists. Watchlist entries win on metadata if a film's on both (arbitrary
    # but harmless - same shape either way).
    by_slug = {f.slug: f for f in watchlist_films}
    for f in hype_films:
        by_slug.setdefault(f.slug, f)
    all_films = list(by_slug.values())

    added, removed = state.sync_watchlist(conn, all_films)

    checked = []
    alerts = []

    for row in conn.execute("SELECT * FROM films"):
        film = Film(slug=row["slug"], title=row["title"], year=row["year"], url=row["url"])
        is_hype = film.slug in hype_slugs

        status_row = state.get_ticket_status(conn, film.slug)
        if status_row and not is_due(status_row.next_check_at, now):
            continue  # not due - skip without even touching matching

        time.sleep(REQUEST_DELAY_SECONDS)

        match = ticket_checker.get_or_match(conn, session, film, is_hype=is_hype)
        if not match or not match.fandango_id:
            continue  # still unmatched (or not due for a match retry); nothing to check yet

        release_date = date.fromisoformat(match.release_date) if match.release_date else None
        tier = compute_tier(release_date, today=now.date(), is_hype=is_hype)
        next_at = next_check_time(tier, now).isoformat()

        outcome = ticket_checker.check_film(conn, session, film, zip_code, tier=tier, next_check_at=next_at)
        if not outcome:
            continue

        checked.append(outcome)
        if outcome.should_alert:
            alerts.append(outcome)

    return {"added": added, "removed": removed, "checked": checked, "alerts": alerts}


if __name__ == "__main__":
    import config

    if len(sys.argv) == 1:
        username, zip_code, hype_list_url = config.LETTERBOXD_USERNAME, config.ZIP_CODE, config.HYPE_LIST_URL
    elif len(sys.argv) in (3, 4):
        username, zip_code = sys.argv[1], sys.argv[2]
        hype_list_url = sys.argv[3] if len(sys.argv) == 4 else None
    else:
        print(f"Usage: python3 {sys.argv[0]} [<letterboxd_username> <zip_code> [hype_list_url]]")
        print("(with no args, reads from config.py / local_config.json)")
        sys.exit(1)

    if not username or not zip_code:
        print("No username/zip given, and none found in config.py / local_config.json.")
        sys.exit(1)

    result = run(username, zip_code, hype_list_url=hype_list_url)

    print(f"Checked {len(result['checked'])} due films this run.")
    if result["alerts"]:
        print(f"\nTICKETS ON SALE ({len(result['alerts'])}):")
        for o in result["alerts"]:
            theaters = ", ".join(o.result.on_sale_theaters)
            print(f"  {o.film.title} ({o.film.year}) - {theaters} - {o.film.url}")
    else:
        print("No new on-sale alerts this run.")
