"""Matches Letterboxd titles to Fandango movies and checks ticket on-sale status near a zip code.

No public Fandango API exists, so this scrapes fandango.com directly:
- /search?q=<title> to find a movie's Fandango id/slug
- /napi/theaterShowtimeGroupings/<id>/<date>?zip=<zip> to check real showtimes near a zip

The napi endpoint 403s ("Session expired or invalid token") unless called with cookies
from a prior fandango.com page visit plus a matching Referer header - it doesn't need a
logged-in account, just a same-session-looking request.
"""

import json
import re
import unicodedata
from datetime import date, timedelta
from difflib import SequenceMatcher

import requests

from models import FandangoCandidate, TicketStatusResult

BASE_URL = "https://www.fandango.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    )
}

TITLE_YEAR_RE = re.compile(r"^(.*)\s\((\d{4})\)$")
DIRECTOR_RE = re.compile(r'"director":\[\{"@type":"Person","name":"((?:[^"\\]|\\.)*)"')
RELEASE_DATE_RE = re.compile(r'"releaseDateQueryParam":"(\d{4}-\d{2}-\d{2})"')

# Below this fuzzy-match score, don't guess - surface for manual review instead.
MATCH_THRESHOLD = 0.85
# Candidates within this score of the top one are treated as "too close to call"
# on title alone and get disambiguated by director instead.
AMBIGUITY_GAP = 0.05
# Letterboxd and Fandango credit directors with different name forms (e.g. "Alejandro
# G. Iñárritu" vs "Alejandro González Iñárritu"), so director comparison is fuzzy too.
DIRECTOR_MATCH_THRESHOLD = 0.6


def new_session():
    """A requests.Session that's visited fandango.com so it holds valid cookies."""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get(BASE_URL + "/", timeout=15)
    return session


def search_movie(session, title):
    """Searches Fandango for a title, returning candidate movies from the results grid.

    Only parses the actual "Movies" results section (search__panel), not the unrelated
    "Coming Soon" promo carousel that Fandango renders on every search page regardless
    of query.
    """
    from bs4 import BeautifulSoup

    resp = session.get(f"{BASE_URL}/search", params={"q": title}, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    candidates = []
    for panel in soup.select("li.search__panel"):
        link = panel.select_one("a.search__movie-title")
        if not link:
            continue
        href = link.get("href", "")
        full_title = link.get_text(strip=True)
        match = TITLE_YEAR_RE.match(full_title)
        movie_title, year = (match.group(1), int(match.group(2))) if match else (full_title, None)

        id_match = re.search(r"-(\d+)/movie-overview", href)
        if not id_match:
            continue

        info_lines = [p.get_text(strip=True) for p in panel.select("p.search__movie-info")]

        candidates.append(
            FandangoCandidate(
                fandango_id=id_match.group(1),
                slug=href.strip("/").split("/")[0],
                title=movie_title,
                year=year,
                status=info_lines[0] if info_lines else None,
                url=BASE_URL + href,
            )
        )
    return candidates


def _normalize(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text)).strip()


def get_director(session, fandango_slug):
    """Fetches a movie's director from its Fandango overview page, for
    disambiguating title matches. Returns None (not raises) if there's no
    director credited or the page can't be parsed - disambiguation callers
    treat that as "can't use this signal", not fatal.
    """
    resp = session.get(f"{BASE_URL}/{fandango_slug}/movie-overview", timeout=15)
    resp.raise_for_status()
    match = DIRECTOR_RE.search(resp.text)
    if not match:
        return None
    try:
        return json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        return match.group(1)


def get_release_date(session, fandango_slug):
    """Fetches a movie's theatrical release date from its Fandango overview page.

    There's no Fandango field for when tickets go on sale (confirmed by inspection -
    only a release date is exposed), so this is a proxy signal for scheduling how
    often to check, not a direct answer. Returns a date, or None if the page doesn't
    have one (rare, but shouldn't be fatal to the caller).
    """
    resp = session.get(f"{BASE_URL}/{fandango_slug}/movie-overview", timeout=15)
    resp.raise_for_status()
    match = RELEASE_DATE_RE.search(resp.text)
    return date.fromisoformat(match.group(1)) if match else None


