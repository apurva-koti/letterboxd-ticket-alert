"""Syncs a Letterboxd watchlist, matches films to Fandango, and checks ticket status.

Alerts (currently just printed - SMS comes later) fire only on the transition into
on_sale from anything else, so a movie that stays on_sale across runs, or sits at
showtimes_announced indefinitely, doesn't get texted about repeatedly.
"""

import sys
from datetime import datetime, timedelta, timezone

import boxofficemojo
import fandango
import letterboxd
import state
from models import CheckOutcome

# How long to wait before retrying a failed match. Without this, an unmatched
# film (Fandango just doesn't have it cataloged) would get re-searched on every
# single run forever - wasteful, and pointless since catalog entries don't
# appear that fast. Matched films never hit this; they're cached indefinitely.
MATCH_RETRY_INTERVAL = timedelta(days=1)


DOCUMENTARY_GENRE = "documentary"


def get_or_match(conn, session, film, is_hype=False):
    """Looks up a film's Fandango match from the DB, or searches for one if it's
    never been attempted or the last attempt (more than MATCH_RETRY_INTERVAL ago)
    found nothing (catalog entries can appear later, e.g. as a release date
    approaches). A film already marked excluded_reason (e.g. "documentary") is
    never retried - that's a permanent scope decision, not a temporary miss -
    UNLESS is_hype is now True, which overrides a past exclusion and skips a
    fresh one: being on the Hype list is a stronger, explicit signal than the
    coarse documentary heuristic. is_hype also bypasses MATCH_RETRY_INTERVAL
    entirely, re-attempting a still-unmatched film on every call - the whole
    point of Hype is catching a Fandango listing (or release date) the moment
    it appears, not waiting up to a day to look again.

    Before attempting a Fandango match, this fetches the film's Letterboxd page
    once (get_film_info) for three things at once: its genres (to exclude
    documentaries from tracking entirely, skipping the Fandango attempt too),
    its director (to help Fandango disambiguation), and its US release date as
    a fallback if Fandango doesn't have one. Fandango's own release date still
    wins when present - it's what check_ticket_status itself polls, so
    scheduling off Fandango's own notion of "release" stays self-consistent -
    Letterboxd (then Box Office Mojo) only fill in when Fandango has nothing.
    """
    existing = state.get_match(conn, film.slug)
    if existing:
        if existing.excluded_reason and not is_hype:
            return existing  # permanently out of scope, never retried
        if existing.fandango_id:
            return existing
        last_checked = datetime.fromisoformat(existing.checked_at)
        if not is_hype and datetime.now(timezone.utc) - last_checked < MATCH_RETRY_INTERVAL:
            return existing  # still unmatched, not due for a retry yet

    letterboxd_info = None
    try:
        letterboxd_info = letterboxd.get_film_info(film.slug)
    except Exception:
        pass  # proceed without it - matching still works, just with less to go on

    if letterboxd_info and DOCUMENTARY_GENRE in letterboxd_info.genres and not is_hype:
        state.save_match(conn, film.slug, None, None, None, None, excluded_reason=DOCUMENTARY_GENRE)
        return state.get_match(conn, film.slug)

    director = letterboxd_info.director if letterboxd_info else None
    match, _candidates = fandango.match_movie(session, film.title, year=film.year, director=director)

    release_date, release_date_source = None, None
    if match:
        try:
            release_date = fandango.get_release_date(session, match.slug)
            if release_date:
                release_date_source = "fandango"
        except Exception:
            pass

    if not release_date and letterboxd_info and letterboxd_info.release_date:
        release_date = letterboxd_info.release_date
        release_date_source = "letterboxd"

    if not release_date:
        try:
            release_date = boxofficemojo.find_release_date(film.title)
            if release_date:
                release_date_source = "boxofficemojo"
        except Exception:
            pass  # still unknown - scheduling falls back to the safe default tier

    poster_url = letterboxd_info.poster_url if letterboxd_info else None

    if match:
        state.save_match(
            conn,
            film.slug,
            match.fandango_id,
            match.slug,
            match.title,
            match.year,
            release_date,
            release_date_source,
            poster_url=poster_url,
        )
    else:
        state.save_match(
            conn, film.slug, None, None, None, None, release_date, release_date_source, poster_url=poster_url
        )

    return state.get_match(conn, film.slug)


def check_film(conn, session, film, zip_code, tier=None, next_check_at=None):
    """Returns a CheckOutcome describing this run's outcome for one film, or
    None if it has no usable Fandango match. `tier`/`next_check_at` are passed
    through to storage as-is (None for direct/manual checks outside the
    scheduler)."""
    match = get_or_match(conn, session, film)
    if not match or not match.fandango_id:
        return None

    result = fandango.check_ticket_status(session, match.fandango_id, match.fandango_slug, zip_code)
    new_status = result.status if result else "none"

    previous = state.get_ticket_status(conn, film.slug)
    previous_status = previous.status if previous else "none"
    should_alert = new_status == fandango.STATUS_ON_SALE and previous_status != fandango.STATUS_ON_SALE

    state.save_ticket_status(
        conn, film.slug, new_status, result, alerted=should_alert, tier=tier, next_check_at=next_check_at
    )

    return CheckOutcome(
        film=film,
        match=match,
        result=result,
        previous_status=previous_status,
        new_status=new_status,
        should_alert=should_alert,
        tier=tier,
    )


def run(username, zip_code, db_path=state.DB_PATH):
    conn = state.connect(db_path)
    session = fandango.new_session()

    films = letterboxd.get_watchlist(username)
    added, removed = state.sync_watchlist(conn, films)

    outcomes = []
    for film in films:
        outcome = check_film(conn, session, film, zip_code)
        if outcome:
            outcomes.append(outcome)

    return {"added": added, "removed": removed, "outcomes": outcomes}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: python3 {sys.argv[0]} <letterboxd_username> <zip_code>")
        sys.exit(1)

    username, zip_code = sys.argv[1], sys.argv[2]
    run_result = run(username, zip_code)

    alerts = [o for o in run_result["outcomes"] if o.should_alert]
    announced = [o for o in run_result["outcomes"] if o.new_status == fandango.STATUS_SHOWTIMES_ANNOUNCED]

    print(f"Checked {len(run_result['outcomes'])} matched films.")

    if alerts:
        print(f"\nTICKETS ON SALE ({len(alerts)}):")
        for o in alerts:
            theaters = ", ".join(o.result.on_sale_theaters)
            print(f"  {o.film.title} ({o.film.year}) - {theaters} - {o.film.url}")

    if announced:
        print(f"\nShowtimes announced, not yet on sale ({len(announced)}):")
        for o in announced:
            print(f"  {o.film.title} ({o.film.year})")

    if not alerts and not announced:
        print("No on-sale or announced tickets this run.")
