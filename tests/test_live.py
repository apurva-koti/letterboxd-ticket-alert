"""Real network smoke tests against the actual sites this project scrapes.

Not run by default - run deliberately with `pytest -m live` after touching a
parser, or periodically to catch markup drift the fixture-based tests can't.
"""

from datetime import date

import pytest

from fandango_client import FandangoClient
from letterboxd_client import LetterboxdClient
from matcher import find_anchored_match

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


def test_anchored_match_still_resolves_moonlight_to_the_right_listing():
    client = FandangoClient()
    match = find_anchored_match(client, "Moonlight", date(2016, 10, 21), "Barry Jenkins")
    assert match is not None
    assert match.year == 2016
