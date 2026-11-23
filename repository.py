"""SQLite persistence: watchlist films, their Fandango binding, and ticket status."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone

from domain import AlertContext, AlertedListing, Config, Film, ListingResult, PendingNotification, Tier, TicketStatus, TrackedFilm
from health import HealthState

DB_PATH = "state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS films (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    year INTEGER,
    url TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

-- Film-level data from matching. Its fandango_* / matched_* columns are
-- from when a film had a single listing - no longer written; only read to
-- fill in an alert saved before film_listings existed.
CREATE TABLE IF NOT EXISTS fandango_matches (
    letterboxd_slug TEXT PRIMARY KEY REFERENCES films(slug),
    fandango_id TEXT,
    fandango_slug TEXT,
    matched_title TEXT,
    matched_year INTEGER,
    release_date TEXT,
    release_date_source TEXT,
    director TEXT,
    runtime INTEGER,
    excluded_reason TEXT,
    checked_at TEXT NOT NULL,
    poster_url TEXT
);

-- Every Fandango listing matched to a film, with what its latest check
-- found. Replaced wholesale on every check.
CREATE TABLE IF NOT EXISTS film_listings (
    letterboxd_slug TEXT NOT NULL REFERENCES films(slug),
    fandango_slug TEXT NOT NULL,
    fandango_id TEXT NOT NULL,
    title TEXT,
    year INTEGER,
    dates TEXT,
    on_sale_theaters TEXT,
    showtimes_only_theaters TEXT,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (letterboxd_slug, fandango_slug)
);

CREATE TABLE IF NOT EXISTS ticket_status (
    letterboxd_slug TEXT PRIMARY KEY REFERENCES films(slug),
    status TEXT NOT NULL,
    status_date TEXT,
    on_sale_theaters TEXT,
    showtimes_only_theaters TEXT,
    updated_at TEXT NOT NULL,
    alerted_at TEXT,
    alerted_theaters TEXT,
    alerted_listings TEXT,
    alert_context TEXT,
    tier TEXT,
    next_check_at TEXT,
    notified_at TEXT
);

-- Single row (id=1) for now - see domain.Config's docstring for the
-- multi-user plan this is step one of.
CREATE TABLE IF NOT EXISTS config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    letterboxd_username TEXT,
    zip_code TEXT,
    hype_list_url TEXT,
    blacklisted_theaters TEXT,
    preferred_theaters TEXT
);

-- Single row (id=1): whether Fandango checking is currently broken - see health.py.
CREATE TABLE IF NOT EXISTS health (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    failing_since TEXT,
    last_problem TEXT,
    alerted_at TEXT
);
"""

