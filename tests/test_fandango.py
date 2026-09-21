import json
from datetime import date, timedelta

import requests

import fandango

from conftest import FakeSession, fake_html_response, make_fandango_candidate


def test_normalize_folds_ampersand_to_and():
    assert fandango._normalize("Fast & Furious") == fandango._normalize("Fast and Furious")


def test_normalize_strips_diacritics():
    assert fandango._normalize("Alejandro G. Iñárritu") == "alejandro g inarritu"


def test_looks_blocked_detects_marker_and_empty_body():
    assert fandango._looks_blocked(_FakeResp("...A Message To Our Fans...")) is True
    assert fandango._looks_blocked(_FakeResp("")) is True
    assert fandango._looks_blocked(_FakeResp("   ")) is True  # whitespace-only counts as empty
    assert fandango._looks_blocked(_FakeResp('{"hasShowtimes": false}')) is False


def test_get_with_retry_backs_off_exponentially_between_attempts(monkeypatch):
    sleeps = []
    monkeypatch.setattr(fandango.time, "sleep", lambda s: sleeps.append(s))

    session = FakeSession(lambda url, params: _FakeResp(""))  # always blocked
    fandango._get_with_retry(session, "https://example.test/x")

    assert len(session.calls) == fandango.RETRY_ATTEMPTS
    assert sleeps == [
        fandango.RETRY_BASE_DELAY_SECONDS * (2**0),
        fandango.RETRY_BASE_DELAY_SECONDS * (2**1),
    ]
    assert sleeps[1] > sleeps[0]  # genuinely increasing, not a fixed delay


def test_get_with_retry_stops_as_soon_as_a_clean_response_arrives(monkeypatch):
    monkeypatch.setattr(fandango.time, "sleep", lambda s: None)
    responses = iter([_FakeResp(""), _FakeResp('{"ok": true}')])
    session = FakeSession(lambda url, params: next(responses))

    resp = fandango._get_with_retry(session, "https://example.test/x")
    assert resp.text == '{"ok": true}'
    assert len(session.calls) == 2  # didn't burn the 3rd retry once it succeeded


def test_get_with_retry_retries_a_connection_exception(monkeypatch):
    """Regression test for a real gap: session.get() itself raising (a plain
    timeout or connection error, not a "blocked-looking" 200) used to skip
    the retry loop entirely and fail on the very first attempt."""
    monkeypatch.setattr(fandango.time, "sleep", lambda s: None)
    calls = []

    def router(url, params):
        calls.append(1)
        if len(calls) == 1:
            raise requests.exceptions.ConnectionError("refused")
        return _FakeResp('{"ok": true}')

    session = FakeSession(router)
    resp = fandango._get_with_retry(session, "https://example.test/x")
    assert resp.text == '{"ok": true}'
    assert len(calls) == 2


def test_get_with_retry_raises_when_every_attempt_fails_to_connect(monkeypatch):
    monkeypatch.setattr(fandango.time, "sleep", lambda s: None)

    def router(url, params):
        raise requests.exceptions.ReadTimeout("timed out")

    session = FakeSession(router)
    try:
        fandango._get_with_retry(session, "https://example.test/x")
        assert False, "expected ReadTimeout to propagate"
    except requests.exceptions.ReadTimeout:
        pass


def test_search_movie_retries_a_blocked_response(monkeypatch):
    """Confirms the shared retry helper is actually wired into search_movie,
    not just check_ticket_status - any Fandango endpoint here is equally
    exposed to the same intermittent block."""
    monkeypatch.setattr(fandango.time, "sleep", lambda s: None)
    responses = iter([_FakeResp(""), fake_html_response("fandango_search_vertigo.html")])
    session = FakeSession(lambda url, params: next(responses))

    candidates = fandango.search_movie(session, "vertigo")
    assert any(c.title == "Vertigo" for c in candidates)


class _FakeResp:
    def __init__(self, text):
        self.text = text
        self.status_code = 200

    def raise_for_status(self):
        pass


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


