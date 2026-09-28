"""Diagnostic: explains why a film is (or isn't) being tracked/checked.

Usage: python3 why.py "<title or letterboxd slug>"
"""

import sys
from datetime import datetime, timezone
from difflib import SequenceMatcher

from config import HYPE_LIST_URL
from domain import Film, TicketStatus
from letterboxd_client import LetterboxdClient
from repository import FilmRepository


def find_film(repo: FilmRepository, query: str) -> Film | None:
    films = repo.all_films()
    for f in films:
        if f.slug == query:
            return f
    if not films:
        return None
    best = max(films, key=lambda f: SequenceMatcher(None, query.lower(), f.title.lower()).ratio())
    score = SequenceMatcher(None, query.lower(), best.title.lower()).ratio()
    return best if score >= 0.5 else None


def get_hype_slugs() -> set[str]:
    if not HYPE_LIST_URL:
        return set()
    try:
        return {f.slug for f in LetterboxdClient().fetch_list(HYPE_LIST_URL)}
    except Exception as e:
        print(f"(couldn't fetch the Hype list to check membership: {e})")
        return set()


def explain(repo: FilmRepository, film: Film, hype_slugs: set[str]) -> None:
    is_hype = film.slug in hype_slugs
    tracked = repo.get(film.slug)

    print(f"{film.title} ({film.year}) [{film.slug}]")
    print(f"  {film.url}")
    print(f"  On Hype list: {'yes' if is_hype else 'no'}")
    print()

    if tracked.excluded_reason:
        if is_hype:
            print(f"  Was excluded as '{tracked.excluded_reason}', now on Hype - will be retried.")
        else:
            print(f"  PERMANENTLY EXCLUDED: {tracked.excluded_reason}. Never retried unless added to Hype.")
        return

    print(f"  Anchor date (Letterboxd): {tracked.anchor_date or 'none yet'}")

    if tracked.fandango:
        print(f"  Fandango: {tracked.fandango.title} ({tracked.fandango.year}) -> {tracked.fandango.slug}")
    else:
        print("  No Fandango listing bound.")

    print(f"  Status: {tracked.status}")
    if tracked.on_sale_theaters:
        print(f"  On sale at: {', '.join(tracked.on_sale_theaters)}")
    if tracked.showtimes_only_theaters:
        print(f"  Showtimes listed (not yet purchasable) at: {', '.join(tracked.showtimes_only_theaters)}")
    if tracked.tier:
        print(f"  Tier: {tracked.tier}")

    if tracked.next_check_at:
        delta = tracked.next_check_at - datetime.now(timezone.utc)
        when = "now (overdue)" if delta.total_seconds() <= 0 else f"in {delta}"
        print(f"  Next check: {when}")

    if tracked.notified_at:
        print(f"  Notified (emailed): {tracked.notified_at}")
    elif tracked.status == TicketStatus.ON_SALE:
        print("  On sale but not yet notified - will be sent (or retried) on the next run.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: python3 {sys.argv[0]} '<title or letterboxd slug>'")
        sys.exit(1)

    repo = FilmRepository.connect()
    film = find_film(repo, sys.argv[1])
    if not film:
        print(f"No film matching {sys.argv[1]!r} found in the tracked watchlist/Hype set.")
        sys.exit(1)

    explain(repo, film, get_hype_slugs())
