"""Shared test fixtures. Fixture HTML files under tests/fixtures/ are real,
saved captures from actual fandango.com / letterboxd.com / boxofficemojo.com
pages - not hand-written approximations - so parser tests exercise the real
markup shape, not an idealized one.

Factory functions below build real model instances with sensible defaults, so
tests construct actual objects instead of monkeypatching a function purely to
produce a particular shape - only the true I/O boundary (requests.get, or a
module's own top-level search/match/status function) still needs a mock."""

from datetime import datetime, timezone
from pathlib import Path

from models import CheckOutcome, FandangoCandidate, FandangoMatch, Film, LetterboxdInfo, TicketStatus, TicketStatusResult

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def make_film(**overrides):
    defaults = {"slug": "digger-2026", "title": "Digger", "year": 2026, "url": "https://letterboxd.com/film/digger-2026/"}
    return Film(**{**defaults, **overrides})


def make_letterboxd_info(**overrides):
    defaults = {"director": None, "release_date": None, "genres": []}
    return LetterboxdInfo(**{**defaults, **overrides})


def make_fandango_candidate(**overrides):
    defaults = {
        "fandango_id": "245150",
        "slug": "digger-2026-245150",
        "title": "Digger",
        "year": 2026,
        "status": None,
        "url": "https://www.fandango.com/digger-2026-245150/movie-overview",
    }
    return FandangoCandidate(**{**defaults, **overrides})


def make_ticket_status_result(**overrides):
    defaults = {"date": "2026-09-25", "status": "on_sale", "on_sale_theaters": ["Alamo Drafthouse"], "showtimes_only_theaters": []}
    return TicketStatusResult(**{**defaults, **overrides})


def make_match(**overrides):
    defaults = {
        "letterboxd_slug": "digger-2026",
        "fandango_id": "245150",
        "fandango_slug": "digger-2026-245150",
        "matched_title": "Digger",
        "matched_year": 2026,
        "release_date": None,
        "release_date_source": None,
        "excluded_reason": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    return FandangoMatch(**{**defaults, **overrides})


def make_ticket_status(**overrides):
    defaults = {
        "letterboxd_slug": "digger-2026",
        "status": "none",
        "status_date": None,
        "on_sale_theaters": [],
        "showtimes_only_theaters": [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "alerted_at": None,
        "tier": None,
        "next_check_at": None,
    }
    return TicketStatus(**{**defaults, **overrides})


def make_check_outcome(**overrides):
    defaults = {
        "film": make_film(),
        "match": make_match(),
        "result": None,
        "previous_status": "none",
        "new_status": "none",
        "should_alert": False,
        "tier": None,
    }
    return CheckOutcome(**{**defaults, **overrides})


def fixture_html(name):
    return (FIXTURES_DIR / name).read_text()


class FakeResponse:
    """Stands in for a requests.Response in tests - just enough surface
    (.text, .json(), .raise_for_status(), .status_code) for the code under
    test, without a real HTTP call."""

    def __init__(self, text=None, json_data=None, status_code=200):
        self.text = text
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json


def fake_html_response(fixture_name, status_code=200):
    return FakeResponse(text=fixture_html(fixture_name), status_code=status_code)


class FakeSession:
    """A minimal stand-in for a requests.Session (or the module-level
    `requests` object) whose .get() is driven by a routing function, so tests
    can return different canned responses per URL without real network."""

    def __init__(self, router):
        self.router = router
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        return self.router(url, params)
