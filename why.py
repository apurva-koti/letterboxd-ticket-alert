"""Diagnostic: explains why a film is (or isn't) being tracked/checked, and
what its current status is - without needing to poke at state.db by hand.

Usage: python3 why.py "<title or letterboxd slug>"
"""

import sys
from datetime import datetime, timezone
from difflib import SequenceMatcher

import letterboxd
import scheduler
import state
import ticket_checker
from config import HYPE_LIST_URL
from models import Film


def find_film(conn, query):
    """Exact slug match first, else best fuzzy title match against films
    currently in the DB (won't find something never synced at all)."""
    row = conn.execute("SELECT * FROM films WHERE slug = ?", (query,)).fetchone()
    if row:
        return dict(row)

    rows = conn.execute("SELECT * FROM films").fetchall()
    if not rows:
        return None

    def score(row):
        return SequenceMatcher(None, query.lower(), row["title"].lower()).ratio()

    best = max(rows, key=score)
    return dict(best) if score(best) >= 0.5 else None


def get_hype_slugs():
    if not HYPE_LIST_URL:
        return set()
    try:
        return {f.slug for f in letterboxd.get_list(HYPE_LIST_URL)}
    except Exception as e:
        print(f"(couldn't fetch the Hype list to check membership: {e})")
        return set()


def explain(conn, film_row, hype_slugs):
    slug = film_row["slug"]
    is_hype = slug in hype_slugs
    film = Film(slug=slug, title=film_row["title"], year=film_row["year"], url=film_row["url"])

    print(f"{film.title} ({film.year}) [{slug}]")
    print(f"  {film.url}")
    print(f"  On Hype list: {'yes' if is_hype else 'no'}")
    print()

    match = state.get_match(conn, slug)
    if not match:
        print("  Not yet attempted - will be matched on its first due check.")
        return

    if match.excluded_reason:
        if is_hype:
            print(
                f"  Was excluded as '{match.excluded_reason}', but is now on the Hype list, "
                f"which overrides that - will be retried."
            )
        else:
            print(f"  PERMANENTLY EXCLUDED: {match.excluded_reason}. Never retried unless added to Hype.")
        return

    if not match.fandango_id:
        checked_at = datetime.fromisoformat(match.checked_at)
        age = datetime.now(timezone.utc) - checked_at
        print(f"  UNMATCHED on Fandango. Last attempted {age.days}d {age.seconds // 3600}h ago.")
        if match.release_date:
            print(f"  Release date known via {match.release_date_source}: {match.release_date}")
        if is_hype:
            print("  On Hype list - retried on every check, not throttled to once a day.")
        else:
            retry_due = checked_at + ticket_checker.MATCH_RETRY_INTERVAL
            print(f"  Next match retry: {retry_due.isoformat()}")
        return

    print(f"  Matched: {match.matched_title} ({match.matched_year}) -> {match.fandango_slug}")
    if match.release_date:
        print(f"  Release date: {match.release_date} (source: {match.release_date_source})")
    else:
        print("  Release date: unknown from any source")

    status = state.get_ticket_status(conn, slug)
    if not status:
        print("  Matched, but not checked for ticket status yet.")
        return

    print(f"  Status: {status.status}")
    if status.on_sale_theaters:
        print(f"  On sale at: {', '.join(status.on_sale_theaters)}")
    if status.showtimes_only_theaters:
        print(f"  Showtimes listed (not yet purchasable) at: {', '.join(status.showtimes_only_theaters)}")
    print(f"  Tier: {status.tier}")
    if status.next_check_at:
        next_check = datetime.fromisoformat(status.next_check_at)
        delta = next_check - datetime.now(timezone.utc)
        when = "now (overdue)" if delta.total_seconds() <= 0 else f"in {delta}"
        print(f"  Next check: {when}")
    if status.notified_at:
        print(f"  Notified (emailed): {status.notified_at}")
    elif status.status == "on_sale":
        print("  On sale but not yet notified - will be sent (or retried) on the next run.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: python3 {sys.argv[0]} '<title or letterboxd slug>'")
        sys.exit(1)

    conn = state.connect()
    film_row = find_film(conn, sys.argv[1])
    if not film_row:
        print(f"No film matching {sys.argv[1]!r} found in the tracked watchlist/Hype set.")
        sys.exit(1)

    hype_slugs = get_hype_slugs()
    explain(conn, film_row, hype_slugs)
