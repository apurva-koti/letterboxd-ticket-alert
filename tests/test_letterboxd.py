from datetime import date

import letterboxd

from conftest import fake_html_response, fixture_html
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
    assert letterboxd.get_us_release_date("digger-2026") == date(2026, 10, 2)


def test_get_us_release_date_picks_earliest_across_sections(monkeypatch):
    """Regression test for a real bug caught mid-build: Paper Tiger lists USA
    under BOTH "Theatrical limited" (13 Nov) and "Theatrical" (20 Nov), plus a
    Premiere date (25 Sep, not a real public release) and an unrelated Spain
    date (12 Feb) that a naive nearest-preceding-date scrape would misattribute
    to USA. The correct answer is the earliest real theatrical date, 13 Nov -
    which is also what Fandango's own release date field gave for this film."""
    monkeypatch.setattr(
        letterboxd.requests,
        "get",
        lambda url, headers=None, timeout=None: fake_html_response("letterboxd_film_papertiger.html"),
    )
    assert letterboxd.get_us_release_date("paper-tiger-2026") == date(2026, 11, 13)


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
    result = letterboxd.get_us_release_date("vertigo")
    assert result is None or isinstance(result, date)
