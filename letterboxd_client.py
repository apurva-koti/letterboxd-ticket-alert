"""Scrapes Letterboxd (no official API/RSS exists for watchlists or films)."""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from domain import Film, LetterboxdRelease

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
US_RELEASE_SECTIONS = ("Theatrical", "Theatrical limited")


def pick_anchor(dates: list[date], today: date | None = None) -> date | None:
    """Earliest still-upcoming date, else the most recent past one (so a
    legacy film anchors on its latest re-release, not its original)."""
    today = today or date.today()
    upcoming = [d for d in dates if d >= today]
    if upcoming:
        return min(upcoming)
    return max(dates) if dates else None


class LetterboxdClient:
    def _get(self, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("headers", HEADERS)
        kwargs.setdefault("timeout", 15)
        resp, last_exc = None, None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = requests.get(url, **kwargs)
                last_exc = None
                if resp.status_code < 500:
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
        while True:
            resp = self._get(f"{BASE_URL}/{username}/watchlist/page/{page}/")
            if resp.status_code == 404:
                break
            resp.raise_for_status()

            page_films = _parse_page(resp.text)
            if not page_films:
                break
            films.extend(page_films)

            page += 1
            if max_pages and page > max_pages:
                break
            time.sleep(delay)
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

            if resp.status_code == 403 and page > 1:
                resp = self._get(f"{base}/?page={page}")

            if resp.status_code in (403, 404):
                break
            resp.raise_for_status()

            page_films = _parse_page(resp.text)
            if not page_films:
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

        return LetterboxdRelease(director=director, genres=genres, poster_url=poster_url, us_dates=us_dates)


def _extract_json_field(pattern: re.Pattern, html: str) -> str | None:
    match = pattern.search(html)
    if not match:
        return None
    try:
        return json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        return match.group(1)


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
