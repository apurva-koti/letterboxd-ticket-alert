"""Fandango I/O: search, director/release-date lookups, live showtime checks.

No public API, so this scrapes fandango.com directly. The napi showtime
endpoint 403s without cookies from a prior page visit plus a matching
Referer - no login needed, just a same-session-looking request.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date

import requests
from bs4 import BeautifulSoup

from domain import FandangoListing, ShowtimeCheck, TicketStatus

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

# Akamai's bot-management can intermittently serve this block page, or a
# plain empty body, instead of a real response - confirmed transient.
BLOCK_PAGE_MARKER = "A Message To Our Fans"
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 1.5

def _looks_blocked(resp: requests.Response) -> bool:
    return BLOCK_PAGE_MARKER in resp.text or not resp.text.strip()


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
            if resp is not None and not _looks_blocked(resp):
                return resp
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(RETRY_BASE_DELAY_SECONDS * (2**attempt))
        if resp is None:
            raise last_exc
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

    def fetch_director(self, slug: str) -> str | None:
        resp = self._get(f"{BASE_URL}/{slug}/movie-overview")
        resp.raise_for_status()
        match = DIRECTOR_RE.search(resp.text)
        if not match:
            return None
        try:
            return json.loads(f'"{match.group(1)}"')
        except json.JSONDecodeError:
            return match.group(1)

    def fetch_release_date(self, slug: str) -> date | None:
        resp = self._get(f"{BASE_URL}/{slug}/movie-overview")
        resp.raise_for_status()
        match = RELEASE_DATE_RE.search(resp.text)
        return date.fromisoformat(match.group(1)) if match else None

    def check_showtimes(self, fandango_id: str, slug: str, zip_code: str) -> ShowtimeCheck | None:
        """Looks up which dates actually have showtimes via Fandango's own
        movieCalendar endpoint (what the real date-picker on the movie page
        itself uses), then checks just those dates for ticket-type detail -
        no more guessing a date window and risking a booking outside it
        (confirmed missed in production: a one-off limited engagement three
        weeks past a film's main release)."""
        referer = f"{BASE_URL}/{slug}/movie-overview"
        for check_date in self._movie_calendar(slug, zip_code):
            data = self._showtime_grouping(fandango_id, zip_code, check_date, referer)
            if not data:
                continue

            on_sale, showtimes_only = set(), set()
            for theater in data["theaterShowtimes"].get("theaters", []):
                is_on_sale, has_showtimes = False, False
                for variant in theater.get("variants", []):
                    for group in variant.get("amenityGroups", []):
                        for showtime in group.get("showtimes", []):
                            has_showtimes = True
                            if showtime.get("type") == "available" or showtime.get("isSoldOut"):
                                is_on_sale = True
                if is_on_sale:
                    on_sale.add(theater["name"])
                elif has_showtimes:
                    showtimes_only.add(theater["name"])

            if on_sale or showtimes_only:
                status = TicketStatus.ON_SALE if on_sale else TicketStatus.SHOWTIMES_ANNOUNCED
                return ShowtimeCheck(
                    checked_date=check_date,
                    status=status,
                    on_sale_theaters=sorted(on_sale),
                    showtimes_only_theaters=sorted(showtimes_only),
                )
        return None

    def _movie_calendar(self, slug: str, zip_code: str) -> list[date]:
        resp = self._get(
            f"{BASE_URL}/napi/movieCalendar/{slug}",
            params={"postalCode": zip_code, "zip": zip_code},
            headers={"Accept": "application/json", "Referer": f"{BASE_URL}/{slug}/movie-overview"},
        )
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            return []
        dates = (data.get("movieCalendar") or {}).get("showtimeDates") or []
        return [date.fromisoformat(d) for d in dates]

    def _showtime_grouping(self, fandango_id: str, zip_code: str, check_date: date, referer: str) -> dict | None:
        url = f"{BASE_URL}/napi/theaterShowtimeGroupings/{fandango_id}/{check_date.isoformat()}"
        resp = self._get(
            url,
            params={"zip": zip_code, "isdesktop": "true", "limit": 5},
            headers={"Accept": "application/json", "Referer": referer},
        )
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            return None
        return data if data.get("hasShowtimes") else None
