import matcher
from conftest import make_listing


class FakeClient:
    def __init__(self, listings, directors=None, runtimes=None, synopses=None):
        self.listings = listings
        self.directors = directors or {}
        self.runtimes = runtimes or {}
        self.synopses = synopses or {}

    def search(self, title):
        return self.listings

    def fetch_details(self, slug):
        return self.directors.get(slug), self.runtimes.get(slug), self.synopses.get(slug)


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


def test_find_candidates_rejects_a_shared_first_name_with_different_surname():
    """Regression test for a real false match: "Casino" (dir. Martin
    Scorsese) matched to "Casino Royale" (dir. Martin Campbell) - a shared
    first name alone inflated plain string similarity past the threshold."""
    listings = [make_listing(fandango_id="1", slug="casino-royale-2006", title="Casino Royale", year=2006)]
    client = FakeClient(listings, directors={"casino-royale-2006": "Martin Campbell"})

    candidates = matcher.find_candidates(client, "Casino", "Martin Scorsese")
    assert candidates == []


def test_find_candidates_rejects_a_shared_surname_with_different_first_name():
    """Same family of bug, the other direction: a shared surname alone
    (two real, distinct directors named Anderson) shouldn't be enough
    either."""
    listings = [make_listing(fandango_id="1", slug="phantom-thread-2017", title="Phantom Thread", year=2017)]
    client = FakeClient(listings, directors={"phantom-thread-2017": "Paul Thomas Anderson"})

    candidates = matcher.find_candidates(client, "Phantom Thread", "Wes Anderson")
    assert candidates == []


def test_find_candidates_director_contradiction_is_not_rescued_by_a_matching_runtime():
    """A contradiction at the director level is a hard stop - a weaker
    signal below it (runtime) never gets a chance to overrule it, even if
    that signal would have agreed. Otherwise "Casino Royale" could match
    "Casino" again just by sharing a similar runtime."""
    listings = [make_listing(fandango_id="1", slug="casino-royale-2006", title="Casino Royale", year=2006)]
    client = FakeClient(listings, directors={"casino-royale-2006": "Martin Campbell"}, runtimes={"casino-royale-2006": 178})

    candidates = matcher.find_candidates(client, "Casino", "Martin Scorsese", runtime=178)
    assert candidates == []


def test_find_candidates_falls_through_to_runtime_when_director_is_missing():
    """Sense and Sensibility-style: a re-release listing has no director
    data on Fandango's side at all - falls through to runtime, which
    matches the real film's, so it survives."""
    listings = [make_listing(fandango_id="1", slug="sense-and-sensibility-anniversary", title="Sense and Sensibility", year=2025)]
    client = FakeClient(listings, runtimes={"sense-and-sensibility-anniversary": 136})

    candidates = matcher.find_candidates(client, "Sense and Sensibility", "Ang Lee", runtime=136)
    assert [c.slug for c in candidates] == ["sense-and-sensibility-anniversary"]


def test_find_candidates_rejects_a_clearly_shorter_runtime_when_director_is_missing():
    """Remake-collision regression: a same-titled listing with no director
    data must not survive on title alone if its runtime is clearly shorter
    than the tracked film's - this is exactly what would let an actual
    remake get matched to the original's re-release listings instead."""
    listings = [make_listing(fandango_id="1", slug="short-cut", title="Sense and Sensibility", year=2025)]
    client = FakeClient(listings, runtimes={"short-cut": 95})

    candidates = matcher.find_candidates(client, "Sense and Sensibility", "Someone Else Entirely", runtime=136)
    assert candidates == []


def test_find_candidates_never_rejects_a_longer_runtime_when_director_is_missing():
    """Moonlight-style: a re-release event's listed runtime can run well
    past the film's own runtime because it bundles in an intro or
    retrospective featurette - a re-release adds to the runtime, it never
    cuts the film, so a longer candidate is never treated as suspicious."""
    listings = [make_listing(fandango_id="1", slug="moonlight-anniversary", title="Moonlight", year=2026)]
    client = FakeClient(listings, runtimes={"moonlight-anniversary": 129})

    candidates = matcher.find_candidates(client, "Moonlight", "Barry Jenkins", runtime=112)
    assert [c.slug for c in candidates] == ["moonlight-anniversary"]


def test_find_candidates_falls_through_to_synopsis_when_director_is_missing():
    """Event listings with no director metadata often describe the event
    in prose instead - confirmed in production: Moonlight's real
    anniversary listing's synopsis names its director even though the
    structured director field is empty."""
    listings = [make_listing(fandango_id="1", slug="moonlight-anniversary", title="Moonlight", year=2026)]
    synopsis = "This landmark Best Picture winner returns with an exclusive conversation with director Barry Jenkins."
    client = FakeClient(listings, synopses={"moonlight-anniversary": synopsis})

    candidates = matcher.find_candidates(client, "Moonlight", "Barry Jenkins")
    assert [c.slug for c in candidates] == ["moonlight-anniversary"]


def test_find_candidates_falls_through_to_strict_title_when_no_other_signal_available():
    """Neither director nor runtime data exists anywhere to check - title
    similarity is the only signal left, so it has to clear a much higher
    bar to be trusted on its own."""
    listings = [make_listing(fandango_id="1", slug="some-film", title="Some Film", year=2020)]
    client = FakeClient(listings)

    candidates = matcher.find_candidates(client, "Some Film", "Someone", runtime=100)
    assert [c.slug for c in candidates] == ["some-film"]


def test_find_candidates_drops_a_weak_title_match_with_no_other_signal():
    listings = [make_listing(fandango_id="1", slug="loosely-similar", title="Some Film Part Two", year=2020)]
    client = FakeClient(listings)

    candidates = matcher.find_candidates(client, "Some Film", "Someone", runtime=100)
    assert candidates == []


def test_find_candidates_rejects_a_candidate_year_earlier_than_the_target():
    """Sense and Sensibility-style, the other direction: the WATCHLIST film
    is a brand-new 2026 remake, and a same-titled listing for the original
    1995 film's re-release ("30th Anniversary (2025)") must not pollute its
    candidates just because the title matches - 2025 predates the remake's
    own 2026 production year, so it's rejected before director/runtime
    even get a say."""
    listings = [make_listing(fandango_id="1", slug="sense-and-sensibility-30th-anniversary", title="Sense and Sensibility", year=2025)]
    client = FakeClient(listings, runtimes={"sense-and-sensibility-30th-anniversary": 136})

    candidates = matcher.find_candidates(client, "Sense and Sensibility", "Georgia Oakley", runtime=131, year=2026)
    assert candidates == []


def test_find_candidates_keeps_a_candidate_year_equal_or_later_than_the_target():
    """American Psycho-style: the reused listing still displays its
    original 2000 production year even for a 2026 re-release - equal to
    the tracked film's own year, so it's never rejected by this filter."""
    listings = [make_listing(fandango_id="1", slug="american-psycho-2000-3", title="American Psycho", year=2000)]
    client = FakeClient(listings, directors={"american-psycho-2000-3": "Mary Harron"})

    candidates = matcher.find_candidates(client, "American Psycho", "Mary Harron", year=2000)
    assert [c.slug for c in candidates] == ["american-psycho-2000-3"]


def test_find_candidates_year_filter_falls_through_when_year_data_missing():
    listings = [make_listing(fandango_id="1", slug="some-combo-booking", title="Casino", year=None)]
    client = FakeClient(listings, directors={"some-combo-booking": "Martin Scorsese"})

    candidates = matcher.find_candidates(client, "Casino", "Martin Scorsese", year=1995)
    assert [c.slug for c in candidates] == ["some-combo-booking"]


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
