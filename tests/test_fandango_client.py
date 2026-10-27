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


def test_fetch_details_from_overview_page():
    client = _client_with_stubbed_get(lambda url, **kw: fake_html_response("fandango_overview_vertigo.html"))
    director, runtime, synopsis = client.fetch_details("vertigo-1958-1951")
    assert director == "Alfred Hitchcock"
    assert runtime == 128
    assert synopsis and "San Francisco" in synopsis


def test_fetch_details_prefers_the_real_synopsis_over_the_rating_blurb():
    """Regression test: the page's JSON-LD also has a same-named nested
    "description" field inside aggregateRating (explaining what the
    Audience Score means) - a naive regex grabbed that one by mistake
    instead of the real movie synopsis."""
    client = _client_with_stubbed_get(lambda url, **kw: fake_html_response("fandango_overview_vertigo.html"))
    _, _, synopsis = client.fetch_details("vertigo-1958-1951")
    assert "Audience Score" not in synopsis


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


def test_nearby_theaters_sorted_by_distance_within_radius():
    seen = {}

    def get(url, **kw):
        seen.update(kw["params"])
        return FakeResponse(
            json_data={
                "theaters": [
                    {"name": "AMC Mercado 20", "distance": 34.0},
                    {"name": "Far Away Cinema", "distance": 51.2},
                    {"name": "Alamo Drafthouse Cinema New Mission", "distance": 1.4},
                ]
            }
        )

    client = _client_with_stubbed_get(get)
    assert client.nearby_theaters("94158", 50) == [("Alamo Drafthouse Cinema New Mission", 1.4), ("AMC Mercado 20", 34.0)]
    assert seen["radius"] == 50


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
    monkeypatch.setattr(client, "_movie_calendar", lambda slug, zip_code, trace=None: [date(2026, 10, 30)])

    seen_dates = []

    def fake_showtime_grouping(fandango_id, zip_code, check_date, referer, limit=fc.DEFAULT_THEATER_LIMIT, trace=None):
        seen_dates.append(check_date)
        return None

    monkeypatch.setattr(client, "_showtime_grouping", fake_showtime_grouping)
    client.check_showtimes("123", "some-slug", "94158")

    assert seen_dates == [date(2026, 10, 30)]


def test_check_showtimes_returns_none_when_calendar_is_empty(monkeypatch):
    client = _client_with_stubbed_get(lambda url, **kw: FakeResponse(text="ok"))
    monkeypatch.setattr(client, "_movie_calendar", lambda slug, zip_code, trace=None: [])

    assert client.check_showtimes("123", "some-slug", "94158") is None


def test_movie_calendar_logs_on_parse_failure(caplog):
    """A JSON parse failure here is indistinguishable from a genuinely
    empty calendar to every caller - logged so the two can finally be
    told apart after the fact."""
    client = _client_with_stubbed_get(lambda url, **kw: FakeResponse(text="not json", status_code=200))
    with caplog.at_level("WARNING"):
        result = client._movie_calendar("misty-green-2026-247569", "94158")

    assert result == []
    assert "didn't parse as JSON" in caplog.text
    assert "misty-green-2026-247569" in caplog.text


def test_showtime_grouping_logs_on_parse_failure(caplog):
    client = _client_with_stubbed_get(lambda url, **kw: FakeResponse(text="not json", status_code=200))
    with caplog.at_level("WARNING"):
        result = client._showtime_grouping("123", "94158", date(2026, 10, 30), "referer")

    assert result is None
    assert "didn't parse as JSON" in caplog.text


def test_check_showtimes_logs_when_calendar_has_dates_but_nothing_found(monkeypatch, caplog):
    """Distinguishes "no calendar dates at all" from "had dates, but every
    per-date lookup came back empty" - the second case is the one with no
    redundancy left to fall back on for a film with only a date or two."""
    client = _client_with_stubbed_get(lambda url, **kw: FakeResponse(text="ok"))
    monkeypatch.setattr(client, "_movie_calendar", lambda slug, zip_code, trace=None: [date(2026, 10, 30)])
    monkeypatch.setattr(client, "_showtime_grouping", lambda *a, **k: None)

    with caplog.at_level("WARNING"):
        result = client.check_showtimes("123", "misty-green-2026-247569", "94158")

    assert result is None
    assert "but none had an upcoming showing" in caplog.text


def test_get_logs_each_blocked_attempt_and_the_final_fallback(monkeypatch, caplog):
    monkeypatch.setattr(fc.time, "sleep", lambda s: None)
    client = _client_with_fake_session(lambda url, **kw: FakeResponse(text=""))

    with caplog.at_level("WARNING"):
        resp = client._get("https://example.test/x")

    assert resp.text == ""
    assert caplog.text.count("looks blocked") == fc.RETRY_ATTEMPTS
    assert "still blocked after" in caplog.text


def _grouping(*theaters):
    """theaters: (name, showtime type) pairs."""
    return {
        "hasShowtimes": True,
        "theaterShowtimes": {
            "theaters": [
                {"name": name, "variants": [{"amenityGroups": [{"showtimes": [{"type": kind}]}]}]} for name, kind in theaters
            ]
        },
    }


