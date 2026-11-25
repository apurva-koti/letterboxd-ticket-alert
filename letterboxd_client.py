"""Scrapes Letterboxd (no official API/RSS exists for watchlists or films)."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from domain import Film, LetterboxdRelease

logger = logging.getLogger("letterboxd-ticket-alert")

BASE_URL = "https://letterboxd.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    )
}

RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 1.5

NAME_YEAR_RE = re.compile(r"^(.*)\s\((\d{4})\)$")
DIRECTOR_RE = re.compile(r'"director":\[\{"@type":"Person","name":"((?:[^"\\]|\\.)*)"')
POSTER_RE = re.compile(r'"image":"((?:[^"\\]|\\.)*)"')
GENRE_LINK_RE = re.compile(r'href="/films/genre/([a-z-]+)/"')
DURATION_RE = re.compile(r'"duration":"((?:[^"\\]|\\.)*)"')
ISO_DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?")
US_RELEASE_SECTIONS = ("Theatrical", "Theatrical limited")
TEXT_SNIPPET_LENGTH = 1000
CLOUDFLARE_CHALLENGE_TITLE = "Just a moment..."


class LetterboxdPageError(Exception):
    """A list or watchlist page that isn't what it should be - a Cloudflare
    check, a 404, or a page with no films that isn't a real watchlist/list
    page. Raised instead of returning a short or empty list, which the
    watchlist sync would take as "these films were removed" and delete them
    (confirmed in production: Nov 13 and Nov 25, every film deleted and
    re-alerted)."""


def _is_cloudflare_challenge(resp: requests.Response) -> bool:
    """Cloudflare's bot check: `cf-mitigated: challenge` and a "Just a
    moment..." page (confirmed live on the Hype share link, as a 403). Not
    the text "challenge-platform" - normal Letterboxd pages contain it too."""
    if (getattr(resp, "headers", None) or {}).get("cf-mitigated") == "challenge":
        return True
    return f"<title>{CLOUDFLARE_CHALLENGE_TITLE}</title>" in (resp.text or "")[:3000]


def _page_title(resp: requests.Response) -> str:
    soup = BeautifulSoup(resp.text, "html.parser")
    return soup.title.get_text(strip=True) if soup.title else ""


def pick_anchor(dates: list[date], today: date | None = None) -> date | None:
    """Earliest still-upcoming date, else the most recent past one (so a
    legacy film anchors on its latest re-release, not its original)."""
    today = today or date.today()
    upcoming = [d for d in dates if d >= today]
    if upcoming:
        return min(upcoming)
    return max(dates) if dates else None


class LetterboxdClient:
    def __init__(self) -> None:
        self._logged_full_page = False

    def _log_list_fetch(
        self, what: str, films: list[Film], pages: int, stop: requests.Response, ok_statuses: set[int], title_has: str
    ) -> None:
        """One line per list fetch saying how it ended, so a watchlist that
        comes back short or empty can be told apart from a real one
        (confirmed in production: on Nov 13 and Nov 25 the watchlist came
        back with no films and no error, and every film on it was deleted).
        A suspicious ending also logs what Letterboxd actually sent."""
        soup = BeautifulSoup(stop.text, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else None
        normal_stop = stop.status_code in ok_statuses and (stop.status_code != 200 or title_has in (title or ""))
        summary = (
            f"Letterboxd {what}: {len(films)} film(s) across {pages} page(s); "
            f"stopped on status={stop.status_code} title={title!r} length={len(stop.text)}"
        )
        if films and normal_stop:
            logger.info(summary)
            return
        text = " ".join(soup.get_text(" ", strip=True).split())[:TEXT_SNIPPET_LENGTH]
        logger.warning(f"{summary} - looks wrong ({'no films at all' if not films else 'unexpected last page'}): text={text!r}")
        # Full page once per run - enough to see what it was.
        if not getattr(self, "_logged_full_page", False):
            self._logged_full_page = True
            logger.warning(f"Letterboxd {what}: full body of that last page: {stop.text}")

    def _fail(self, what: str, films: list[Film], pages: int, resp: requests.Response, reason: str) -> None:
        self._log_list_fetch(what, films, pages, resp, ok_statuses=set(), title_has="")
        raise LetterboxdPageError(f"Letterboxd {what}: {reason} (status={resp.status_code} title={_page_title(resp)!r})")

    def _get(self, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("headers", HEADERS)
        kwargs.setdefault("timeout", 15)
        resp, last_exc = None, None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = requests.get(url, **kwargs)
                last_exc = None
                if _is_cloudflare_challenge(resp):
                    logger.warning(
                        f"Letterboxd Cloudflare check (attempt {attempt + 1}/{RETRY_ATTEMPTS}) {url}: status={resp.status_code}"
                    )
                elif resp.status_code < 500:
                    return resp
            except requests.exceptions.RequestException as e:
                last_exc, resp = e, None
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(RETRY_BASE_DELAY_SECONDS * (2**attempt))
        if resp is None:
            raise last_exc
        return resp

    def fetch_watchlist(self, username: str, delay: float = 0.5, max_pages: int | None = None) -> list[Film]:
        films: list[Film] = []
        page = 1
        what = f"watchlist {username}"
        while True:
            resp = self._get(f"{BASE_URL}/{username}/watchlist/page/{page}/")
            if _is_cloudflare_challenge(resp):
                self._fail(what, films, page - 1, resp, "Cloudflare check")
            if resp.status_code == 404:
                if page == 1:
                    self._fail(what, films, 0, resp, "404 on page 1")
                break
            resp.raise_for_status()

            page_films = _parse_page(resp.text)
            if not page_films:
                # Past the last page Letterboxd serves the watchlist page
                # itself with no films; anything else is not a real end.
                if "Watchlist" not in _page_title(resp):
                    self._fail(what, films, page - 1, resp, "page with no films that isn't a watchlist page")
                break
            films.extend(page_films)

            page += 1
            if max_pages and page > max_pages:
                break
            time.sleep(delay)
        # Past the last page Letterboxd normally serves the same watchlist
        # page with no films (status 200, "<user>'s Watchlist • Letterboxd").
        self._log_list_fetch(f"watchlist {username}", films, page - 1, resp, {200}, title_has="Watchlist")
        return films

    def fetch_list(self, list_url: str, delay: float = 0.5, max_pages: int | None = None) -> list[Film]:
        """list_url may be a share-token URL: page 1 must be the bare URL
        (/page/1/ 403s there), and /page/N/ for N>1 also 403s on a share link
        specifically, falling back to a ?page=N query param."""
        films: list[Film] = []
        previous_slugs: set[str] | None = None
        page = 1
        base = list_url.rstrip("/")
        while True:
            url = f"{base}/" if page == 1 else f"{base}/page/{page}/"
            resp = self._get(url)

            if resp.status_code == 403 and page > 1 and not _is_cloudflare_challenge(resp):
                resp = self._get(f"{base}/?page={page}")

            what = f"list {base}"
            if _is_cloudflare_challenge(resp):
                self._fail(what, films, page - 1, resp, "Cloudflare check")
            if resp.status_code in (403, 404):
                if page == 1:
                    self._fail(what, films, 0, resp, f"{resp.status_code} on page 1")
                break  # a share link 403s past page 1 - a normal end
            resp.raise_for_status()

            page_films = _parse_page(resp.text)
            if not page_films:
                if page == 1 and "Letterboxd" not in _page_title(resp):
                    self._fail(what, films, 0, resp, "page with no films that isn't a Letterboxd list page")
                break

            page_slugs = {f.slug for f in page_films}
            if page_slugs == previous_slugs:
                break  # duplicate content - the query-param fallback didn't really paginate
            previous_slugs = page_slugs

            films.extend(page_films)
            page += 1
            if max_pages and page > max_pages:
                break
            time.sleep(delay)
        # A share link 403s past page 1, which is a normal end there.
        ok = {200, 403} if page > 1 else {200}
        self._log_list_fetch(f"list {base}", films, page - 1, resp, ok, title_has="Letterboxd")
        return films

    def fetch_release(self, slug: str) -> LetterboxdRelease:
        resp = self._get(f"{BASE_URL}/film/{slug}/")
        resp.raise_for_status()
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        director = _extract_json_field(DIRECTOR_RE, html)
        poster_url = _extract_json_field(POSTER_RE, html)
        genres = sorted(set(GENRE_LINK_RE.findall(html)))
        us_dates = _extract_us_dates(soup)
        runtime = _extract_runtime(html)

        return LetterboxdRelease(director=director, genres=genres, poster_url=poster_url, us_dates=us_dates, runtime=runtime)


def _extract_json_field(pattern: re.Pattern, html: str) -> str | None:
    match = pattern.search(html)
    if not match:
        return None
    try:
        return json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        return match.group(1)


def _extract_runtime(html: str) -> int | None:
    match = DURATION_RE.search(html)
    if not match:
        return None
    parsed = ISO_DURATION_RE.fullmatch(match.group(1))
    if not parsed or not any(parsed.groups()):
        return None
    hours, minutes = parsed.groups()
    return int(hours or 0) * 60 + int(minutes or 0)


def _extract_us_dates(soup: BeautifulSoup) -> list[date]:
    dates: list[date] = []
    for heading in soup.select("h3.release-table-title"):
        if heading.get_text(strip=True) not in US_RELEASE_SECTIONS:
            continue
        table = heading.find_next_sibling("div", class_="release-table")
        if not table:
            continue
        for item in table.select(".listitem"):
            date_el = item.select_one(".date")
            countries = [c.get_text(strip=True) for c in item.select(".release-country .name")]
            if date_el and "USA" in countries:
                try:
                    dates.append(datetime.strptime(date_el.get_text(strip=True), "%d %b %Y").date())
                except ValueError:
                    continue
    return dates


def _parse_page(html: str) -> list[Film]:
    soup = BeautifulSoup(html, "html.parser")
    films = []
    for poster in soup.select('[data-component-class="LazyPoster"]'):
        full_name = poster.get("data-item-full-display-name") or poster.get("data-item-name")
        slug = poster.get("data-item-slug")
        link = poster.get("data-item-link")
        if not full_name or not slug:
            continue

        match = NAME_YEAR_RE.match(full_name)
        title, year = (match.group(1), int(match.group(2))) if match else (full_name, None)
        films.append(Film(slug=slug, title=title, year=year, url=BASE_URL + link if link else None))
    return films
