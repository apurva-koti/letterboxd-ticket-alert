"""Real network smoke tests against the actual sites this project scrapes.

Not run by default (pytest.ini excludes the `live` marker) since these are
slow and depend on external sites being up and unchanged - run deliberately
with `pytest -m live` after a change to any parser, or periodically to catch
the sites' markup drifting out from under the fixture-based unit tests.
"""

from datetime import date

import pytest

import boxofficemojo
import fandango
import letterboxd

pytestmark = pytest.mark.live


def test_letterboxd_watchlist_scrape_still_works():
    films = letterboxd.get_watchlist("dave", max_pages=1)
    assert len(films) == 28


def test_letterboxd_director_scrape_still_works():
    assert letterboxd.get_director("vertigo") == "Alfred Hitchcock"


def test_fandango_match_and_release_date_still_work():
    session = fandango.new_session()
    match, _ = fandango.match_movie(session, "Vertigo", year=1958)
    assert match is not None
    assert match.title == "Vertigo"

    release_date = fandango.get_release_date(session, match.slug)
    assert release_date is None or isinstance(release_date, date)


def test_boxofficemojo_domestic_date_still_works():
    result = boxofficemojo.find_release_date("Coyote vs. Acme")
    assert result == date(2026, 8, 28)
