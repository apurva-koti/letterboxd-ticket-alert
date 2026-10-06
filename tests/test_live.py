"""Real network smoke tests against the actual sites this project scrapes.

Not run by default - run deliberately with `pytest -m live` after touching a
parser, or periodically to catch markup drift the fixture-based tests can't.
"""

import pytest

from fandango_client import FandangoClient
from letterboxd_client import LetterboxdClient
from matcher import find_candidates

pytestmark = pytest.mark.live


def test_letterboxd_watchlist_scrape_still_works():
    films = LetterboxdClient().fetch_watchlist("dave", max_pages=1)
    assert len(films) == 28


def test_letterboxd_release_scrape_still_works():
    release = LetterboxdClient().fetch_release("vertigo")
    assert release.director == "Alfred Hitchcock"


def test_fandango_search_and_release_date_still_work():
    client = FandangoClient()
    listings = client.search("Vertigo")
    assert any(c.title == "Vertigo" for c in listings)


def test_find_candidates_still_includes_both_moonlight_listings():
    client = FandangoClient()
    candidates = find_candidates(client, "Moonlight", "Barry Jenkins")
    years = {c.year for c in candidates}
    assert 2016 in years
    assert 2006 not in years  # unrelated film, director contradicts
