from datetime import date

import requests

import fandango_client as fc

from conftest import FakeResponse, fake_html_response


def _client_with_stubbed_get(get):
    client = fc.FandangoClient.__new__(fc.FandangoClient)
    client._get = get
    return client


def test_search_scoped_to_results_not_promo_carousel():
    client = _client_with_stubbed_get(lambda url, **kw: fake_html_response("fandango_search_vertigo.html"))
    listings = client.search("vertigo")
    assert any(c.title == "Vertigo" and c.year == 1958 for c in listings)
    assert not any(c.title == "Buddy" for c in listings)


def test_fetch_director_from_overview_page():
    client = _client_with_stubbed_get(lambda url, **kw: fake_html_response("fandango_overview_vertigo.html"))
    assert client.fetch_director("vertigo-1958-1951") == "Alfred Hitchcock"


def test_looks_blocked_detects_marker_and_empty_body():
    assert fc._looks_blocked(FakeResponse(text="...A Message To Our Fans...")) is True
    assert fc._looks_blocked(FakeResponse(text="")) is True
    assert fc._looks_blocked(FakeResponse(text='{"hasShowtimes": false}')) is False


def _client_with_fake_session(get):
    """Bypasses __init__'s warmup request - only .session needs to be real."""
    client = fc.FandangoClient.__new__(fc.FandangoClient)
    client.session = type("FakeSession", (), {"get": staticmethod(get)})()
    return client


def test_get_retries_on_blocked_response(monkeypatch):
    monkeypatch.setattr(fc.time, "sleep", lambda s: None)
    responses = iter([FakeResponse(text=""), FakeResponse(text="ok")])
    client = _client_with_fake_session(lambda url, **kw: next(responses))

    resp = client._get("https://example.test/x")
    assert resp.text == "ok"


def test_get_retries_connection_exception(monkeypatch):
    monkeypatch.setattr(fc.time, "sleep", lambda s: None)
    calls = []

    def get(url, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise requests.exceptions.ConnectionError("refused")
        return FakeResponse(text="ok")

    client = _client_with_fake_session(get)
    resp = client._get("https://example.test/x")
    assert resp.text == "ok"
    assert len(calls) == 2


def test_movie_calendar_parses_showtime_dates():
    client = _client_with_stubbed_get(
        lambda url, **kw: FakeResponse(json_data={"movieCalendar": {"showtimeDates": ["2026-10-30"]}})
    )
    assert client._movie_calendar("fjord-2026-247279", "94158") == [date(2026, 10, 30)]


def test_movie_calendar_empty_when_no_calendar():
    client = _client_with_stubbed_get(
        lambda url, **kw: FakeResponse(json_data={"movieCalendar": None, "hasCalendar": False})
    )
    assert client._movie_calendar("paper-tiger-2026", "94158") == []


def test_check_showtimes_checks_only_the_real_calendar_dates(monkeypatch):
    """Regression test for a real production incident: a one-off limited
    engagement (a theater booking a film three weeks after its main
    release) sat outside any fixed date window we guessed, so it was
    missed entirely. Using Fandango's own movieCalendar - the same thing
    the real page's date picker uses - means there's no window to guess:
    we only ever check dates it says actually have something."""
    client = _client_with_stubbed_get(lambda url, **kw: FakeResponse(text="ok"))
    monkeypatch.setattr(client, "_movie_calendar", lambda slug, zip_code: [date(2026, 10, 30)])

    seen_dates = []

    def fake_showtime_grouping(fandango_id, zip_code, check_date, referer):
        seen_dates.append(check_date)
        return None

    monkeypatch.setattr(client, "_showtime_grouping", fake_showtime_grouping)
    client.check_showtimes("123", "some-slug", "94158")

    assert seen_dates == [date(2026, 10, 30)]


def test_check_showtimes_returns_none_when_calendar_is_empty(monkeypatch):
    client = _client_with_stubbed_get(lambda url, **kw: FakeResponse(text="ok"))
    monkeypatch.setattr(client, "_movie_calendar", lambda slug, zip_code: [])

    assert client.check_showtimes("123", "some-slug", "94158") is None
