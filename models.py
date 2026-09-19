"""Shared data shapes used across modules.

Dataclasses instead of raw dicts for two reasons: field access is checked (a
typo'd key becomes an AttributeError at the call site, not a silent None three
functions later), and tests can construct a real instance directly instead of
monkeypatching a function purely to make it return a particular dict shape.
"""

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Film:
    """One entry from a Letterboxd watchlist."""

    slug: str
    title: str
    year: int | None
    url: str | None


@dataclass
class LetterboxdInfo:
    """Everything pulled from a single film's Letterboxd page in one fetch."""

    director: str | None
    release_date: date | None
    genres: list[str] = field(default_factory=list)
    poster_url: str | None = None


@dataclass
class FandangoCandidate:
    """One Fandango search result, or a confirmed match."""

    fandango_id: str
    slug: str
    title: str
    year: int | None
    status: str | None
    url: str


@dataclass
class TicketStatusResult:
    """The outcome of checking Fandango showtimes near a zip code."""

    date: str
    status: str
    on_sale_theaters: list[str]
    showtimes_only_theaters: list[str]


@dataclass
class FandangoMatch:
    """A film's persisted Fandango-matching state (a fandango_matches row)."""

    letterboxd_slug: str
    fandango_id: str | None
    fandango_slug: str | None
    matched_title: str | None
    matched_year: int | None
    release_date: str | None  # ISO date string, as stored
    release_date_source: str | None
    excluded_reason: str | None
    checked_at: str
    poster_url: str | None = None


@dataclass
class TicketStatus:
    """A film's persisted ticket-status state (a ticket_status row)."""

    letterboxd_slug: str
    status: str
    status_date: str | None
    on_sale_theaters: list[str]
    showtimes_only_theaters: list[str]
    updated_at: str
    alerted_at: str | None
    tier: str | None
    next_check_at: str | None
    notified_at: str | None = None


@dataclass
class PendingNotification:
    """A film currently on_sale that hasn't been successfully notified about
    yet - either a fresh transition this run, or a leftover from an earlier
    run whose send failed and needs retrying."""

    film: Film
    on_sale_theaters: list[str]
    ticket_url: str | None  # Fandango's own page - where tickets can actually be bought
    poster_url: str | None = None


@dataclass
class CheckOutcome:
    """Result of ticket_checker.check_film for one film."""

    film: Film
    match: FandangoMatch
    result: TicketStatusResult | None
    previous_status: str
    new_status: str
    should_alert: bool
    tier: str | None = None
