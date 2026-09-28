from datetime import date

import matcher
from conftest import make_listing


class FakeClient:
    def __init__(self, listings, directors=None, release_dates=None):
        self.listings = listings
        self.directors = directors or {}
        self.release_dates = release_dates or {}

    def search(self, title):
        return self.listings

    def fetch_director(self, slug):
        return self.directors.get(slug)

    def fetch_release_date(self, slug):
        return self.release_dates.get(slug)


def test_find_anchored_match_picks_closest_release_date():
    """Moonlight-style: four Fandango listings share (or nearly share) the
    title "Moonlight" - the real 2016 film, an unrelated 2006 film, and two
    later re-release events. Anchoring on the 2016 release date must pick
    the 2016 listing even though a same-titled unrelated film also exists."""
    listings = [
        make_listing(fandango_id="1", slug="moonlight-2016", title="Moonlight", year=2016),
        make_listing(fandango_id="2", slug="moonlight-2006", title="Moonlight", year=2006),
        make_listing(fandango_id="3", slug="moonlight-anniversary", title="Moonlight 10th Anniversary Remastered", year=2026),
    ]
    dates = {"moonlight-2016": date(2016, 10, 21), "moonlight-2006": date(2006, 3, 1), "moonlight-anniversary": date(2026, 10, 2)}
    directors = {"moonlight-2016": "Barry Jenkins", "moonlight-2006": "Paula van der Oest"}
    client = FakeClient(listings, directors=directors, release_dates=dates)

    match = matcher.find_anchored_match(client, "Moonlight", date(2016, 10, 21), "Barry Jenkins")
    assert match.slug == "moonlight-2016"


def test_find_anchored_match_picks_the_re_release_once_anchor_moves():
    listings = [
        make_listing(fandango_id="1", slug="moonlight-2016", title="Moonlight", year=2016),
        make_listing(fandango_id="2", slug="moonlight-2006", title="Moonlight", year=2006),
        make_listing(fandango_id="3", slug="moonlight-anniversary", title="Moonlight 10th Anniversary Remastered", year=2026),
    ]
    dates = {"moonlight-2016": date(2016, 10, 21), "moonlight-2006": date(2006, 3, 1), "moonlight-anniversary": date(2026, 10, 2)}
    directors = {"moonlight-anniversary": "Barry Jenkins"}
    client = FakeClient(listings, directors=directors, release_dates=dates)

    match = matcher.find_anchored_match(client, "Moonlight", date(2026, 10, 2), "Barry Jenkins")
    assert match.slug == "moonlight-anniversary"


def test_find_anchored_match_declines_when_director_contradicts():
    """Regression test for a real mismatch: a lone, date-plausible candidate
    whose director doesn't match should be declined, not guessed."""
    listings = [make_listing(fandango_id="1", slug="center-stage-2000", title="Center Stage", year=2000)]
    client = FakeClient(listings, directors={"center-stage-2000": "Nicholas Hytner"}, release_dates={"center-stage-2000": date(2000, 5, 12)})

    match = matcher.find_anchored_match(client, "Center Stage", date(2000, 5, 20), "Robert Sidney")
    assert match is None


def test_find_anchored_match_rejects_candidate_too_far_from_anchor():
    listings = [make_listing(fandango_id="1", slug="unrelated", title="Moonlight", year=2006)]
    client = FakeClient(listings, release_dates={"unrelated": date(2006, 3, 1)})

    match = matcher.find_anchored_match(client, "Moonlight", date(2026, 10, 2), None)
    assert match is None


def test_find_anchored_match_no_candidates():
    client = FakeClient([])
    assert matcher.find_anchored_match(client, "Nothing Here", date(2026, 1, 1), None) is None


def test_find_match_by_title_resolves_tie_via_director():
    listings = [
        make_listing(fandango_id="1", slug="insomnia-1", title="Insomnia", year=2002),
        make_listing(fandango_id="2", slug="insomnia-2", title="Insomnia", year=1997),
    ]
    directors = {"insomnia-1": "Christopher Nolan", "insomnia-2": "Erik Skjoldbjaerg"}
    client = FakeClient(listings, directors=directors)

    match = matcher.find_match_by_title(client, "Insomnia", "Christopher Nolan")
    assert match.slug == "insomnia-1"

    match = matcher.find_match_by_title(client, "Insomnia", "Someone Else")
    assert match is None


def test_find_match_by_title_no_director_declines_tie():
    listings = [
        make_listing(fandango_id="1", slug="insomnia-1", title="Insomnia", year=2002),
        make_listing(fandango_id="2", slug="insomnia-2", title="Insomnia", year=1997),
    ]
    client = FakeClient(listings)
    assert matcher.find_match_by_title(client, "Insomnia", None) is None


def test_find_match_by_title_below_threshold_returns_none():
    listings = [make_listing(fandango_id="1", slug="something-else", title="A Completely Different Film", year=2020)]
    client = FakeClient(listings)
    assert matcher.find_match_by_title(client, "Moonlight", None) is None
