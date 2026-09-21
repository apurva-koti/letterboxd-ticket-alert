"""Fallback for a US release date when Letterboxd doesn't have one listed yet.

Box Office Mojo (IMDb/Amazon-owned) tracks a "Domestic" release date per title.
Despite the heavy Amazon UI chrome, it's plain-scrapeable - confirmed by
inspection, no bot-detection encountered - as long as you use /title/tt<id>/,
not the JS-rendered /date/ calendar pages or a guessed /release/rl<id>/ URL.
"""

import re
import time
from datetime import datetime
from difflib import SequenceMatcher

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.boxofficemojo.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    )
}

TITLE_LINK_RE = re.compile(r"/title/(tt\d+)/")

# Same "don't guess" principle as the Fandango matcher.
MATCH_THRESHOLD = 0.85

# Mirrors letterboxd.py/fandango.py's own retry helpers - this project runs
# every 15 minutes, so a plain connection hiccup or a 5xx from Box Office
# Mojo's own server shouldn't fail an entire run on the first attempt.
RETRY_ATTEMPTS = 3  # 1 initial try + 2 retries
RETRY_BASE_DELAY_SECONDS = 1.5


def _get_with_retry(url, **kwargs):
    """GET with exponential backoff (1.5s, 3s) against a connection failure
    or a 5xx from Box Office Mojo's own server. Raises the underlying
    exception if every attempt failed to connect at all; otherwise always
    returns a Response, even a still-5xx one - the caller's own
    raise_for_status() is what surfaces that as a real error at that point."""
    kwargs.setdefault("headers", HEADERS)
    kwargs.setdefault("timeout", 15)
    resp = None
    last_exc = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = requests.get(url, **kwargs)
            last_exc = None
            if resp.status_code < 500:
                return resp
        except requests.exceptions.RequestException as e:
            last_exc = e
            resp = None
        if attempt < RETRY_ATTEMPTS - 1:
            time.sleep(RETRY_BASE_DELAY_SECONDS * (2**attempt))
    if resp is None:
        raise last_exc
    return resp


def _normalize(title):
    return re.sub(r"[^a-z0-9 ]", " ", title.lower().replace("&", " and ")).strip()


def search_title(title):
    resp = _get_with_retry(f"{BASE_URL}/search/", params={"q": title})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    candidates = []
    for link in soup.select('a.a-link-normal[href*="/title/tt"]'):
        match = TITLE_LINK_RE.search(link.get("href", ""))
        text = link.get_text(strip=True)
        if match and text:
            candidates.append({"imdb_id": match.group(1), "title": text})
    return candidates


def get_domestic_release_date(imdb_id):
    """Returns the "Domestic" (US) release date from a title's Box Office Mojo
    page, or None if it isn't listed (not yet released, or no US release)."""
    resp = _get_with_retry(f"{BASE_URL}/title/{imdb_id}/")
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    for link in soup.select("td a"):
        if link.get_text(strip=True) != "Domestic":
            continue
        cell = link.find_parent("td")
        date_cell = cell.find_next_sibling("td") if cell else None
        if date_cell:
            try:
                return datetime.strptime(date_cell.get_text(strip=True), "%b %d, %Y").date()
            except ValueError:
                return None
    return None


def find_release_date(title):
    """Searches Box Office Mojo for a title and returns its US release date, or
    None if there's no confidently-matching title or no domestic date listed."""
    candidates = search_title(title)
    if not candidates:
        return None

    target = _normalize(title)
    best = max(candidates, key=lambda c: SequenceMatcher(None, target, _normalize(c["title"])).ratio())
    score = SequenceMatcher(None, target, _normalize(best["title"])).ratio()
    if score < MATCH_THRESHOLD:
        return None

    return get_domestic_release_date(best["imdb_id"])


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print(f"Usage: python3 {sys.argv[0]} <movie title>")
        sys.exit(1)

    result = find_release_date(sys.argv[1])
    print(result if result else "No confident match / no domestic date listed.")
