"""Shared test fixtures. HTML fixtures under tests/fixtures/ are real, saved
captures from actual fandango.com / letterboxd.com pages, not hand-written
approximations."""

import json
from datetime import date
from pathlib import Path

import requests

from domain import FandangoListing, Film, LetterboxdRelease, ShowtimeCheck, TicketStatus

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def make_film(**overrides):
    defaults = {"slug": "digger-2026", "title": "Digger", "year": 2026, "url": "https://letterboxd.com/film/digger-2026/"}
    return Film(**{**defaults, **overrides})


def make_release(**overrides):
    defaults = {"director": None, "genres": [], "poster_url": None, "us_dates": [], "runtime": None}
    return LetterboxdRelease(**{**defaults, **overrides})


def make_listing(**overrides):
    defaults = {
        "fandango_id": "245150",
        "slug": "digger-2026-245150",
        "title": "Digger",
        "year": 2026,
        "url": "https://www.fandango.com/digger-2026-245150/movie-overview",
        "director": None,
        "runtime": None,
    }
    return FandangoListing(**{**defaults, **overrides})


def make_showtime_check(**overrides):
    defaults = {
        "checked_date": date(2026, 9, 25),
        "status": TicketStatus.ON_SALE,
        "on_sale_theaters": ["Alamo Drafthouse"],
        "showtimes_only_theaters": [],
    }
    return ShowtimeCheck(**{**defaults, **overrides})


def fixture_html(name):
    return (FIXTURES_DIR / name).read_text()


class FakeResponse:
    def __init__(self, text=None, json_data=None, status_code=200):
        self.text = text
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        """Matches the real requests.Response contract: given explicit
        json_data, return it; otherwise actually parse .text, raising
        ValueError on invalid JSON like the real thing would - needed to
        simulate a response that looks like JSON was expected but wasn't
        (e.g. an HTML block page)."""
        if self._json is not None:
            return self._json
        return json.loads(self.text)


def fake_html_response(fixture_name, status_code=200):
    return FakeResponse(text=fixture_html(fixture_name), status_code=status_code)