def test_match_movie_declines_sole_candidate_when_director_contradicts(monkeypatch):
    """Regression test for a real mismatch caught in production: Letterboxd's
    "Center Stage" (1991) confidently matched Fandango's unrelated "Center
    Stage" (2000) - same title, nothing else cataloged to tie against, so
    title score alone had nothing to stop it. A sole, unambiguous top
    candidate must now also be confirmed by director, not accepted on title
    score alone - a contradiction there means decline, not guess."""
    candidates = [make_fandango_candidate(fandango_id="1", slug="center-stage-2000", title="Center Stage", year=2000)]
    monkeypatch.setattr(fandango, "search_movie", lambda session, title: candidates)
    monkeypatch.setattr(fandango, "get_director", lambda session, slug: "Nicholas Hytner")

    match, _ = fandango.match_movie(None, "Center Stage", year=1991, director="Robert Sidney")
    assert match is None


def test_match_movie_accepts_sole_candidate_when_director_confirms(monkeypatch):
    """The flip side: a large year gap (e.g. a legacy film's original year vs.
    a Fandango re-release listing) should still match once the director
    genuinely lines up - this is exactly the shape of a real re-release
    (Top Gun-style), not a false-positive risk like the mismatch above."""
    candidates = [make_fandango_candidate(fandango_id="1", slug="top-gun-2026", title="Top Gun", year=2026)]
    monkeypatch.setattr(fandango, "search_movie", lambda session, title: candidates)
    monkeypatch.setattr(fandango, "get_director", lambda session, slug: "Tony Scott")

    match, _ = fandango.match_movie(None, "Top Gun", year=1986, director="Tony Scott")

    assert match is not None
    assert match.slug == "top-gun-2026"


def test_match_movie_checks_director_even_without_ambiguity_or_year_gap(monkeypatch):
    """The director confirmation isn't gated by a tie or a suspicious year gap
    - it's checked whenever a director is available at all, since the Center
    Stage mismatch above had neither of those signals either. Even an
    ordinary, matching-year single candidate gets the extra request."""
    candidates = [make_fandango_candidate(fandango_id="1", slug="some-film", title="Some Film", year=2026)]
    director_calls = []
    monkeypatch.setattr(fandango, "search_movie", lambda session, title: candidates)
    monkeypatch.setattr(fandango, "get_director", lambda session, slug: director_calls.append(1) or "Someone")

    match, _ = fandango.match_movie(None, "Some Film", year=2026, director="Someone")

    assert match is not None
    assert director_calls == [1]  # the confirmation request did happen


def test_match_movie_falls_back_to_title_only_when_no_director_given(monkeypatch):
    """When the caller has no Letterboxd director to compare (e.g. it
    couldn't be parsed), there's nothing to confirm or refute - a sole,
    unambiguous title match should still be accepted rather than declined
    just because that data happens to be missing."""
    candidates = [make_fandango_candidate(fandango_id="1", slug="some-film", title="Some Film", year=2026)]
    director_calls = []
    monkeypatch.setattr(fandango, "search_movie", lambda session, title: candidates)
    monkeypatch.setattr(fandango, "get_director", lambda session, slug: director_calls.append(1) or "Someone")

    match, _ = fandango.match_movie(None, "Some Film", year=2026)  # no director passed at all

    assert match is not None
    assert director_calls == []  # nothing to confirm against - never even fetched


def test_match_movie_falls_back_to_title_only_when_fandango_has_no_director(monkeypatch):
    """When a director IS given but Fandango's own page doesn't credit one for
    the candidate (a real, fairly common gap), that's a missing signal, not a
    contradiction - it shouldn't be treated the same as a real mismatch."""
    candidates = [make_fandango_candidate(fandango_id="1", slug="some-film", title="Some Film", year=2026)]
    monkeypatch.setattr(fandango, "search_movie", lambda session, title: candidates)
    monkeypatch.setattr(fandango, "get_director", lambda session, slug: None)

    match, _ = fandango.match_movie(None, "Some Film", year=2026, director="Someone")

    assert match is not None


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


