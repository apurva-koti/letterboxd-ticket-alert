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


import pytest


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(lc.time, "sleep", lambda s: None)


def _route(pages):
    """fake requests.get serving {page number: response} for watchlist URLs."""
    return lambda url, **kw: pages[int(url.rstrip("/").rsplit("/", 1)[1])]


CHALLENGE = FakeResponse(
    text="<html><head><title>Just a moment...</title></head><body>Enable JavaScript and cookies to continue</body></html>",
    status_code=403,
)
CHALLENGE.headers = {"cf-mitigated": "challenge"}


def test_watchlist_page_that_isnt_a_watchlist_raises_and_logs_it(monkeypatch, caplog, no_sleep):
    """Regression test: on Nov 13 and Nov 25 the watchlist came back with no
    films and no error, and the sync deleted every film. A page with no
    films that isn't a real watchlist page must fail the fetch instead."""
    page = "<html><head><title>Letterboxd - Down for maintenance</title></head><body>Back soon</body></html>"
    monkeypatch.setattr(lc.requests, "get", lambda url, **kw: FakeResponse(text=page))
    with caplog.at_level("INFO"), pytest.raises(lc.LetterboxdPageError, match="isn't a watchlist page"):
        lc.LetterboxdClient().fetch_watchlist("dave", delay=0)
    assert "title='Letterboxd - Down for maintenance'" in caplog.text
    assert "text='Letterboxd - Down for maintenance Back soon'" in caplog.text
    assert "full body of that last page: <html>" in caplog.text


def test_unexpected_page_mid_watchlist_raises_instead_of_truncating(monkeypatch, no_sleep):
    other = FakeResponse(text="<html><head><title>Error</title></head><body>Something went wrong</body></html>")
    monkeypatch.setattr(lc.requests, "get", _route({1: fake_html_response("letterboxd_watchlist_page1.html"), 2: other}))
    with pytest.raises(lc.LetterboxdPageError):
        lc.LetterboxdClient().fetch_watchlist("dave", delay=0)


def test_genuinely_empty_watchlist_is_allowed(monkeypatch, no_sleep):
    monkeypatch.setattr(lc.requests, "get", lambda url, **kw: FakeResponse(text=END_PAGE))
    assert lc.LetterboxdClient().fetch_watchlist("dave", delay=0) == []


def test_watchlist_404_on_page_one_raises(monkeypatch, no_sleep):
    monkeypatch.setattr(lc.requests, "get", lambda url, **kw: FakeResponse(text="<html><title>Not found</title></html>", status_code=404))
    with pytest.raises(lc.LetterboxdPageError, match="404 on page 1"):
        lc.LetterboxdClient().fetch_watchlist("dave", delay=0)


def test_watchlist_404_after_some_pages_raises_instead_of_truncating(monkeypatch, no_sleep):
    """Regression test for Nov 25: the fetch stopped after page 2, and the
    sync kept 56 of 271 films and deleted the other 215."""
    not_found = FakeResponse(text="<html><title>Not found</title></html>", status_code=404)
    page = fake_html_response("letterboxd_watchlist_page1.html")
    monkeypatch.setattr(lc.requests, "get", _route({1: page, 2: page, 3: not_found}))
    with pytest.raises(lc.LetterboxdPageError, match="404 on page 3"):
        lc.LetterboxdClient().fetch_watchlist("dave", delay=0)


def test_cloudflare_check_is_retried_with_backoff_then_passes(monkeypatch, caplog):
    sleeps = []
    monkeypatch.setattr(lc.time, "sleep", sleeps.append)
    responses = iter([CHALLENGE, fake_html_response("letterboxd_watchlist_page1.html"), FakeResponse(text=END_PAGE)])
    monkeypatch.setattr(lc.requests, "get", lambda url, **kw: next(responses))
    with caplog.at_level("WARNING"):
        films = lc.LetterboxdClient().fetch_watchlist("dave", delay=0)
    assert len(films) == 28
    assert "Letterboxd Cloudflare check (attempt 1/3)" in caplog.text
    assert sleeps[0] == lc.RETRY_BASE_DELAY_SECONDS


def test_cloudflare_check_that_never_clears_raises(monkeypatch, caplog, no_sleep):
    calls = []
    monkeypatch.setattr(lc.requests, "get", lambda url, **kw: calls.append(url) or CHALLENGE)
    with caplog.at_level("WARNING"), pytest.raises(lc.LetterboxdPageError, match="Cloudflare check"):
        lc.LetterboxdClient().fetch_watchlist("dave", delay=0)
    assert len(calls) == lc.RETRY_ATTEMPTS
    assert "Enable JavaScript and cookies to continue" in caplog.text


def test_hype_list_cloudflare_check_on_page_one_raises(monkeypatch, no_sleep):
    """Seen live: the Hype share link got Cloudflare's check (403) - the old
    code read that as an empty Hype list."""
    monkeypatch.setattr(lc.requests, "get", lambda url, **kw: CHALLENGE)
    with pytest.raises(lc.LetterboxdPageError, match="Cloudflare check"):
        lc.LetterboxdClient().fetch_list("https://letterboxd.com/dave/list/hype/share/tok123/")


def test_hype_list_plain_403_on_page_one_raises(monkeypatch, no_sleep):
    monkeypatch.setattr(lc.requests, "get", lambda url, **kw: FakeResponse(text="<html><title>Forbidden</title></html>", status_code=403))
    with pytest.raises(lc.LetterboxdPageError, match="403 on page 1"):
        lc.LetterboxdClient().fetch_list("https://letterboxd.com/dave/list/hype/share/tok123/")


def test_share_link_plain_403_past_page_one_is_still_a_normal_end(monkeypatch, no_sleep):
    def get(url, **kw):
        if url.endswith("/tok123/"):
            return fake_html_response("letterboxd_watchlist_page1.html")
        return FakeResponse(text="<html><title>Forbidden</title></html>", status_code=403)

    monkeypatch.setattr(lc.requests, "get", get)
    assert len(lc.LetterboxdClient().fetch_list("https://letterboxd.com/dave/list/hype/share/tok123/")) == 28


def test_full_page_is_logged_only_once_per_client(monkeypatch, caplog, no_sleep):
    monkeypatch.setattr(lc.requests, "get", lambda url, **kw: FakeResponse(text="<html><body>oops</body></html>"))
    client = lc.LetterboxdClient()
    with caplog.at_level("WARNING"):
        for fetch in (lambda: client.fetch_watchlist("dave", delay=0), lambda: client.fetch_list("https://letterboxd.com/dave/list/hype/", delay=0)):
            with pytest.raises(lc.LetterboxdPageError):
                fetch()
    assert caplog.text.count("full body of that last page") == 1
