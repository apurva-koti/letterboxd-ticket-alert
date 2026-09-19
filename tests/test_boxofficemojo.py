from datetime import date

import boxofficemojo

from conftest import fake_html_response, fixture_html


def _route(url, params=None, headers=None, timeout=None):
    if "/search/" in url:
        return fake_html_response("boxofficemojo_search.html")
    if "/title/" in url:
        return fake_html_response("boxofficemojo_title.html")
    raise AssertionError(f"unexpected URL: {url}")


def test_search_title_finds_imdb_id(monkeypatch):
    monkeypatch.setattr(boxofficemojo.requests, "get", _route)
    candidates = boxofficemojo.search_title("coyote vs acme")
    assert any(c["imdb_id"] == "tt1756855" for c in candidates)


def test_get_domestic_release_date_parses_domestic_row():
    html = fixture_html("boxofficemojo_title.html")
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    domestic_link = next(link for link in soup.select("td a") if link.get_text(strip=True) == "Domestic")
    date_cell = domestic_link.find_parent("td").find_next_sibling("td")
    assert date_cell.get_text(strip=True) == "Aug 28, 2026"


def test_find_release_date_end_to_end(monkeypatch):
    monkeypatch.setattr(boxofficemojo.requests, "get", _route)
    assert boxofficemojo.find_release_date("Coyote vs. Acme") == date(2026, 8, 28)


def test_find_release_date_rejects_low_similarity_match(monkeypatch):
    monkeypatch.setattr(
        boxofficemojo,
        "search_title",
        lambda title: [{"imdb_id": "tt0000000", "title": "A Completely Unrelated Film"}],
    )
    assert boxofficemojo.find_release_date("Coyote vs. Acme") is None


def test_find_release_date_none_when_no_candidates(monkeypatch):
    monkeypatch.setattr(boxofficemojo, "search_title", lambda title: [])
    assert boxofficemojo.find_release_date("Some Obscure Title") is None