def test_check_ticket_status_also_scans_around_a_far_off_release_date():
    """Real production bug found and fixed: for a film whose release is
    beyond the near-term window, Fandango can have real purchasable showtimes
    already posted around the release date itself, with nothing in between -
    confirmed on Dune: Part Three, ~90 days out: the near-term window was
    completely empty while real on_sale data already existed around its
    December release."""
    today = date.today()
    release_date = today + timedelta(days=90)
    hit_date = (release_date - timedelta(days=2)).isoformat()  # within the -7/+7 window

    def router(url, params):
        if hit_date in url:
            return _FakeJsonResponse(
                _showtime_response([_theater("Alamo Drafthouse", [{"type": "available", "isSoldOut": False}])])
            )
        return _FakeJsonResponse({"hasShowtimes": False, "theaterShowtimes": {"theaters": []}})

    session = FakeSession(router)
    result = fandango.check_ticket_status(session, "1", "slug", "94158", release_date=release_date, days_ahead=14)

    assert result is not None
    assert result.status == fandango.STATUS_ON_SALE
    assert result.date == hit_date


def test_check_ticket_status_skips_release_window_when_release_is_near_term():
    """If release_date already falls inside the near-term window, there's no
    separate release-window scan needed - the near-term scan already covers it,
    so no extra requests should be made."""
    calls = []
    today = date.today()
    release_date = today + timedelta(days=5)  # well within days_ahead=14

    def router(url, params):
        calls.append(url)
        return _FakeJsonResponse({"hasShowtimes": False, "theaterShowtimes": {"theaters": []}})

    session = FakeSession(router)
    fandango.check_ticket_status(session, "1", "slug", "94158", release_date=release_date, days_ahead=14)

    assert len(calls) == 14  # only the near-term window - no extra release-window dates


def test_check_ticket_status_skips_a_date_still_malformed_after_retries(monkeypatch):
    """Real production incident: Fandango (via Akamai bot-management) can
    serve an HTML block page instead of JSON. If a date is STILL malformed
    after MALFORMED_RESPONSE_RETRIES retries, it's skipped (not crashed on) -
    later dates in the window still get scanned normally."""
    monkeypatch.setattr(fandango.time, "sleep", lambda s: None)

    responses = iter(
        [_FakeBadJsonResponse()] * fandango.RETRY_ATTEMPTS  # day 0: bad every attempt (initial + all retries)
        + [_FakeJsonResponse(_showtime_response([_theater("Alamo Drafthouse", [{"type": "available", "isSoldOut": False}])]))]  # day 1: fine
    )
    session = FakeSession(lambda url, params: next(responses))

    result = fandango.check_ticket_status(session, "1", "slug", "94158", days_ahead=2)
    assert result.status == fandango.STATUS_ON_SALE
    assert result.on_sale_theaters == ["Alamo Drafthouse"]


def test_check_ticket_status_retries_a_blocked_response_and_succeeds(monkeypatch):
    """The actual real-world case this was built for: one blocked/malformed
    response, then a clean one on retry for the SAME date - the retry has to
    actually recover the real data, not just move on to the next date."""
    monkeypatch.setattr(fandango.time, "sleep", lambda s: None)

    responses = iter(
        [
            _FakeBadJsonResponse(),  # first attempt at day 0: blocked
            _FakeJsonResponse(_showtime_response([_theater("Alamo Drafthouse", [{"type": "available", "isSoldOut": False}])])),  # retry: real data
        ]
    )
    session = FakeSession(lambda url, params: next(responses))

    result = fandango.check_ticket_status(session, "1", "slug", "94158", days_ahead=2)
    assert result.status == fandango.STATUS_ON_SALE
    assert result.date == date.today().isoformat()  # found on day 0's retry, not day 1


class _FakeJsonResponse:
    def __init__(self, data):
        self._data = data
        self.status_code = 200
        self.text = json.dumps(data)

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeBadJsonResponse:
    """A 200 response whose body isn't valid JSON - reproduces the real
    empty-body case without needing an actual malformed-JSON fixture."""

    status_code = 200
    text = ""  # empty body - not valid JSON, and doesn't contain BLOCK_PAGE_MARKER either

    def raise_for_status(self):
        pass

    def json(self):
        import json as _json

        _json.loads("")  # raises json.JSONDecodeError, a ValueError subclass
