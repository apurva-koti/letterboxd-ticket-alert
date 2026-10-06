"""SQLite persistence: watchlist films, their Fandango binding, and ticket status."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone

from domain import FandangoListing, Film, PendingNotification, Tier, TicketStatus, TrackedFilm

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

CREATE TABLE IF NOT EXISTS ticket_status (
    letterboxd_slug TEXT PRIMARY KEY REFERENCES films(slug),
    status TEXT NOT NULL,
    status_date TEXT,
    on_sale_theaters TEXT,
    showtimes_only_theaters TEXT,
    updated_at TEXT NOT NULL,
    alerted_at TEXT,
    tier TEXT,
    next_check_at TEXT,
    notified_at TEXT
);
"""

# CREATE TABLE IF NOT EXISTS above only applies to brand-new DBs - an
# existing one needs columns added after the fact explicitly.
MIGRATIONS = [
    "ALTER TABLE fandango_matches ADD COLUMN director TEXT",
    "ALTER TABLE fandango_matches ADD COLUMN runtime INTEGER",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


class FilmRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def connect(cls, db_path: str = DB_PATH) -> "FilmRepository":
        conn = sqlite3.connect(db_path)
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

        fandango = None
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
            if match_row["fandango_id"]:
                fandango = FandangoListing(
                    fandango_id=match_row["fandango_id"],
                    slug=match_row["fandango_slug"],
                    title=match_row["matched_title"],
                    year=match_row["matched_year"],
                    url=f"https://www.fandango.com/{match_row['fandango_slug']}/movie-overview",
                )

        status = TicketStatus.NONE
        on_sale_theaters: list[str] = []
        showtimes_only_theaters: list[str] = []
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
            tier = Tier(status_row["tier"]) if status_row["tier"] else None
            next_check_at = _parse_dt(status_row["next_check_at"])
            alerted_at = _parse_dt(status_row["alerted_at"])
            notified_at = _parse_dt(status_row["notified_at"])

        return TrackedFilm(
            film=film,
            anchor_date=anchor_date,
            director=director,
            runtime=runtime,
            fandango=fandango,
            poster_url=poster_url,
            excluded_reason=excluded_reason,
            status=status,
            on_sale_theaters=on_sale_theaters,
            showtimes_only_theaters=showtimes_only_theaters,
            tier=tier,
            next_check_at=next_check_at,
            alerted_at=alerted_at,
            notified_at=notified_at,
            checked_at=checked_at,
        )

    def save_match(
        self,
        slug: str,
        fandango: FandangoListing | None,
        anchor_date: date | None,
        director: str | None = None,
        runtime: int | None = None,
        excluded_reason: str | None = None,
        poster_url: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO fandango_matches
                (letterboxd_slug, fandango_id, fandango_slug, matched_title, matched_year,
                 release_date, release_date_source, director, runtime, excluded_reason, poster_url, checked_at)
            VALUES (:slug, :fid, :fslug, :title, :year, :date, :source, :director, :runtime, :excluded, :poster, :now)
            ON CONFLICT(letterboxd_slug) DO UPDATE SET
                fandango_id = excluded.fandango_id, fandango_slug = excluded.fandango_slug,
                matched_title = excluded.matched_title, matched_year = excluded.matched_year,
                release_date = excluded.release_date, release_date_source = excluded.release_date_source,
                director = excluded.director, runtime = excluded.runtime, excluded_reason = excluded.excluded_reason,
                poster_url = excluded.poster_url, checked_at = excluded.checked_at
            """,
            {
                "slug": slug,
                "fid": fandango.fandango_id if fandango else None,
                "fslug": fandango.slug if fandango else None,
                "title": fandango.title if fandango else None,
                "year": fandango.year if fandango else None,
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
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO ticket_status
                (letterboxd_slug, status, status_date, on_sale_theaters, showtimes_only_theaters,
                 updated_at, alerted_at, tier, next_check_at)
            VALUES (:slug, :status, :date, :on_sale, :showtimes_only, :now, :alerted_at, :tier, :next_check)
            ON CONFLICT(letterboxd_slug) DO UPDATE SET
                status = excluded.status, status_date = excluded.status_date,
                on_sale_theaters = excluded.on_sale_theaters, showtimes_only_theaters = excluded.showtimes_only_theaters,
                updated_at = excluded.updated_at,
                alerted_at = CASE WHEN excluded.alerted_at IS NOT NULL THEN excluded.alerted_at ELSE ticket_status.alerted_at END,
                notified_at = CASE WHEN excluded.status != 'on_sale' THEN NULL ELSE ticket_status.notified_at END,
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
                "tier": tier.value,
                "next_check": next_check_at.isoformat(),
            },
        )
        self.conn.commit()

    def get_unnotified(self) -> list[PendingNotification]:
        rows = self.conn.execute(
            """
            SELECT films.slug, films.title, films.year, films.url,
                   ticket_status.on_sale_theaters, fandango_matches.fandango_slug, fandango_matches.poster_url
            FROM ticket_status
            JOIN films ON films.slug = ticket_status.letterboxd_slug
            LEFT JOIN fandango_matches ON fandango_matches.letterboxd_slug = ticket_status.letterboxd_slug
            WHERE ticket_status.status = 'on_sale' AND ticket_status.notified_at IS NULL
            """
        ).fetchall()
        return [
            PendingNotification(
                film=Film(slug=row["slug"], title=row["title"], year=row["year"], url=row["url"]),
                on_sale_theaters=json.loads(row["on_sale_theaters"]) if row["on_sale_theaters"] else [],
                ticket_url=f"https://www.fandango.com/{row['fandango_slug']}/movie-overview" if row["fandango_slug"] else row["url"],
                poster_url=row["poster_url"],
            )
            for row in rows
        ]

    def mark_notified(self, slug: str) -> None:
        self.conn.execute("UPDATE ticket_status SET notified_at = ? WHERE letterboxd_slug = ?", (_now(), slug))
        self.conn.commit()
