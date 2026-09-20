from datetime import date

import fandango

from conftest import FakeSession, fake_html_response, make_fandango_candidate


def test_normalize_folds_ampersand_to_and():
    assert fandango._normalize("Fast & Furious") == fandango._normalize("Fast and Furious")


def test_normalize_strips_diacritics():
    assert fandango._normalize("Alejandro G. Iñárritu") == "alejandro g inarritu"


def test_search_movie_scoped_to_results_not_promo_carousel(monkeypatch):
    """The search page also renders an unrelated "Coming Soon" carousel with
    titles like Buddy/Coyote vs. Acme regardless of query - search_movie must
    not pick those up as if they were search results."""
    session = FakeSession(lambda url, params: fake_html_response("fandango_search_vertigo.html"))
    candidates = fandango.search_movie(session, "vertigo")

    assert any(c.title == "Vertigo" and c.year == 1958 for c in candidates)
    assert not any(c.title == "Buddy" for c in candidates)
    assert not any(c.title == "Coyote vs. Acme" for c in candidates)


def test_match_movie_picks_best_title_match():
    session = FakeSession(lambda url, params: fake_html_response("fandango_search_vertigo.html"))
    match, candidates = fandango.match_movie(session, "Vertigo", year=1958)
    assert match.title == "Vertigo"
    assert match.fandango_id == "1951"


def test_match_movie_refuses_ambiguous_tie_without_director(monkeypatch):
    candidates = [
        make_fandango_candidate(fandango_id="1", slug="insomnia-1", title="Insomnia", year=2002),
        make_fandango_candidate(fandango_id="2", slug="insomnia-2", title="Insomnia", year=1997),
    ]
    monkeypatch.setattr(fandango, "search_movie", lambda session, title: candidates)

    match, _ = fandango.match_movie(None, "Insomnia")
    assert match is None


def test_match_movie_resolves_tie_via_fuzzy_director_match(monkeypatch):
    """Letterboxd and Fandango credit directors with different name forms
    (e.g. abbreviated middle name) - the comparison must be fuzzy, not exact."""
    candidates = [
        make_fandango_candidate(fandango_id="1", slug="insomnia-1", title="Insomnia", year=2002),
        make_fandango_candidate(fandango_id="2", slug="insomnia-2", title="Insomnia", year=1997),
    ]
    directors = {"insomnia-1": "Christopher Nolan", "insomnia-2": "Erik Skjoldbjaerg"}
    monkeypatch.setattr(fandango, "search_movie", lambda session, title: candidates)
    monkeypatch.setattr(fandango, "get_director", lambda session, slug: directors[slug])

    match, _ = fandango.match_movie(None, "Insomnia", director="Christopher Nolan")
    assert match.slug == "insomnia-1"

    match, _ = fandango.match_movie(None, "Insomnia", director="Someone Else")
    assert match is None


def test_get_director_from_overview_page(monkeypatch):
    session = FakeSession(lambda url, params: fake_html_response("fandango_overview_vertigo.html"))
    assert fandango.get_director(session, "vertigo-1958-1951") == "Alfred Hitchcock"


def test_get_release_date_from_overview_page(monkeypatch):
    session = FakeSession(lambda url, params: fake_html_response("fandango_overview_digger.html"))
    assert fandango.get_release_date(session, "digger-2026-245150") == date(2026, 10, 2)


# ---- check_ticket_status classification ----
# The `type` field on a showtime (not `hasShowtimes` alone) is what actually
# distinguishes a purchasable slot from one Fandango lists before sales open.

def _showtime_response(theaters):
    return {"hasShowtimes": True, "theaterShowtimes": {"theaters": theaters}}


def _theater(name, showtimes):
    return {"name": name, "variants": [{"amenityGroups": [{"showtimes": showtimes}]}]}


def test_check_ticket_status_on_sale_when_type_available():
    data = _showtime_response([_theater("Alamo Drafthouse", [{"type": "available", "isSoldOut": False}])])
    session = FakeSession(lambda url, params: _FakeJsonResponse(data))

    result = fandango.check_ticket_status(session, "246407", "your-mother-x3", "94158", days_ahead=1)
    assert result.status == fandango.STATUS_ON_SALE
    assert result.on_sale_theaters == ["Alamo Drafthouse"]
    assert result.showtimes_only_theaters == []


def test_check_ticket_status_announced_when_type_restricted():
    data = _showtime_response(
        [_theater("AMC Metreon 16", [{"type": "restricted", "isSoldOut": False, "message": "Tickets coming soon"}])]
    )
    session = FakeSession(lambda url, params: _FakeJsonResponse(data))

    result = fandango.check_ticket_status(session, "245150", "digger-2026", "94158", days_ahead=1)
    assert result.status == fandango.STATUS_SHOWTIMES_ANNOUNCED
    assert result.on_sale_theaters == []
    assert result.showtimes_only_theaters == ["AMC Metreon 16"]


def test_check_ticket_status_sold_out_still_counts_as_on_sale():
    data = _showtime_response([_theater("AMC Kabuki 8", [{"type": "restricted", "isSoldOut": True}])])
    session = FakeSession(lambda url, params: _FakeJsonResponse(data))

    result = fandango.check_ticket_status(session, "1", "slug", "94158", days_ahead=1)
    assert result.status == fandango.STATUS_ON_SALE
    assert result.on_sale_theaters == ["AMC Kabuki 8"]


def test_check_ticket_status_none_when_no_showtimes():
    data = {"hasShowtimes": False, "theaterShowtimes": {"theaters": []}}
    session = FakeSession(lambda url, params: _FakeJsonResponse(data))

    result = fandango.check_ticket_status(session, "1", "slug", "94158", days_ahead=2)
    assert result is None


def test_check_ticket_status_mixed_theaters_bucket_separately():
    data = _showtime_response(
        [
            _theater("On Sale Cinema", [{"type": "available", "isSoldOut": False}]),
            _theater("Announced Only Cinema", [{"type": "restricted", "isSoldOut": False}]),
        ]
    )
    session = FakeSession(lambda url, params: _FakeJsonResponse(data))

    result = fandango.check_ticket_status(session, "1", "slug", "94158", days_ahead=1)
    assert result.status == fandango.STATUS_ON_SALE  # on_sale wins if any theater has it
    assert result.on_sale_theaters == ["On Sale Cinema"]
    assert result.showtimes_only_theaters == ["Announced Only Cinema"]


def test_check_ticket_status_skips_a_date_with_malformed_json_instead_of_crashing():
    """Real production incident: Fandango occasionally returns a 200 with an
    empty/non-JSON body for one date. That date should be skipped, not crash
    the whole check - later dates in the window still get scanned normally."""
    responses = iter(
        [
            _FakeBadJsonResponse(),  # day 0: malformed
            _FakeJsonResponse(_showtime_response([_theater("Alamo Drafthouse", [{"type": "available", "isSoldOut": False}])])),  # day 1: fine
        ]
    )
    session = FakeSession(lambda url, params: next(responses))

    result = fandango.check_ticket_status(session, "1", "slug", "94158", days_ahead=2)
    assert result.status == fandango.STATUS_ON_SALE
    assert result.on_sale_theaters == ["Alamo Drafthouse"]


class _FakeJsonResponse:
    def __init__(self, data):
        self._data = data
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeBadJsonResponse:
    """A 200 response whose body isn't valid JSON - reproduces the real
    empty-body case without needing an actual malformed-JSON fixture."""

    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        import json

        json.loads("")  # raises json.JSONDecodeError, a ValueError subclass
