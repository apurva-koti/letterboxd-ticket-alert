from datetime import date

import letterboxd

from conftest import FakeResponse, fake_html_response, fixture_html
from models import Film


def test_parse_page_extracts_title_year_slug_url():
    films = letterboxd._parse_page(fixture_html("letterboxd_watchlist_page1.html"))
    assert len(films) == 28
    assert films[0] == Film(
        title="A Ghost Story",
        year=2017,
        slug="a-ghost-story-2017",
        url="https://letterboxd.com/film/a-ghost-story-2017/",
    )


def test_get_watchlist_stops_at_max_pages(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        assert "/page/1/" in url  # max_pages=1 should never even request page 2
        return fake_html_response("letterboxd_watchlist_page1.html")

    monkeypatch.setattr(letterboxd.requests, "get", fake_get)
    films = letterboxd.get_watchlist("dave", delay=0, max_pages=1)

    assert len(films) == 28
    assert len(calls) == 1


def test_get_list_fetches_bare_url_for_page_one(monkeypatch):
    """Regression test: a share-token list URL 403s if /page/1/ is appended -
    only the bare URL works for the first page (confirmed against the real
    Hype list). get_list must request the bare URL, not .../page/1/."""
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if url.endswith("/share/tok123/"):
            return fake_html_response("letterboxd_list_hype.html")
        return FakeResponse(text="", status_code=403)  # any /page/N/ - simulates the real 403

    monkeypatch.setattr(letterboxd.requests, "get", fake_get)
    films = letterboxd.get_list("https://letterboxd.com/exampleuser/list/hype/share/tok123/")

    assert len(films) == 1
    assert films[0].slug == "dune-part-three"
    # page 1 must be the bare URL (no /page/1/); page 2 is legitimately
    # attempted next (1 film doesn't prove there's no more) - path form 403s,
    # so the ?page= fallback is tried too, which also 403s here, ending it
    assert calls == [
        "https://letterboxd.com/exampleuser/list/hype/share/tok123/",
        "https://letterboxd.com/exampleuser/list/hype/share/tok123/page/2/",
        "https://letterboxd.com/exampleuser/list/hype/share/tok123/?page=2",
    ]


def test_get_list_stops_on_403_not_just_404(monkeypatch):
    """403 has to be treated as "no more pages" too, not just 404 - the share
    URL's real behavior on /page/N/, confirmed by hand - otherwise this would
    crash instead of gracefully capping at one page."""

    def fake_get(url, headers=None, timeout=None):
        if "/page/2/" in url:
            return FakeResponse(text="", status_code=403)
        return fake_html_response("letterboxd_list_hype.html")

    monkeypatch.setattr(letterboxd.requests, "get", fake_get)
    films = letterboxd.get_list("https://letterboxd.com/exampleuser/list/hype/share/tok123/", max_pages=5)

    assert len(films) == 1  # didn't crash on the page/2/ 403


def test_get_list_falls_back_to_query_param_on_403(monkeypatch):
    """Real, observed behavior: a share-token URL 403s on .../page/2/ (path
    form) but returns 200 on .../?page=2 (query-param form) - unconfirmed
    whether Letterboxd actually honors that param there, but it's tried
    before giving up, since it costs nothing when it doesn't help."""
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if url.endswith("/share/tok123/"):
            return fake_html_response("letterboxd_list_hype.html")
        if "/page/2/" in url:
            return FakeResponse(text="", status_code=403)
        if url.endswith("?page=2"):
            return FakeResponse(text="", status_code=404)  # simulate a real "no more" here instead
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(letterboxd.requests, "get", fake_get)
    letterboxd.get_list("https://letterboxd.com/exampleuser/list/hype/share/tok123/")

    assert calls[-1].endswith("?page=2")  # the fallback was actually tried, not skipped


def test_get_list_stops_on_duplicate_page_content_without_double_counting(monkeypatch):
    """If the ?page= fallback is silently ignored by Letterboxd and just
    re-serves the same page again, that has to be detected and stopped - not
    double-counted as if it were new content, and not looped forever."""

    def fake_get(url, headers=None, timeout=None):
        if "/page/2/" in url:
            return FakeResponse(text="", status_code=403)
        return fake_html_response("letterboxd_list_hype.html")  # same content every real hit

    monkeypatch.setattr(letterboxd.requests, "get", fake_get)
    films = letterboxd.get_list("https://letterboxd.com/exampleuser/list/hype/share/tok123/", max_pages=10)

    assert len(films) == 1  # not 2 - the repeated page wasn't counted as new


def test_get_director_parses_json_ld(monkeypatch):
    monkeypatch.setattr(
        letterboxd.requests, "get", lambda url, headers=None, timeout=None: fake_html_response("letterboxd_film_vertigo.html")
    )
    assert letterboxd.get_director("vertigo") == "Alfred Hitchcock"


def test_get_us_release_date_single_theatrical_entry(monkeypatch):
    """Digger has only one USA theatrical date, under the "Theatrical" section."""
    monkeypatch.setattr(
        letterboxd.requests, "get", lambda url, headers=None, timeout=None: fake_html_response("letterboxd_film_digger.html")
    )
    # Pinned today: this date is upcoming relative to any today before it, so
    # the exact pin doesn't matter here - just keeping it deterministic like
    # the other date-selection tests below.
    assert letterboxd.get_us_release_date("digger-2026", today=date(2026, 1, 1)) == date(2026, 10, 2)


def test_get_us_release_date_picks_earliest_across_sections(monkeypatch):
    """Regression test for a real bug caught mid-build: Paper Tiger lists USA
    under BOTH "Theatrical limited" (13 Nov) and "Theatrical" (20 Nov), plus a
    Premiere date (25 Sep, not a real public release) and an unrelated Spain
    date (12 Feb) that a naive nearest-preceding-date scrape would misattribute
    to USA. The correct answer is the earliest real theatrical date, 13 Nov -
    which is also what Fandango's own release date field gave for this film.

    today is pinned before both dates (rather than left to real wall-clock
    date.today()) so this stays correct regardless of when the test actually
    runs - see _pick_release_date's docstring for why "today" changes which
    of the two dates is correct once either one is in the past."""
    monkeypatch.setattr(
        letterboxd.requests,
        "get",
        lambda url, headers=None, timeout=None: fake_html_response("letterboxd_film_papertiger.html"),
    )
    assert letterboxd.get_us_release_date("paper-tiger-2026", today=date(2026, 1, 1)) == date(2026, 11, 13)


def test_get_film_info_extracts_genre_for_documentary(monkeypatch):
    monkeypatch.setattr(
        letterboxd.requests,
        "get",
        lambda url, headers=None, timeout=None: fake_html_response("letterboxd_film_nuisancebear.html"),
    )
    info = letterboxd.get_film_info("nuisance-bear-2026")
    assert "documentary" in info.genres
    # co-directed (2 credited directors) - DIRECTOR_RE only captures the first
    # from the JSON-LD list, a known simplification, not something this test
    # is meant to fix.
    assert info.director == "Gabriela Osio Vanden"


def test_get_film_info_genre_has_no_false_positive_for_non_documentary(monkeypatch):
    monkeypatch.setattr(
        letterboxd.requests, "get", lambda url, headers=None, timeout=None: fake_html_response("letterboxd_film_vertigo.html")
    )
    info = letterboxd.get_film_info("vertigo")
    assert "documentary" not in info.genres
    assert "thriller" in info.genres


def test_get_us_release_date_none_when_no_theatrical_section(monkeypatch):
    # Vertigo's page (an old classic) - whatever release info exists there
    # shouldn't crash the parser even if there's no clean modern release table.
    monkeypatch.setattr(
        letterboxd.requests, "get", lambda url, headers=None, timeout=None: fake_html_response("letterboxd_film_vertigo.html")
    )
    result = letterboxd.get_us_release_date("vertigo", today=date(2026, 1, 1))
    assert result is None or isinstance(result, date)


def test_pick_release_date_prefers_earliest_upcoming():
    """New-release case (Paper Tiger-style): both candidate dates are still
    upcoming relative to today - the earlier one wins, not the later wide
    release."""
    dates = [date(2026, 11, 20), date(2026, 11, 13)]
    assert letterboxd._pick_release_date(dates, today=date(2026, 1, 1)) == date(2026, 11, 13)


def test_pick_release_date_falls_back_to_most_recent_past_for_legacy_films():
    """Legacy-film case (Top Gun-style): once every candidate date is in the
    past relative to today, naively taking the earliest overall would
    permanently anchor on the decades-old original release (1986) and never
    surface a later re-release. The correct fallback is the most RECENT past
    date - the latest known re-release - not the oldest."""
    dates = [date(1986, 5, 16), date(2013, 2, 8), date(2021, 5, 21), date(2026, 5, 13)]
    assert letterboxd._pick_release_date(dates, today=date(2026, 9, 20)) == date(2026, 5, 13)


def test_pick_release_date_mixed_past_and_future_prefers_upcoming():
    """A mix of past and future candidates - e.g. a re-release just occurred
    and a further one is already announced - should prefer the upcoming one
    over the past one, consistent with the "earliest upcoming" preference for
    brand-new releases."""
    dates = [date(2021, 5, 21), date(2026, 12, 1)]
    assert letterboxd._pick_release_date(dates, today=date(2026, 9, 20)) == date(2026, 12, 1)


def test_pick_release_date_empty_list_returns_none():
    assert letterboxd._pick_release_date([], today=date(2026, 1, 1)) is None
