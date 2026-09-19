"""Fallback for a US release date when Letterboxd doesn't have one listed yet.

Box Office Mojo (IMDb/Amazon-owned) tracks a "Domestic" release date per title.
Despite the heavy Amazon UI chrome, it's plain-scrapeable - confirmed by
inspection, no bot-detection encountered - as long as you use /title/tt<id>/,
not the JS-rendered /date/ calendar pages or a guessed /release/rl<id>/ URL.
"""

import re
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


def _normalize(title):
    return re.sub(r"[^a-z0-9 ]", " ", title.lower().replace("&", " and ")).strip()


def search_title(title):
    resp = requests.get(f"{BASE_URL}/search/", params={"q": title}, headers=HEADERS, timeout=15)
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
    resp = requests.get(f"{BASE_URL}/title/{imdb_id}/", headers=HEADERS, timeout=15)
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