def test_find_on_sale_at_looks_past_the_first_date_with_every_theater(monkeypatch):
    client = _client_with_stubbed_get(lambda url, **kw: FakeResponse(text="ok"))
    monkeypatch.setattr(client, "_movie_calendar", lambda slug, zip_code, trace=None: [date(2026, 12, 18), date(2026, 12, 25), date(2026, 12, 26)])
    by_date = {
        date(2026, 12, 18): _grouping(("Alamo Drafthouse Cinema New Mission", "available")),
        date(2026, 12, 25): _grouping(("Alamo Drafthouse Cinema New Mission", "available"), ("AMC Metreon 16", "available")),
    }
    calls = []

    def fake_grouping(fandango_id, zip_code, check_date, referer, limit=fc.DEFAULT_THEATER_LIMIT, trace=None):
        calls.append((check_date, limit))
        return by_date.get(check_date)

    monkeypatch.setattr(client, "_showtime_grouping", fake_grouping)
    found = client.find_on_sale_at("1", "werwulf", "94158", frozenset({"AMC Metreon 16", "AMC Kabuki 8"}))

    assert found == {"AMC Metreon 16"}
    assert calls == [(date(2026, 12, 18), fc.FULL_THEATER_LIMIT), (date(2026, 12, 25), fc.FULL_THEATER_LIMIT)]  # stops once found


def test_find_on_sale_at_ignores_showtimes_not_yet_bookable(monkeypatch):
    client = _client_with_stubbed_get(lambda url, **kw: FakeResponse(text="ok"))
    monkeypatch.setattr(client, "_movie_calendar", lambda slug, zip_code, trace=None: [date(2026, 12, 25)])
    monkeypatch.setattr(
        client, "_showtime_grouping", lambda *a, **k: _grouping(("AMC Metreon 16", "notAvailable"))
    )
    assert client.find_on_sale_at("1", "werwulf", "94158", frozenset({"AMC Metreon 16"})) == set()


def test_finished_day_is_skipped_for_the_next_date(monkeypatch):
    """Regression test for the duplicate emails (Sense and Sensibility,
    Oct 26 12:30am PDT): the date list still started with the day that
    had just ended, every showing on it was "pastshowtime", and that read
    as "listed, not bookable" - the film dropped off sale and the next
    check re-alerted. A finished day must be skipped instead."""
    client = _client_with_stubbed_get(lambda url, **kw: FakeResponse(text="ok"))
    monkeypatch.setattr(client, "_movie_calendar", lambda slug, zip_code, trace=None: [date(2026, 10, 25), date(2026, 10, 26)])
    by_date = {
        date(2026, 10, 25): _grouping(("AMC Metreon 16", "pastshowtime"), ("Apple Cinemas Van Ness", "pastshowtime")),
        date(2026, 10, 26): _grouping(("AMC Metreon 16", "available"), ("Presidio Theatre", "available")),
    }
    monkeypatch.setattr(client, "_showtime_grouping", lambda fid, z, d, ref, limit=fc.DEFAULT_THEATER_LIMIT, trace=None: by_date[d])

    check = client.check_showtimes("244998", "sense-and-sensibility-2026-244998", "94158")

    assert check.checked_date == date(2026, 10, 26)
    assert check.status == fc.TicketStatus.ON_SALE
    assert check.on_sale_theaters == ["AMC Metreon 16", "Presidio Theatre"]
    assert check.showtimes_only_theaters == []


def test_past_showings_dont_make_a_theater_showtimes_only_when_later_ones_remain(monkeypatch):
    """Tonight with a 7pm show done and a 10pm show still bookable: on sale."""
    client = _client_with_stubbed_get(lambda url, **kw: FakeResponse(text="ok"))
    monkeypatch.setattr(client, "_movie_calendar", lambda slug, zip_code, trace=None: [date(2026, 10, 26)])
    grouping = _grouping(("AMC Metreon 16", "pastshowtime"), ("The New Parkway", "pastshowtime"))
    grouping["theaterShowtimes"]["theaters"][0]["variants"][0]["amenityGroups"][0]["showtimes"].append({"type": "available"})
    monkeypatch.setattr(client, "_showtime_grouping", lambda *a, **k: grouping)

    check = client.check_showtimes("1", "naza", "94158")

    assert check.on_sale_theaters == ["AMC Metreon 16"]
    assert check.showtimes_only_theaters == []  # The New Parkway's only show is over - not "listed"


def test_only_finished_days_left_means_no_showtimes(monkeypatch):
    client = _client_with_stubbed_get(lambda url, **kw: FakeResponse(text="ok"))
    monkeypatch.setattr(client, "_movie_calendar", lambda slug, zip_code, trace=None: [date(2026, 10, 26)])
    monkeypatch.setattr(client, "_showtime_grouping", lambda *a, **k: _grouping(("The New Parkway", "pastshowtime")))
    assert client.check_showtimes("1", "naza", "94158") is None


def test_unknown_showtime_type_is_flagged_on_the_trace(monkeypatch):
    from checktrace import CandidateTrace

    client = _client_with_stubbed_get(lambda url, **kw: FakeResponse(text="ok"))
    monkeypatch.setattr(client, "_movie_calendar", lambda slug, zip_code, trace=None: [date(2026, 12, 25)])
    monkeypatch.setattr(client, "_showtime_grouping", lambda *a, **k: _grouping(("AMC Metreon 16", "presale")))
    trace = CandidateTrace(label="werwulf")

    client.check_showtimes("1", "werwulf", "94158", trace=trace)

    assert trace.anomalies == ["unknown showtime type 'presale' at AMC Metreon 16 2026-12-25"]
    assert trace.dates[0].theaters[0].other == ["presale ?"]
