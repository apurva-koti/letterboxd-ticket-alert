from datetime import date

import letterboxd_client as lc

from conftest import FakeResponse, fake_html_response, fixture_html


def test_parse_page_extracts_title_year_slug_url():
    films = lc._parse_page(fixture_html("letterboxd_watchlist_page1.html"))
    assert len(films) == 28
    assert films[0].title == "A Ghost Story"
    assert films[0].year == 2017
    assert films[0].slug == "a-ghost-story-2017"


def test_fetch_watchlist_stops_at_max_pages(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        assert "/page/1/" in url
        return fake_html_response("letterboxd_watchlist_page1.html")

    monkeypatch.setattr(lc.requests, "get", fake_get)
    films = lc.LetterboxdClient().fetch_watchlist("dave", delay=0, max_pages=1)

    assert len(films) == 28
    assert len(calls) == 1


def test_fetch_list_bare_url_for_page_one(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        if url.endswith("/share/tok123/"):
            return fake_html_response("letterboxd_list_hype.html")
        return FakeResponse(text="", status_code=403)

    monkeypatch.setattr(lc.requests, "get", fake_get)
    films = lc.LetterboxdClient().fetch_list("https://letterboxd.com/exampleuser/list/hype/share/tok123/")

    assert len(films) == 1
    assert films[0].slug == "dune-part-three"
    assert calls == [
        "https://letterboxd.com/exampleuser/list/hype/share/tok123/",
        "https://letterboxd.com/exampleuser/list/hype/share/tok123/page/2/",
        "https://letterboxd.com/exampleuser/list/hype/share/tok123/?page=2",
    ]


def test_fetch_list_stops_on_duplicate_page(monkeypatch):
    def fake_get(url, **kw):
        if "/page/2/" in url:
            return FakeResponse(text="", status_code=403)
        return fake_html_response("letterboxd_list_hype.html")

    monkeypatch.setattr(lc.requests, "get", fake_get)
    films = lc.LetterboxdClient().fetch_list("https://letterboxd.com/exampleuser/list/hype/share/tok123/", max_pages=10)
    assert len(films) == 1


def test_fetch_release_single_theatrical_date(monkeypatch):
    monkeypatch.setattr(lc.requests, "get", lambda url, **kw: fake_html_response("letterboxd_film_digger.html"))
    release = lc.LetterboxdClient().fetch_release("digger-2026")
    assert release.us_dates == [date(2026, 10, 2)]


def test_fetch_release_collects_dates_across_sections(monkeypatch):
    monkeypatch.setattr(lc.requests, "get", lambda url, **kw: fake_html_response("letterboxd_film_papertiger.html"))
    release = lc.LetterboxdClient().fetch_release("paper-tiger-2026")
    assert date(2026, 11, 13) in release.us_dates
    assert date(2026, 11, 20) in release.us_dates


def test_fetch_release_extracts_genre_and_director_for_documentary(monkeypatch):
    monkeypatch.setattr(lc.requests, "get", lambda url, **kw: fake_html_response("letterboxd_film_nuisancebear.html"))
    release = lc.LetterboxdClient().fetch_release("nuisance-bear-2026")
    assert "documentary" in release.genres
    assert release.director == "Gabriela Osio Vanden"


def test_fetch_release_no_false_positive_documentary(monkeypatch):
    monkeypatch.setattr(lc.requests, "get", lambda url, **kw: fake_html_response("letterboxd_film_vertigo.html"))
    release = lc.LetterboxdClient().fetch_release("vertigo")
    assert "documentary" not in release.genres
    assert "thriller" in release.genres
    assert release.director == "Alfred Hitchcock"


def test_fetch_release_extracts_runtime_in_minutes(monkeypatch):
    monkeypatch.setattr(lc.requests, "get", lambda url, **kw: fake_html_response("letterboxd_film_digger.html"))
    release = lc.LetterboxdClient().fetch_release("digger-2026")
    assert release.runtime == 129  # "PT2H9M"


def test_pick_anchor_prefers_earliest_upcoming():
    dates = [date(2026, 11, 20), date(2026, 11, 13)]
    assert lc.pick_anchor(dates, today=date(2026, 1, 1)) == date(2026, 11, 13)


def test_pick_anchor_falls_back_to_most_recent_past():
    dates = [date(1986, 5, 16), date(2013, 2, 8), date(2021, 5, 21), date(2026, 5, 13)]
    assert lc.pick_anchor(dates, today=date(2026, 9, 20)) == date(2026, 5, 13)


def test_pick_anchor_empty_list_returns_none():
    assert lc.pick_anchor([], today=date(2026, 1, 1)) is None


def test_get_with_retry_retries_server_error(monkeypatch):
    monkeypatch.setattr(lc.time, "sleep", lambda s: None)
    responses = iter([FakeResponse(status_code=500), FakeResponse(text="ok", status_code=200)])
    monkeypatch.setattr(lc.requests, "get", lambda url, **kw: next(responses))
    resp = lc.LetterboxdClient()._get("https://example.test/x")
    assert resp.status_code == 200


def test_get_with_retry_does_not_retry_4xx(monkeypatch):
    calls = []
    monkeypatch.setattr(lc.requests, "get", lambda url, **kw: calls.append(1) or FakeResponse(status_code=404))
    resp = lc.LetterboxdClient()._get("https://example.test/x")
    assert resp.status_code == 404
    assert len(calls) == 1


END_PAGE = "<html><head><title>dave’s Watchlist • Letterboxd</title></head><body><p>No films</p></body></html>"


def test_normal_end_of_watchlist_logs_one_summary_line(monkeypatch, caplog):
    pages = {1: fake_html_response("letterboxd_watchlist_page1.html"), 2: FakeResponse(text=END_PAGE)}
    monkeypatch.setattr(lc.requests, "get", lambda url, **kw: pages[int(url.rstrip("/").rsplit("/", 1)[1])])
    with caplog.at_level("INFO"):
        films = lc.LetterboxdClient().fetch_watchlist("dave", delay=0)

    assert len(films) == 28
    assert "Letterboxd watchlist dave: 28 film(s) across 1 page(s); stopped on status=200" in caplog.text
    assert "WARNING" not in caplog.text


def test_empty_watchlist_logs_what_letterboxd_sent(monkeypatch, caplog):
    """Regression test: on Nov 13 and Nov 25 the watchlist came back with no
    films and no error, deleting every film - with nothing in the logs to
    say what Letterboxd had actually returned."""
    page = "<html><head><title>Letterboxd - Down for maintenance</title></head><body>Back soon</body></html>"
    monkeypatch.setattr(lc.requests, "get", lambda url, **kw: FakeResponse(text=page))
    with caplog.at_level("INFO"):
        films = lc.LetterboxdClient().fetch_watchlist("dave", delay=0)

    assert films == []
    assert "Letterboxd watchlist dave: 0 film(s) across 0 page(s); stopped on status=200 title='Letterboxd - Down for maintenance'" in caplog.text
    assert "looks wrong (no films at all): text='Letterboxd - Down for maintenance Back soon'" in caplog.text
    assert "full body of that last page: <html>" in caplog.text


def test_watchlist_ending_on_an_unexpected_page_is_flagged(monkeypatch, caplog):
    other = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
    pages = {1: fake_html_response("letterboxd_watchlist_page1.html"), 2: FakeResponse(text=other)}
    monkeypatch.setattr(lc.requests, "get", lambda url, **kw: pages[int(url.rstrip("/").rsplit("/", 1)[1])])
    with caplog.at_level("INFO"):
        films = lc.LetterboxdClient().fetch_watchlist("dave", delay=0)

    assert len(films) == 28
    assert "looks wrong (unexpected last page)" in caplog.text
    assert "Checking your browser" in caplog.text


def test_full_page_is_logged_only_once_per_client(monkeypatch, caplog):
    monkeypatch.setattr(lc.requests, "get", lambda url, **kw: FakeResponse(text="<html><body>oops</body></html>"))
    client = lc.LetterboxdClient()
    with caplog.at_level("WARNING"):
        client.fetch_watchlist("dave", delay=0)
        client.fetch_list("https://letterboxd.com/dave/list/hype/", delay=0)
    assert caplog.text.count("full body of that last page") == 1
    assert caplog.text.count("looks wrong (no films at all)") == 2
