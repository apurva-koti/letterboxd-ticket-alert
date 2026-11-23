"""Typed shapes shared across the tracking pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class Tier(StrEnum):
    MUST_WATCH = "must_watch"
    HOT = "hot"
    RECENT = "recent"
    FAR_FUTURE = "far_future"
    UNKNOWN = "unknown"
    RETIRED = "retired"


class TicketStatus(StrEnum):
    NONE = "none"
    SHOWTIMES_ANNOUNCED = "showtimes_announced"
    ON_SALE = "on_sale"


@dataclass
class Film:
    slug: str
    title: str
    year: int | None
    url: str | None


@dataclass
class LetterboxdRelease:
    director: str | None
    genres: list[str]
    poster_url: str | None
    us_dates: list[date]
    runtime: int | None = None


@dataclass
class FandangoListing:
    fandango_id: str
    slug: str
    title: str
    year: int | None
    url: str
    director: str | None = None
    runtime: int | None = None


@dataclass
class ShowtimeCheck:
    checked_date: date
    status: TicketStatus
    on_sale_theaters: list[str]
    showtimes_only_theaters: list[str]


@dataclass
class ListingResult:
    """One matching Fandango listing and what its latest check found. A film
    can have several (an original listing plus a separate anniversary
    re-release listing, say), and each is stored and emailed on its own."""

    fandango_id: str
    slug: str
    title: str
    year: int | None
    dates: list[date] = field(default_factory=list)  # Fandango's showtime calendar for this listing
    on_sale_theaters: list[str] = field(default_factory=list)
    showtimes_only_theaters: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://www.fandango.com/{self.slug}/movie-overview"


@dataclass
class AlertedListing:
    """What an alert told you about one listing, frozen when the alert fired -
    the email's section for that listing."""

    slug: str
    title: str
    dates: list[date]
    theaters: list[str]

    @property
    def url(self) -> str | None:
        return f"https://www.fandango.com/{self.slug}/movie-overview" if self.slug else None


@dataclass
class AlertContext:
    """Why an alert fired and what can still trigger another - so the email
    can say what to expect next. Frozen with the alert."""

    kind: str  # "first" (just went on sale) or "preferred" (a preferred theater joined later)
    preferred_on_sale: list[str] = field(default_factory=list)
    preferred_missing: list[str] = field(default_factory=list)  # preferred theaters without tickets yet
    previous_theaters: list[str] = field(default_factory=list)  # "preferred" only: what the earlier email listed
    previous_alerted_at: datetime | None = None


@dataclass
class TrackedFilm:
    """The full persisted state for one film - a joined view of the films,
    fandango_matches, and ticket_status rows."""

    film: Film
    anchor_date: date | None
    director: str | None
    runtime: int | None
    listings: list[ListingResult]
    poster_url: str | None
    excluded_reason: str | None
    status: TicketStatus
    on_sale_theaters: list[str]
    showtimes_only_theaters: list[str]
    alerted_theaters: list[str]
    alerted_listings: list[AlertedListing]
    tier: Tier | None
    next_check_at: datetime | None
    alerted_at: datetime | None
    notified_at: datetime | None
    checked_at: datetime | None


@dataclass
class PendingNotification:
    film: Film
    listings: list[AlertedListing]
    poster_url: str | None = None
    context: AlertContext | None = None

    @property
    def alerted_theaters(self) -> list[str]:
        return sorted({t for listing in self.listings for t in listing.theaters})


@dataclass
class Config:
    """Account-level settings, stored in the DB so they're editable from the
    config web form instead of baked in at deploy time. Single-user for now
    - see repository.py's config table."""

    letterboxd_username: str | None
    zip_code: str | None
    hype_list_url: str | None
    blacklisted_theaters: frozenset[str]
    preferred_theaters: frozenset[str] = frozenset()


@dataclass
class RunResult:
    added: list[Film] = field(default_factory=list)
    removed: list[Film] = field(default_factory=list)
    checked: list[Film] = field(default_factory=list)
    alerted: list[Film] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # "<title>: <error>" per film whose check raised
    anomalies: list[str] = field(default_factory=list)  # unexpected Fandango data seen while checking
    fandango_unavailable: bool = False