def match_movie(session, title, year=None, director=None):
    """Finds the best Fandango candidate for a Letterboxd title.

    `director`, when given, is only used to break ties between candidates whose
    title-similarity scores are within AMBIGUITY_GAP of each other (e.g. a movie
    and its anniversary re-release, or two same-titled films) - it costs an extra
    request per close candidate, so it's not fetched unless there's a tie to break.

    Returns (match, candidates). `match` is None when nothing clears MATCH_THRESHOLD,
    or candidates are still tied after the director check - caller should treat that
    as "needs manual review", not silently pick one.
    """
    candidates = search_movie(session, title)
    if not candidates:
        return None, []

    target = _normalize(title)
    scored = []
    for c in candidates:
        score = SequenceMatcher(None, target, _normalize(c.title)).ratio()
        if year and c.year:
            # Letterboxd's year can be a festival year, not the theatrical year, so
            # this nudges the score rather than gating on it.
            score += 0.05 if abs(c.year - year) <= 1 else 0
        scored.append((score, c))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    top_score, top = scored[0]

    if top_score < MATCH_THRESHOLD:
        return None, candidates

    tied = [c for s, c in scored if top_score - s <= AMBIGUITY_GAP]

    if len(tied) > 1 and director:
        target_director = _normalize(director)
        director_matches = []
        for c in tied:
            d = get_director(session, c.slug)
            if d and SequenceMatcher(None, target_director, _normalize(d)).ratio() >= DIRECTOR_MATCH_THRESHOLD:
                director_matches.append(c)
        if len(director_matches) == 1:
            return director_matches[0], candidates
        return None, candidates

    if len(tied) > 1:
        return None, candidates

    return top, candidates


# A showtime's `type` field distinguishes an actually-purchasable slot ("available")
# from one Fandango lists before sales open ("restricted", with a "Tickets coming
# soon" message). Collapsing those would lose a real, separately-useful signal.
STATUS_ON_SALE = "on_sale"
STATUS_SHOWTIMES_ANNOUNCED = "showtimes_announced"


def check_ticket_status(session, fandango_id, fandango_slug, zip_code, days_ahead=14):
    """Checks a rolling window of upcoming dates for showtimes near zip_code, and
    classifies what's found into two distinct states rather than one on/off signal:

    - STATUS_ON_SALE: at least one theater has a showtime that's actually purchasable
    - STATUS_SHOWTIMES_ANNOUNCED: showtimes are listed (a date/time exists) but every
      one of them is pre-sale ("restricted") - useful to track on its own since
      whether that's worth alerting on is a judgment call, not automatic.

    Fandango only has showtime data for the near term (tickets typically go on sale
    1-3 weeks before a screening), so scanning further out than that isn't useful.

    Returns None if no theater/showtime data at all in the window, else a TicketStatusResult.
    """
    referer = f"{BASE_URL}/{fandango_slug}/movie-overview"
    today = date.today()

    for offset in range(days_ahead):
        check_date = (today + timedelta(days=offset)).isoformat()
        resp = session.get(
            f"{BASE_URL}/napi/theaterShowtimeGroupings/{fandango_id}/{check_date}",
            params={"zip": zip_code, "isdesktop": "true", "limit": 5},
            headers={"Accept": "application/json", "Referer": referer},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        if not data.get("hasShowtimes"):
            continue

        on_sale_theaters = set()
        showtimes_only_theaters = set()
        for theater in data["theaterShowtimes"].get("theaters", []):
            theater_on_sale = False
            theater_has_showtimes = False
            for variant in theater.get("variants", []):
                for group in variant.get("amenityGroups", []):
                    for showtime in group.get("showtimes", []):
                        theater_has_showtimes = True
                        # A sold-out showtime still proves tickets went on sale at
                        # some point, so it counts as on-sale, not merely announced.
                        if showtime.get("type") == "available" or showtime.get("isSoldOut"):
                            theater_on_sale = True
            if theater_on_sale:
                on_sale_theaters.add(theater["name"])
            elif theater_has_showtimes:
                showtimes_only_theaters.add(theater["name"])

        if on_sale_theaters or showtimes_only_theaters:
            return TicketStatusResult(
                date=check_date,
                status=STATUS_ON_SALE if on_sale_theaters else STATUS_SHOWTIMES_ANNOUNCED,
                on_sale_theaters=sorted(on_sale_theaters),
                showtimes_only_theaters=sorted(showtimes_only_theaters),
            )

    return None


if __name__ == "__main__":
    import sys

    if len(sys.argv) not in (3, 4):
        print(f"Usage: python3 {sys.argv[0]} <movie title> <zip code> [director]")
        sys.exit(1)

    title, zip_code = sys.argv[1], sys.argv[2]
    director = sys.argv[3] if len(sys.argv) == 4 else None
    session = new_session()

    match, candidates = match_movie(session, title, director=director)
    if not match:
        print(f"No confident match for {title!r}. Candidates seen:")
        for c in candidates[:5]:
            print(f"  - {c.title} ({c.year}) [{c.status}] {c.url}")
        sys.exit(1)

    print(f"Matched: {match.title} ({match.year}) -> {match.url}")
    result = check_ticket_status(session, match.fandango_id, match.slug, zip_code)
    if not result:
        print(f"No showtimes found near {zip_code} in the next 14 days.")
    else:
        print(f"As of {result.date} (status: {result.status}):")
        if result.on_sale_theaters:
            print(f"  Tickets ON SALE at: {', '.join(result.on_sale_theaters)}")
        if result.showtimes_only_theaters:
            print(f"  Showtimes listed but NOT YET ON SALE at: {', '.join(result.showtimes_only_theaters)}")
