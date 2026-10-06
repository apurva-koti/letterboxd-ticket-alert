import matcher
from conftest import make_listing


class FakeClient:
    def __init__(self, listings, directors=None):
        self.listings = listings
        self.directors = directors or {}

    def search(self, title):
        return self.listings

    def fetch_director(self, slug):
        return self.directors.get(slug)


def test_find_candidates_keeps_every_director_confirmed_listing():
    """Moonlight-style: a real 2016 film, an unrelated same-titled 2006 film,
    and a 10th-anniversary re-release under a totally different title all
    show up in search - director confirmation (not date, not one "best"
    pick) is what separates the real film's listings from the unrelated one."""
    listings = [
        make_listing(fandango_id="1", slug="moonlight-2016", title="Moonlight", year=2016),
        make_listing(fandango_id="2", slug="moonlight-2006", title="Moonlight", year=2006),
        make_listing(fandango_id="3", slug="moonlight-anniversary", title="Moonlight 10th Anniversary Remastered", year=2026),
    ]
    directors = {
        "moonlight-2016": "Barry Jenkins",
        "moonlight-2006": "Paula van der Oest",
        "moonlight-anniversary": "Barry Jenkins",
    }
    client = FakeClient(listings, directors=directors)

    candidates = matcher.find_candidates(client, "Moonlight", "Barry Jenkins")
    slugs = {c.slug for c in candidates}
    assert slugs == {"moonlight-2016", "moonlight-anniversary"}


def test_find_candidates_keeps_a_reused_listing_with_no_director_contradiction():
    """American Psycho-style: a re-release can reuse the original listing
    instead of getting a new one - that listing must survive even though
    nothing about it (like a release date) signals "this is current"."""
    listings = [make_listing(fandango_id="1", slug="american-psycho-2000-3", title="American Psycho", year=2000)]
    client = FakeClient(listings, directors={"american-psycho-2000-3": "Mary Harron"})

    candidates = matcher.find_candidates(client, "American Psycho", "Mary Harron")
    assert [c.slug for c in candidates] == ["american-psycho-2000-3"]


def test_find_candidates_declines_when_director_contradicts():
    listings = [make_listing(fandango_id="1", slug="center-stage-2000", title="Center Stage", year=2000)]
    client = FakeClient(listings, directors={"center-stage-2000": "Nicholas Hytner"})

    candidates = matcher.find_candidates(client, "Center Stage", "Robert Sidney")
    assert candidates == []


def test_find_candidates_keeps_a_missing_director_as_a_data_gap():
    """A listing with no director info at all is a data gap, not a
    contradiction - it survives rather than getting dropped."""
    listings = [make_listing(fandango_id="1", slug="some-film", title="Some Film", year=2020)]
    client = FakeClient(listings)

    candidates = matcher.find_candidates(client, "Some Film", "Someone")
    assert [c.slug for c in candidates] == ["some-film"]


def test_find_candidates_no_director_uses_a_strict_title_floor():
    listings = [
        make_listing(fandango_id="1", slug="insomnia-1", title="Insomnia", year=2002),
        make_listing(fandango_id="2", slug="unrelated", title="A Completely Different Film", year=2020),
    ]
    client = FakeClient(listings)

    candidates = matcher.find_candidates(client, "Insomnia", None)
    assert [c.slug for c in candidates] == ["insomnia-1"]


def test_find_candidates_no_matches():
    client = FakeClient([])
    assert matcher.find_candidates(client, "Nothing Here", None) == []