# CREATE TABLE IF NOT EXISTS above only applies to brand-new DBs - an
# existing one needs columns added after the fact explicitly.
MIGRATIONS = [
    "ALTER TABLE fandango_matches ADD COLUMN director TEXT",
    "ALTER TABLE fandango_matches ADD COLUMN runtime INTEGER",
    "ALTER TABLE ticket_status ADD COLUMN alerted_theaters TEXT",
    "ALTER TABLE config ADD COLUMN preferred_theaters TEXT",
    "ALTER TABLE ticket_status ADD COLUMN alerted_listings TEXT",
    "ALTER TABLE ticket_status ADD COLUMN alert_context TEXT",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _dates_json(dates: list[date]) -> str:
    return json.dumps([d.isoformat() for d in dates])


def _parse_dates(raw: str | None) -> list[date]:
    return [date.fromisoformat(d) for d in json.loads(raw)] if raw else []


def _alerted_listings_json(listings: list[AlertedListing]) -> str:
    return json.dumps(
        [{"slug": l.slug, "title": l.title, "dates": [d.isoformat() for d in l.dates], "theaters": l.theaters} for l in listings]
    )


def _alert_context_json(context: AlertContext) -> str:
    return json.dumps(
        {
            "kind": context.kind,
            "preferred_on_sale": context.preferred_on_sale,
            "preferred_missing": context.preferred_missing,
            "previous_theaters": context.previous_theaters,
            "previous_alerted_at": context.previous_alerted_at.isoformat() if context.previous_alerted_at else None,
        }
    )


def _parse_alert_context(raw: str | None) -> AlertContext | None:
    if not raw:
        return None
    d = json.loads(raw)
    return AlertContext(
        kind=d["kind"],
        preferred_on_sale=d["preferred_on_sale"],
        preferred_missing=d["preferred_missing"],
        previous_theaters=d["previous_theaters"],
        previous_alerted_at=_parse_dt(d["previous_alerted_at"]),
    )


def _parse_alerted_listings(raw: str | None) -> list[AlertedListing]:
    if not raw:
        return []
    return [
        AlertedListing(slug=l["slug"], title=l["title"], dates=[date.fromisoformat(d) for d in l["dates"]], theaters=l["theaters"])
        for l in json.loads(raw)
    ]


class FilmRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def connect(cls, db_path: str = DB_PATH) -> "FilmRepository":
        # check_same_thread=False: the config web form (FastAPI) runs sync
        # endpoints in a thread pool, not the thread that opened the
        # connection - safe here since SQLite serializes file access itself
        # and this is a low-frequency, single-process, non-concurrent-write
        # workload either way.
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        for migration in MIGRATIONS:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise
        conn.commit()
        return cls(conn)

    def sync_watchlist(self, films: list[Film]) -> tuple[list[Film], list[Film]]:
        now = _now()
        current = {f.slug: f for f in films}
        existing_rows = {row["slug"]: row for row in self.conn.execute("SELECT * FROM films")}

        added = [f for slug, f in current.items() if slug not in existing_rows]
        for f in current.values():
            self.conn.execute(
                """
                INSERT INTO films (slug, title, year, url, first_seen, last_seen)
                VALUES (:slug, :title, :year, :url, :now, :now)
                ON CONFLICT(slug) DO UPDATE SET
                    title = excluded.title, year = excluded.year,
                    url = excluded.url, last_seen = excluded.last_seen
                """,
                {"slug": f.slug, "title": f.title, "year": f.year, "url": f.url, "now": now},
            )

        removed = [
            Film(slug=row["slug"], title=row["title"], year=row["year"], url=row["url"])
            for slug, row in existing_rows.items()
            if slug not in current
        ]
        for f in removed:
            self.conn.execute("DELETE FROM films WHERE slug = ?", (f.slug,))
            self.conn.execute("DELETE FROM fandango_matches WHERE letterboxd_slug = ?", (f.slug,))
            self.conn.execute("DELETE FROM film_listings WHERE letterboxd_slug = ?", (f.slug,))
            self.conn.execute("DELETE FROM ticket_status WHERE letterboxd_slug = ?", (f.slug,))

        self.conn.commit()
        return added, removed

    def all_films(self) -> list[Film]:
        return [
            Film(slug=row["slug"], title=row["title"], year=row["year"], url=row["url"])
            for row in self.conn.execute("SELECT * FROM films")
        ]

    def get(self, slug: str) -> TrackedFilm | None:
        film_row = self.conn.execute("SELECT * FROM films WHERE slug = ?", (slug,)).fetchone()
        if not film_row:
            return None
        film = Film(slug=film_row["slug"], title=film_row["title"], year=film_row["year"], url=film_row["url"])

        match_row = self.conn.execute("SELECT * FROM fandango_matches WHERE letterboxd_slug = ?", (slug,)).fetchone()
        status_row = self.conn.execute("SELECT * FROM ticket_status WHERE letterboxd_slug = ?", (slug,)).fetchone()

        anchor_date = None
        director = None
        runtime = None
        excluded_reason = None
        poster_url = None
        checked_at = None
        if match_row:
            anchor_date = _parse_date(match_row["release_date"])
            director = match_row["director"]
            runtime = match_row["runtime"]
            excluded_reason = match_row["excluded_reason"]
            poster_url = match_row["poster_url"]
            checked_at = _parse_dt(match_row["checked_at"])

        status = TicketStatus.NONE
        on_sale_theaters: list[str] = []
        showtimes_only_theaters: list[str] = []
        alerted_theaters: list[str] = []
        alerted_listings: list[AlertedListing] = []
        tier = None
        next_check_at = None
        alerted_at = None
        notified_at = None
        if status_row:
            status = TicketStatus(status_row["status"])
            on_sale_theaters = json.loads(status_row["on_sale_theaters"]) if status_row["on_sale_theaters"] else []
            showtimes_only_theaters = (
                json.loads(status_row["showtimes_only_theaters"]) if status_row["showtimes_only_theaters"] else []
            )
            alerted_theaters = json.loads(status_row["alerted_theaters"]) if status_row["alerted_theaters"] else []
            alerted_listings = _parse_alerted_listings(status_row["alerted_listings"])
            tier = Tier(status_row["tier"]) if status_row["tier"] else None
            next_check_at = _parse_dt(status_row["next_check_at"])
            alerted_at = _parse_dt(status_row["alerted_at"])
            notified_at = _parse_dt(status_row["notified_at"])

        return TrackedFilm(
            film=film,
            anchor_date=anchor_date,
            director=director,
            runtime=runtime,
            listings=self.get_listings(slug),
            poster_url=poster_url,
            excluded_reason=excluded_reason,
            status=status,
            on_sale_theaters=on_sale_theaters,
            showtimes_only_theaters=showtimes_only_theaters,
            alerted_theaters=alerted_theaters,
            alerted_listings=alerted_listings,
            tier=tier,
            next_check_at=next_check_at,
            alerted_at=alerted_at,
            notified_at=notified_at,
            checked_at=checked_at,
        )

    def get_listings(self, slug: str) -> list[ListingResult]:
        rows = self.conn.execute(
            "SELECT * FROM film_listings WHERE letterboxd_slug = ? ORDER BY rowid", (slug,)
        ).fetchall()
        return [
            ListingResult(
                fandango_id=r["fandango_id"],
                slug=r["fandango_slug"],
                title=r["title"],
                year=r["year"],
                dates=_parse_dates(r["dates"]),
                on_sale_theaters=json.loads(r["on_sale_theaters"]) if r["on_sale_theaters"] else [],
                showtimes_only_theaters=json.loads(r["showtimes_only_theaters"]) if r["showtimes_only_theaters"] else [],
            )
            for r in rows
        ]

    def save_listings(self, slug: str, listings: list[ListingResult]) -> None:
        """Replaces the film's listings with this check's - a listing that no
        longer matches simply disappears."""
        now = _now()
        self.conn.execute("DELETE FROM film_listings WHERE letterboxd_slug = ?", (slug,))
        for l in listings:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO film_listings
                    (letterboxd_slug, fandango_slug, fandango_id, title, year, dates,
                     on_sale_theaters, showtimes_only_theaters, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    slug, l.slug, l.fandango_id, l.title, l.year, _dates_json(l.dates),
                    json.dumps(l.on_sale_theaters), json.dumps(l.showtimes_only_theaters), now,
                ),
            )
        self.conn.commit()

    def save_match(
        self,
        slug: str,
        anchor_date: date | None,
        director: str | None = None,
        runtime: int | None = None,
        excluded_reason: str | None = None,
        poster_url: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO fandango_matches
                (letterboxd_slug, release_date, release_date_source, director, runtime, excluded_reason, poster_url, checked_at)
            VALUES (:slug, :date, :source, :director, :runtime, :excluded, :poster, :now)
            ON CONFLICT(letterboxd_slug) DO UPDATE SET
                release_date = excluded.release_date, release_date_source = excluded.release_date_source,
                director = excluded.director, runtime = excluded.runtime, excluded_reason = excluded.excluded_reason,
                poster_url = excluded.poster_url, checked_at = excluded.checked_at
            """,
            {
                "slug": slug,
                "date": anchor_date.isoformat() if anchor_date else None,
                "source": "letterboxd" if anchor_date else None,
                "director": director,
                "runtime": runtime,
                "excluded": excluded_reason,
                "poster": poster_url,
                "now": _now(),
            },
        )
        self.conn.commit()

    def save_status(
        self,
        slug: str,
        status: TicketStatus,
        status_date: date | None,
        on_sale_theaters: list[str],
        showtimes_only_theaters: list[str],
        alerted: bool,
        tier: Tier,
        next_check_at: datetime,
        alerted_theaters: list[str] | None = None,
        alerted_listings: list[AlertedListing] | None = None,
        alert_context: AlertContext | None = None,
    ) -> None:
        """`on_sale_theaters` is the full live picture as of this check -
        always overwritten, purely for display/diagnostics (why.py). It is
        NOT what drives re-alerting or the email: `alerted_theaters` is the
        frozen subset that actually justified the most recent alert, only
        ever updated when `alerted` is True (same CASE pattern as
        `alerted_at`) - so a theater merging in silently on a later check,
        with no new alert, never gets credited as "already told to the
        user" (confirmed necessary in production: a second real theater
        joined while status stayed on_sale, and nothing further to read
        from on_sale_theaters alone could tell the difference)."""
        self.conn.execute(
            """
            INSERT INTO ticket_status
                (letterboxd_slug, status, status_date, on_sale_theaters, showtimes_only_theaters,
                 updated_at, alerted_at, alerted_theaters, alerted_listings, alert_context, tier, next_check_at)
            VALUES (:slug, :status, :date, :on_sale, :showtimes_only, :now, :alerted_at, :alerted_theaters, :alerted_listings,
                    :alert_context, :tier, :next_check)
            ON CONFLICT(letterboxd_slug) DO UPDATE SET
                status = excluded.status, status_date = excluded.status_date,
                on_sale_theaters = excluded.on_sale_theaters, showtimes_only_theaters = excluded.showtimes_only_theaters,
                updated_at = excluded.updated_at,
                alerted_at = CASE WHEN excluded.alerted_at IS NOT NULL THEN excluded.alerted_at ELSE ticket_status.alerted_at END,
                alerted_theaters = CASE
                    WHEN excluded.alerted_at IS NOT NULL THEN excluded.alerted_theaters ELSE ticket_status.alerted_theaters
                END,
                alerted_listings = CASE
                    WHEN excluded.alerted_at IS NOT NULL THEN excluded.alerted_listings ELSE ticket_status.alerted_listings
                END,
                alert_context = CASE
                    WHEN excluded.alerted_at IS NOT NULL THEN excluded.alert_context ELSE ticket_status.alert_context
                END,
                notified_at = CASE
                    WHEN excluded.status != 'on_sale' OR excluded.alerted_at IS NOT NULL THEN NULL ELSE ticket_status.notified_at
                END,
                tier = excluded.tier, next_check_at = excluded.next_check_at
            """,
            {
                "slug": slug,
                "status": status.value,
                "date": status_date.isoformat() if status_date else None,
                "on_sale": json.dumps(on_sale_theaters) if on_sale_theaters else None,
                "showtimes_only": json.dumps(showtimes_only_theaters) if showtimes_only_theaters else None,
                "now": _now(),
                "alerted_at": _now() if alerted else None,
                "alerted_theaters": json.dumps(alerted_theaters) if alerted_theaters else None,
                "alerted_listings": _alerted_listings_json(alerted_listings) if alerted_listings else None,
                "alert_context": _alert_context_json(alert_context) if alert_context else None,
                "tier": tier.value,
                "next_check": next_check_at.isoformat(),
            },
        )
        self.conn.commit()

    def clear_alert_history(self, slug: str) -> None:
        self.conn.execute(
            "UPDATE ticket_status SET alerted_at = NULL, alerted_theaters = NULL, alerted_listings = NULL, alert_context = NULL, "
            "notified_at = NULL "
            "WHERE letterboxd_slug = ?",
            (slug,),
        )
        self.conn.commit()

    def get_unnotified(self) -> list[PendingNotification]:
        """Built from alerted_theaters, not on_sale_theaters - the email
        should only ever list the theaters that actually justified this
        alert (already blacklist-filtered), not whatever else happens to
        be on sale by the time the email actually sends."""
        rows = self.conn.execute(
            """
            SELECT films.slug, films.title, films.year, films.url,
                   ticket_status.alerted_theaters, ticket_status.alerted_listings, ticket_status.alert_context,
                   fandango_matches.fandango_slug, fandango_matches.matched_title, fandango_matches.poster_url
            FROM ticket_status
            JOIN films ON films.slug = ticket_status.letterboxd_slug
            LEFT JOIN fandango_matches ON fandango_matches.letterboxd_slug = ticket_status.letterboxd_slug
            WHERE ticket_status.status = 'on_sale' AND ticket_status.notified_at IS NULL
            """
        ).fetchall()
        return [
            PendingNotification(
                film=Film(slug=row["slug"], title=row["title"], year=row["year"], url=row["url"]),
                listings=_parse_alerted_listings(row["alerted_listings"]) or _legacy_alerted_listing(row),
                poster_url=row["poster_url"],
                context=_parse_alert_context(row["alert_context"]),
            )
            for row in rows
        ]

    def mark_notified(self, slug: str) -> None:
        self.conn.execute("UPDATE ticket_status SET notified_at = ? WHERE letterboxd_slug = ?", (_now(), slug))
        self.conn.commit()

    def reapply_blacklist(self, blacklisted_theaters: frozenset[str]) -> list[Film]:
        """Call right after the blacklist changes. Checks alerted_theaters
        - the frozen set that actually justified the current alert - not
        on_sale_theaters, which keeps getting overwritten with whatever's
        live and would hide the fact that a second, real theater joined
        later without ever being its own alert (confirmed in production:
        Pickpocket alerted on Mercado alone, Metreon joined silently
        afterward with on new alert, and checking the live, merged theater
        list made Mercado's blacklisting look like nothing needed fixing).
        A film stays alerted as long as the theater(s) that actually
        justified its current alert aren't ALL blacklisted - getting one
        good alert already satisfied it, and a second theater joining
        later doesn't warrant a second one regardless of blacklist status.
        Only a film whose alerted theaters are now entirely blacklisted
        gets cleared, so it's eligible to alert again once it reaches a
        real one. Recomputes from data already saved on each row - no
        Fandango request needed. Returns the films that got cleared."""
        rows = self.conn.execute(
            """
            SELECT films.slug, films.title, films.year, films.url, ticket_status.alerted_theaters
            FROM ticket_status JOIN films ON films.slug = ticket_status.letterboxd_slug
            WHERE ticket_status.status = 'on_sale'
            """
        ).fetchall()
        cleared = []
        for row in rows:
            theaters = set(json.loads(row["alerted_theaters"])) if row["alerted_theaters"] else set()
            if theaters and not (theaters - blacklisted_theaters):
                self.conn.execute(
                    """
                    UPDATE ticket_status SET status = 'none', alerted_at = NULL, alerted_theaters = NULL,
                        alerted_listings = NULL, alert_context = NULL, notified_at = NULL
                    WHERE letterboxd_slug = ?
                    """,
                    (row["slug"],),
                )
                cleared.append(Film(slug=row["slug"], title=row["title"], year=row["year"], url=row["url"]))
        self.conn.commit()
        return cleared

    def get_health(self) -> HealthState:
        row = self.conn.execute("SELECT * FROM health WHERE id = 1").fetchone()
        if not row:
            return HealthState()
        return HealthState(
            failing_since=_parse_dt(row["failing_since"]),
            last_problem=row["last_problem"],
            alerted_at=_parse_dt(row["alerted_at"]),
        )

    def save_health(self, state: HealthState) -> None:
        self.conn.execute(
            """
            INSERT INTO health (id, failing_since, last_problem, alerted_at) VALUES (1, :since, :problem, :alerted)
            ON CONFLICT(id) DO UPDATE SET
                failing_since = excluded.failing_since, last_problem = excluded.last_problem, alerted_at = excluded.alerted_at
            """,
            {
                "since": state.failing_since.isoformat() if state.failing_since else None,
                "problem": state.last_problem,
                "alerted": state.alerted_at.isoformat() if state.alerted_at else None,
            },
        )
        self.conn.commit()

    def get_config(self, defaults: Config | None = None) -> Config:
        """Returns the stored config, or `defaults` (seeded on first read)
        if nothing's been saved yet - so the form shows real values on its
        very first load instead of blanks."""
        row = self.conn.execute("SELECT * FROM config WHERE id = 1").fetchone()
        if row:
            return Config(
                letterboxd_username=row["letterboxd_username"],
                zip_code=row["zip_code"],
                hype_list_url=row["hype_list_url"],
                blacklisted_theaters=_parse_theater_list(row["blacklisted_theaters"]),
                preferred_theaters=_parse_theater_list(row["preferred_theaters"]),
            )
        defaults = defaults or Config(None, None, None, frozenset())
        self.save_config(defaults)
        return defaults

    def save_config(self, config: Config) -> None:
        self.conn.execute(
            """
            INSERT INTO config (id, letterboxd_username, zip_code, hype_list_url, blacklisted_theaters, preferred_theaters)
            VALUES (1, :username, :zip, :hype, :blacklist, :preferred)
            ON CONFLICT(id) DO UPDATE SET
                letterboxd_username = excluded.letterboxd_username, zip_code = excluded.zip_code,
                hype_list_url = excluded.hype_list_url, blacklisted_theaters = excluded.blacklisted_theaters,
                preferred_theaters = excluded.preferred_theaters
            """,
            {
                "username": config.letterboxd_username,
                "zip": config.zip_code,
                "hype": config.hype_list_url,
                "blacklist": ", ".join(sorted(config.blacklisted_theaters)),
                "preferred": ", ".join(sorted(config.preferred_theaters)),
            },
        )
        self.conn.commit()


def _parse_theater_list(raw: str | None) -> frozenset[str]:
    return frozenset(t.strip() for t in (raw or "").split(",") if t.strip())


def _legacy_alerted_listing(row: sqlite3.Row) -> list[AlertedListing]:
    """An alert saved before per-listing snapshots existed: its theaters,
    under the single listing that was stored back then."""
    theaters = json.loads(row["alerted_theaters"]) if row["alerted_theaters"] else []
    if not row["fandango_slug"]:
        return [AlertedListing(slug="", title=row["title"], dates=[], theaters=theaters)] if theaters else []
    return [AlertedListing(slug=row["fandango_slug"], title=row["matched_title"] or row["title"], dates=[], theaters=theaters)]
