"""Scrapes a public Letterboxd watchlist (no official API/RSS exists for watchlists)."""

import json
import re
import sys
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from models import Film, LetterboxdInfo

BASE_URL = "https://letterboxd.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    )
}

NAME_YEAR_RE = re.compile(r"^(.*)\s\((\d{4})\)$")
DIRECTOR_RE = re.compile(r'"director":\[\{"@type":"Person","name":"((?:[^"\\]|\\.)*)"')
# The JSON-LD "image" field, not to be confused with the page's og:image/
# twitter:image meta tags (different syntax: content="...", not "image":"...")
# - this one's the actual portrait poster, not a social-share crop.
POSTER_RE = re.compile(r'"image":"((?:[^"\\]|\\.)*)"')


def _parse_page(html):
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


GENRE_LINK_RE = re.compile(r'href="/films/genre/([a-z-]+)/"')


def get_film_info(slug):
    """Single fetch of a film's Letterboxd page, returning everything this
    project needs from it together: director, US theatrical release date, and
    genre slugs. These used to be two separate fetches to the same page
    (get_director, get_us_release_date) at different points in the pipeline;
    consolidated so a film is only ever fetched once here, since the scheduler
    now also needs genre (to exclude documentaries) before it decides whether
    to even attempt a Fandango match at all.

    Returns a LetterboxdInfo.
    """
    resp = requests.get(f"{BASE_URL}/film/{slug}/", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    director = None
    director_match = DIRECTOR_RE.search(html)
    if director_match:
        try:
            director = json.loads(f'"{director_match.group(1)}"')
        except json.JSONDecodeError:
            director = director_match.group(1)

    poster_url = None
    poster_match = POSTER_RE.search(html)
    if poster_match:
        try:
            poster_url = json.loads(f'"{poster_match.group(1)}"')
        except json.JSONDecodeError:
            poster_url = poster_match.group(1)

    # A film can list multiple USA dates - e.g. a limited release ahead of a
    # wider one, or a later awards-qualifying expansion - across the
    # "Theatrical limited" and "Theatrical" sections (verified against real
    # cases: Paper Tiger has USA dates of 13 Nov, 20 Nov, and 12 Feb the
    # following year across those two sections). The earliest of those is what
    # matters for scheduling - the first point real public tickets could
    # plausibly exist - and it's also what matched Fandango's own release date
    # exactly in both cases checked. "Premiere" entries (festival/special
    # screenings, not public tickets) are excluded.
    dates = []
    for heading in soup.select("h3.release-table-title"):
        if heading.get_text(strip=True) not in ("Theatrical", "Theatrical limited"):
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
    release_date = min(dates) if dates else None

    genres = sorted(set(GENRE_LINK_RE.findall(html)))

    return LetterboxdInfo(director=director, release_date=release_date, genres=genres, poster_url=poster_url)


def get_director(slug):
    """Fetches a film's director from its Letterboxd page, for disambiguating
    Fandango matches when title similarity alone is ambiguous. Returns None if
    the page has no director credited rather than raising - disambiguation
    callers treat that as "can't use this signal", not fatal.
    """
    return get_film_info(slug).director


def get_us_release_date(slug):
    """Fetches a film's US theatrical release date from Letterboxd's own
    Releases tab. See get_film_info for the parsing details."""
    return get_film_info(slug).release_date


def get_watchlist(username, delay=0.5, max_pages=None):
    """Fetches a user's full public watchlist by paginating through the HTML grid.

    Raises requests.HTTPError if the profile doesn't exist or the watchlist is private.
    """
    films = []
    page = 1
    while True:
        url = f"{BASE_URL}/{username}/watchlist/page/{page}/"
        resp = requests.get(url, headers=HEADERS, timeout=15)
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
        time.sleep(delay)  # be polite, avoid rate limiting

    return films


def get_list(list_url, delay=0.5, max_pages=None):
    """Fetches all films in a Letterboxd list - same grid/LazyPoster markup as
    the watchlist, just a different URL family, so this reuses _parse_page.

    list_url can be a plain public list URL or a private list's share-token
    URL (https://letterboxd.com/<user>/list/<slug>/share/<token>/, from a
    boxd.it short link). Verified by hand against a real share-token URL:

    - Page 1 has to be the bare URL - appending /page/1/ 403s there even
      though it's identical content to the bare URL.
    - .../page/N/ for N>1 also 403s on a share-token URL specifically (a
      plain public list paginates fine this way, same as the watchlist).
    - .../?page=N as a query param returns 200 there instead of 403 - but
      with only one film in the real list this was tested against, its
      content was byte-identical to page 1, so it's NOT confirmed whether
      Letterboxd actually honors that param for a share-token view or just
      silently ignores it and re-serves page 1. Duplicate-page detection
      below guards against that ambiguity: if a "later page" comes back
      identical (by slug set) to the page before it, that's treated as the
      real end, not counted twice.

    Net effect: path-based pagination is tried first (proven for public
    lists), falling back to the query-param form on a 403; a 403/404 on
    either, or a duplicate page, ends pagination. A share-linked list still
    likely caps out somewhere - just less certain exactly where than the
    initial "one page only" finding suggested.
    """
    films = []
    previous_slugs = None
    page = 1
    base = list_url.rstrip("/")
    while True:
        # Page 1 has to be the bare URL, not .../page/1/ - confirmed by hand
        # that a share-token URL 403s on /page/1/ even though the identical
        # content is served at the bare URL.
        url = f"{base}/" if page == 1 else f"{base}/page/{page}/"
        resp = requests.get(url, headers=HEADERS, timeout=15)

        if resp.status_code == 403 and page > 1:
            url = f"{base}/?page={page}"  # untested fallback - see docstring
            resp = requests.get(url, headers=HEADERS, timeout=15)

        if resp.status_code in (403, 404):
            break
        resp.raise_for_status()

        page_films = _parse_page(resp.text)
        if not page_films:
            break

        page_slugs = {f.slug for f in page_films}
        if page_slugs == previous_slugs:
            break  # same content as the page before - not real pagination
        previous_slugs = page_slugs

        films.extend(page_films)
        page += 1
        if max_pages and page > max_pages:
            break
        time.sleep(delay)

    return films


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: python3 {sys.argv[0]} <letterboxd_username>")
        sys.exit(1)

    username = sys.argv[1]
    watchlist = get_watchlist(username)
    print(f"{len(watchlist)} films in {username}'s watchlist:\n")
    for film in watchlist:
        print(f"- {film.title} ({film.year}) [{film.slug}]")
