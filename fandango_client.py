"""Fandango I/O: search, director/runtime lookups, live showtime checks.

No public API, so this scrapes fandango.com directly. The napi endpoints
403 without cookies from a prior page visit plus a matching Referer - no
login needed, just a same-session-looking request. Fandango's own
"release date" field is deliberately never used here - verified unreliable
(see matcher.py's docstring).
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date

import requests
from bs4 import BeautifulSoup

from checktrace import KNOWN_SHOWTIME_TYPES, CandidateTrace, DateRead, TheaterShowings, showing_time
from domain import FandangoListing, ShowtimeCheck, TicketStatus

logger = logging.getLogger("letterboxd-ticket-alert")

BASE_URL = "https://www.fandango.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    )
}

TITLE_YEAR_RE = re.compile(r"^(.*)\s\((\d{4})\)$")

# Akamai's bot-management can intermittently serve this block page, or a
# plain empty body, instead of a real response - confirmed transient.
BLOCK_PAGE_MARKER = "A Message To Our Fans"
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 1.5
SNIPPET_LENGTH = 200

# The normal check asks for the 5 nearest theaters showing a film on a
# date. Looking for specific (preferred) theaters needs every theater, or a
# farther one gets cut off behind 5 closer ones.
DEFAULT_THEATER_LIMIT = 5
FULL_THEATER_LIMIT = 100
MAX_SEARCH_DATES = 21
PAST_SHOWTIME_TYPE = "pastshowtime"

def _looks_blocked(resp: requests.Response) -> bool:
    return BLOCK_PAGE_MARKER in resp.text or not resp.text.strip()


def _snippet(resp: requests.Response | None) -> str:
    if resp is None:
        return "<no response>"
    body = resp.text.strip().replace("\n", " ")
    return f"status={resp.status_code} body={body[:SNIPPET_LENGTH]!r}"


def _extract_movie_json_ld(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
        except (TypeError, ValueError):
            continue
        if data.get("@type") == "Movie":
            return data
    return None


class FandangoClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._get(BASE_URL + "/")  # visit once to pick up cookies

    def _get(self, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", 15)
        resp, last_exc = None, None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = self.session.get(url, **kwargs)
                last_exc = None
            except requests.exceptions.RequestException as e:
                last_exc, resp = e, None
                logger.warning(f"Fandango request exception (attempt {attempt + 1}/{RETRY_ATTEMPTS}) {url}: {e!r}")
            if resp is not None and not _looks_blocked(resp):
                return resp
            if resp is not None:
                logger.warning(f"Fandango response looks blocked (attempt {attempt + 1}/{RETRY_ATTEMPTS}) {url}: {_snippet(resp)}")
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(RETRY_BASE_DELAY_SECONDS * (2**attempt))
        if resp is None:
            raise last_exc
        logger.warning(f"Fandango still blocked after {RETRY_ATTEMPTS} attempts, returning it anyway: {url}: {_snippet(resp)}")
        return resp

    def search(self, title: str) -> list[FandangoListing]:
        """Only the "Movies" results panel - the page also renders an unrelated
        "Coming Soon" promo carousel regardless of query."""
        resp = self._get(f"{BASE_URL}/search", params={"q": title})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        listings = []
        for panel in soup.select("li.search__panel"):
            link = panel.select_one("a.search__movie-title")
            if not link:
                continue
            href = link.get("href", "")
            id_match = re.search(r"-(\d+)/movie-overview", href)
            if not id_match:
                continue

            full_title = link.get_text(strip=True)
            year_match = TITLE_YEAR_RE.match(full_title)
            movie_title, year = (year_match.group(1), int(year_match.group(2))) if year_match else (full_title, None)

            listings.append(
                FandangoListing(
                    fandango_id=id_match.group(1),
                    slug=href.strip("/").split("/")[0],
                    title=movie_title,
                    year=year,
                    url=BASE_URL + href,
                )
            )
        return listings

    def fetch_details(self, slug: str) -> tuple[str | None, int | None, str | None]:
        """Director, runtime, and synopsis all live in the same
        movie-overview page's JSON-LD block - one request covers all three
        signals. A real JSON parse, not a regex: the block also has a
        same-named nested "description" field inside aggregateRating (what
        the Audience Score means), which a naive regex grabs by mistake
        instead of the actual synopsis."""
        resp = self._get(f"{BASE_URL}/{slug}/movie-overview")
        resp.raise_for_status()
        movie = _extract_movie_json_ld(resp.text)
        if not movie:
            return None, None, None
        directors = movie.get("director") or []
        director = directors[0].get("name") if directors else None
        return director, movie.get("duration"), movie.get("description")

    def nearby_theaters(self, zip_code: str, radius_miles: int) -> list[tuple[str, float]]:
        """(name, distance in miles) for every theater Fandango lists within
        the radius, nearest first - what the settings form offers to pick from."""
        resp = self._get(
            f"{BASE_URL}/napi/nearbyTheaters",
            params={"zipCode": zip_code, "radius": radius_miles, "limit": 500},
            headers={"Accept": "application/json", "Referer": f"{BASE_URL}/{zip_code}_movietimes"},
        )
        resp.raise_for_status()
        theaters = [(t["name"], float(t.get("distance") or 0)) for t in resp.json().get("theaters", [])]
        return sorted((t for t in theaters if t[1] <= radius_miles), key=lambda t: t[1])

    def check_showtimes(
        self, fandango_id: str, slug: str, zip_code: str, trace: CandidateTrace | None = None
    ) -> ShowtimeCheck | None:
        """Looks up which dates actually have showtimes via Fandango's own
        movieCalendar endpoint (what the real date-picker on the movie page
        itself uses), then checks just those dates for ticket-type detail -
        no more guessing a date window and risking a booking outside it
        (confirmed missed in production: a one-off limited engagement three
        weeks past a film's main release)."""
        referer = f"{BASE_URL}/{slug}/movie-overview"
        trace = trace or CandidateTrace(label=slug)
        calendar_dates = self._movie_calendar(slug, zip_code, trace=trace)
        trace.calendar = calendar_dates
        for check_date in calendar_dates:
            read = DateRead(day=check_date, limit=DEFAULT_THEATER_LIMIT)
            trace.dates.append(read)
            data = self._showtime_grouping(fandango_id, zip_code, check_date, referer, trace=trace)
            if not data:
                read.note = "no showtime data"
                continue

            on_sale, showtimes_only = _parse_theaters(data, read, trace)
            if not (on_sale or showtimes_only):
                read.note = "only finished showings - skipped"
            if on_sale or showtimes_only:
                status = TicketStatus.ON_SALE if on_sale else TicketStatus.SHOWTIMES_ANNOUNCED
                trace.used_date = check_date
                return ShowtimeCheck(
                    checked_date=check_date,
                    status=status,
                    on_sale_theaters=sorted(on_sale),
                    showtimes_only_theaters=sorted(showtimes_only),
                )
        if calendar_dates:
            logger.warning(
                f"check_showtimes for {slug} ({fandango_id}): calendar had {len(calendar_dates)} date(s) "
                f"({calendar_dates}) but none had an upcoming showing (empty, or only finished showings)"
            )
        return None

    def find_on_sale_at(
        self, fandango_id: str, slug: str, zip_code: str, theaters: frozenset[str], trace: CandidateTrace | None = None
    ) -> set[str]:
        """Which of `theaters` have tickets on sale on ANY calendar date, not
        just the first one check_showtimes looks at - e.g. a film whose first
        date is an Alamo-only preview, while Metreon already sells tickets for
        opening day. Stops at the first date where one is found."""
        referer = f"{BASE_URL}/{slug}/movie-overview"
        # A throwaway trace for the per-date detail: up to 21 dates x every
        # theater is too much to log every check - only the outcome is kept.
        scratch = CandidateTrace(label=slug)
        calendar_dates = self._movie_calendar(slug, zip_code, trace=scratch)
        searched = calendar_dates[:MAX_SEARCH_DATES]
        for i, check_date in enumerate(searched, 1):
            data = self._showtime_grouping(fandango_id, zip_code, check_date, referer, limit=FULL_THEATER_LIMIT, trace=scratch)
            if not data:
                continue
            on_sale, _ = _parse_theaters(data, DateRead(day=check_date, limit=FULL_THEATER_LIMIT), scratch)
            found = on_sale & theaters
            if found:
                if trace:
                    trace.preferred_search = f"found {', '.join(sorted(found))} on {check_date:%m-%d} (date {i} of {len(calendar_dates)})"
                    trace.anomalies.extend(scratch.anomalies)
                return found
        if trace:
            trace.preferred_search = f"none on sale across {len(searched)} of {len(calendar_dates)} date(s)"
            trace.anomalies.extend(scratch.anomalies)
        return set()

    def _movie_calendar(self, slug: str, zip_code: str, trace: CandidateTrace | None = None) -> list[date]:
        resp = self._get(
            f"{BASE_URL}/napi/movieCalendar/{slug}",
            params={"postalCode": zip_code, "zip": zip_code},
            headers={"Accept": "application/json", "Referer": f"{BASE_URL}/{slug}/movie-overview"},
        )
        if trace:
            trace.add_raw(f"movieCalendar {slug} zip={zip_code}", resp.status_code, resp.text)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            # Indistinguishable from "genuinely no dates" to every caller -
            # logged so a real failure here can finally be told apart from
            # that, instead of silently reading as "nothing on sale."
            logger.warning(f"movieCalendar for {slug} didn't parse as JSON: {_snippet(resp)}")
            return []
        if trace and data.get("error"):
            trace.calendar_error = str(data["error"])
        dates = (data.get("movieCalendar") or {}).get("showtimeDates") or []
        return [date.fromisoformat(d) for d in dates]

    def _showtime_grouping(
        self,
        fandango_id: str,
        zip_code: str,
        check_date: date,
        referer: str,
        limit: int = DEFAULT_THEATER_LIMIT,
        trace: CandidateTrace | None = None,
    ) -> dict | None:
        url = f"{BASE_URL}/napi/theaterShowtimeGroupings/{fandango_id}/{check_date.isoformat()}"
        resp = self._get(
            url,
            params={"zip": zip_code, "isdesktop": "true", "limit": limit},
            headers={"Accept": "application/json", "Referer": referer},
        )
        if trace:
            trace.add_raw(f"theaterShowtimeGroupings {fandango_id} {check_date} zip={zip_code} limit={limit}", resp.status_code, resp.text)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            logger.warning(f"theaterShowtimeGroupings for {fandango_id} on {check_date} didn't parse as JSON: {_snippet(resp)}")
            return None
        return data if data.get("hasShowtimes") else None


def _parse_theaters(data: dict, read: DateRead, trace: CandidateTrace) -> tuple[set[str], set[str]]:
    """(on_sale, showtimes_only) theater names from one theaterShowtimeGroupings
    response; every showing is also recorded on `read` for the log."""
    on_sale, showtimes_only = set(), set()
    for theater in data["theaterShowtimes"].get("theaters", []):
        is_on_sale, has_showtimes = False, False
        seen = TheaterShowings(name=theater["name"], distance=theater.get("distance"))
        read.theaters.append(seen)
        for variant in theater.get("variants", []):
            for group in variant.get("amenityGroups", []):
                for showtime in group.get("showtimes", []):
                    kind, at = showtime.get("type"), showing_time(showtime)
                    if showtime.get("isSoldOut"):
                        seen.sold_out.append(at)
                    elif kind == "available":
                        seen.open.append(at)
                    elif kind == PAST_SHOWTIME_TYPE:
                        seen.past.append(at)
                    else:
                        seen.other.append(f"{kind} {at}")
                    if kind not in KNOWN_SHOWTIME_TYPES and not showtime.get("isSoldOut"):
                        trace.anomalies.append(f"unknown showtime type {kind!r} at {theater['name']} {read.day}")
                    # A showing that already started says nothing about
                    # ticket sales. Counting it as "listed, not bookable"
                    # made a finished day (yesterday after midnight, or
                    # today after its last show) read as off sale, and the
                    # next check re-alerted - the duplicate emails.
                    if kind == PAST_SHOWTIME_TYPE:
                        continue
                    has_showtimes = True
                    if kind == "available" or showtime.get("isSoldOut"):
                        is_on_sale = True
        if is_on_sale:
            on_sale.add(theater["name"])
        elif has_showtimes:
            showtimes_only.add(theater["name"])
    return on_sale, showtimes_only
