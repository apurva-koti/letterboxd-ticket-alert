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
class TrackedFilm:
    """The full persisted state for one film - a joined view of the films,
    fandango_matches, and ticket_status rows."""

    film: Film
    anchor_date: date | None
    director: str | None
    runtime: int | None
    fandango: FandangoListing | None
    poster_url: str | None
    excluded_reason: str | None
    status: TicketStatus
    on_sale_theaters: list[str]
    showtimes_only_theaters: list[str]
    alerted_theaters: list[str]
    tier: Tier | None
    next_check_at: datetime | None
    alerted_at: datetime | None
    notified_at: datetime | None
    checked_at: datetime | None


@dataclass
class PendingNotification:
    film: Film
    alerted_theaters: list[str]
    ticket_url: str | None
    poster_url: str | None = None


@dataclass
class Config:
    """Account-level settings, stored in the DB so they're editable from the
    config web form instead of baked in at deploy time. Single-user for now
    - see repository.py's config table."""

    letterboxd_username: str | None
    zip_code: str | None
    hype_list_url: str | None
    blacklisted_theaters: frozenset[str]


@dataclass
class RunResult:
    added: list[Film] = field(default_factory=list)
    removed: list[Film] = field(default_factory=list)
    checked: list[Film] = field(default_factory=list)
    alerted: list[Film] = field(default_factory=list)
