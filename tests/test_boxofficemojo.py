from datetime import date

import requests

import boxofficemojo

from conftest import FakeResponse, fake_html_response, fixture_html


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


def test_get_with_retry_retries_a_server_error(monkeypatch):
    monkeypatch.setattr(boxofficemojo.time, "sleep", lambda s: None)
    responses = iter([FakeResponse(status_code=500), FakeResponse(text="ok", status_code=200)])
    monkeypatch.setattr(boxofficemojo.requests, "get", lambda url, **kw: next(responses))

    resp = boxofficemojo._get_with_retry("https://example.test/x")
    assert resp.status_code == 200


def test_get_with_retry_retries_a_connection_exception(monkeypatch):
    monkeypatch.setattr(boxofficemojo.time, "sleep", lambda s: None)
    calls = []

    def fake_get(url, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise requests.exceptions.ConnectionError("refused")
        return FakeResponse(text="ok", status_code=200)

    monkeypatch.setattr(boxofficemojo.requests, "get", fake_get)

    resp = boxofficemojo._get_with_retry("https://example.test/x")
    assert resp.status_code == 200
    assert len(calls) == 2


def test_get_with_retry_raises_when_every_attempt_fails_to_connect(monkeypatch):
    monkeypatch.setattr(boxofficemojo.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        boxofficemojo.requests, "get", lambda url, **kw: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("timed out"))
    )

    try:
        boxofficemojo._get_with_retry("https://example.test/x")
        assert False, "expected ReadTimeout to propagate"
    except requests.exceptions.ReadTimeout:
        pass
