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


@dataclass
class FandangoListing:
    fandango_id: str
    slug: str
    title: str
    year: int | None
    url: str
    release_date: date | None = None
    director: str | None = None


@dataclass
class ShowtimeCheck:
    checked_date: date
    status: TicketStatus
    on_sale_theaters: list[str]
    showtimes_only_theaters: list[str]


@dataclass
class TrackedFilm:
    """The full persisted state for one film - a joined view of the films,
    fandango_matches, and ticket_status rows."""

    film: Film
    anchor_date: date | None
    fandango: FandangoListing | None
    poster_url: str | None
    excluded_reason: str | None
    status: TicketStatus
    on_sale_theaters: list[str]
    showtimes_only_theaters: list[str]
    tier: Tier | None
    next_check_at: datetime | None
    alerted_at: datetime | None
    notified_at: datetime | None
    checked_at: datetime | None


@dataclass
class PendingNotification:
    film: Film
    on_sale_theaters: list[str]
    ticket_url: str | None
    poster_url: str | None = None


@dataclass
class RunResult:
    added: list[Film] = field(default_factory=list)
    removed: list[Film] = field(default_factory=list)
    checked: list[Film] = field(default_factory=list)
    alerted: list[Film] = field(default_factory=list)
