"""SQLite persistence for watchlist state, so we can diff runs over time."""

import json
import sqlite3
from datetime import datetime, timezone

from models import FandangoMatch, Film, PendingNotification, TicketStatus

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

# Columns added after the original schema. CREATE TABLE IF NOT EXISTS above only
# applies to brand-new DBs, so existing ones need these added explicitly - each
# ALTER is a no-op (caught) if the column's already there.
MIGRATIONS = [
    "ALTER TABLE fandango_matches ADD COLUMN release_date TEXT",
    "ALTER TABLE fandango_matches ADD COLUMN release_date_source TEXT",
    "ALTER TABLE fandango_matches ADD COLUMN excluded_reason TEXT",
    "ALTER TABLE ticket_status ADD COLUMN tier TEXT",
    "ALTER TABLE ticket_status ADD COLUMN next_check_at TEXT",
    "ALTER TABLE ticket_status ADD COLUMN notified_at TEXT",
    "ALTER TABLE fandango_matches ADD COLUMN poster_url TEXT",
]


def connect(db_path=DB_PATH):
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
    return conn


def sync_watchlist(conn, films):
    """Upserts the current watchlist into the DB and reports what changed.

    Films no longer on the watchlist are deleted from the DB, not kept
    around, so state here always mirrors what's actually on the watchlist.

    Returns (added, removed) as lists of Film.
    """
    now = datetime.now(timezone.utc).isoformat()
    current_slugs = {f.slug: f for f in films}

    existing_rows = {row["slug"]: row for row in conn.execute("SELECT * FROM films")}

    added = [film for slug, film in current_slugs.items() if slug not in existing_rows]

    for slug, film in current_slugs.items():
        conn.execute(
            """
            INSERT INTO films (slug, title, year, url, first_seen, last_seen)
            VALUES (:slug, :title, :year, :url, :now, :now)
            ON CONFLICT(slug) DO UPDATE SET
                title = excluded.title,
                year = excluded.year,
                url = excluded.url,
                last_seen = excluded.last_seen
            """,
            {"slug": film.slug, "title": film.title, "year": film.year, "url": film.url, "now": now},
        )

    removed = [
        Film(slug=row["slug"], title=row["title"], year=row["year"], url=row["url"])
        for slug, row in existing_rows.items()
        if slug not in current_slugs
    ]
    for film in removed:
        conn.execute("DELETE FROM films WHERE slug = ?", (film.slug,))
        conn.execute("DELETE FROM fandango_matches WHERE letterboxd_slug = ?", (film.slug,))
        conn.execute("DELETE FROM ticket_status WHERE letterboxd_slug = ?", (film.slug,))

    conn.commit()
    return added, removed


def get_match(conn, slug):
    row = conn.execute("SELECT * FROM fandango_matches WHERE letterboxd_slug = ?", (slug,)).fetchone()
    return FandangoMatch(**dict(row)) if row else None


def save_match(
    conn,
    slug,
    fandango_id,
    fandango_slug,
    matched_title,
    matched_year,
    release_date=None,
    release_date_source=None,
    excluded_reason=None,
    poster_url=None,
):
    """fandango_id=None records a failed match attempt, so callers can retry it
    later without re-scraping every film on every run. release_date is an ISO
    date string (or None) - it's the scheduler's proxy for how urgently to poll,
    since Fandango has no field for when tickets actually go on sale. It can
    come from Fandango itself, or - when Fandango has no match or no date yet -
    from Letterboxd's Releases tab or, failing that, Box Office Mojo;
    release_date_source records which, for transparency/debugging.
    excluded_reason (e.g. "documentary") marks a film as permanently out of
    scope - unlike a plain failed match, this is never retried. poster_url
    comes from Letterboxd (fetched alongside director/release_date/genres in
    get_film_info) and is stored here for later use composing alert emails."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO fandango_matches
            (letterboxd_slug, fandango_id, fandango_slug, matched_title, matched_year,
             release_date, release_date_source, excluded_reason, poster_url, checked_at)
        VALUES (:slug, :fandango_id, :fandango_slug, :matched_title, :matched_year,
                :release_date, :release_date_source, :excluded_reason, :poster_url, :now)
        ON CONFLICT(letterboxd_slug) DO UPDATE SET
            fandango_id = excluded.fandango_id,
            fandango_slug = excluded.fandango_slug,
            matched_title = excluded.matched_title,
            matched_year = excluded.matched_year,
            release_date = excluded.release_date,
            release_date_source = excluded.release_date_source,
            excluded_reason = excluded.excluded_reason,
            poster_url = excluded.poster_url,
            checked_at = excluded.checked_at
        """,
        {
            "slug": slug,
            "fandango_id": fandango_id,
            "fandango_slug": fandango_slug,
            "matched_title": matched_title,
            "matched_year": matched_year,
            "release_date": release_date.isoformat() if release_date else None,
            "release_date_source": release_date_source,
            "excluded_reason": excluded_reason,
            "poster_url": poster_url,
            "now": now,
        },
    )
    conn.commit()


def get_ticket_status(conn, slug):
    row = conn.execute("SELECT * FROM ticket_status WHERE letterboxd_slug = ?", (slug,)).fetchone()
    if not row:
        return None
    return TicketStatus(
        letterboxd_slug=row["letterboxd_slug"],
        status=row["status"],
        status_date=row["status_date"],
        on_sale_theaters=json.loads(row["on_sale_theaters"]) if row["on_sale_theaters"] else [],
        showtimes_only_theaters=json.loads(row["showtimes_only_theaters"]) if row["showtimes_only_theaters"] else [],
        updated_at=row["updated_at"],
        alerted_at=row["alerted_at"],
        tier=row["tier"],
        next_check_at=row["next_check_at"],
        notified_at=row["notified_at"],
    )


def save_ticket_status(conn, slug, status, result, alerted, tier=None, next_check_at=None):
    """`result` is fandango.check_ticket_status()'s return value: a
    TicketStatusResult, or None. `alerted` is whether this call represents a
    new on-sale alert firing - once true for a given on-sale episode,
    alerted_at is preserved across later calls so it doesn't get reset by
    every subsequent check. `tier`/`next_check_at` are the scheduler's
    bookkeeping for when this film is next due; left as None for
    direct/manual checks outside the scheduler.

    notified_at is deliberately NOT set here - it only gets set by
    mark_notified(), after an SMS actually sends successfully. It's cleared
    back to NULL whenever status isn't on_sale, so a later on_sale episode
    needs a fresh notification rather than being silently skipped because an
    old episode was already notified about.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO ticket_status
            (letterboxd_slug, status, status_date, on_sale_theaters, showtimes_only_theaters,
             updated_at, alerted_at, tier, next_check_at)
        VALUES (:slug, :status, :status_date, :on_sale_theaters, :showtimes_only_theaters,
                :now, :alerted_at, :tier, :next_check_at)
        ON CONFLICT(letterboxd_slug) DO UPDATE SET
            status = excluded.status,
            status_date = excluded.status_date,
            on_sale_theaters = excluded.on_sale_theaters,
            showtimes_only_theaters = excluded.showtimes_only_theaters,
            updated_at = excluded.updated_at,
            alerted_at = CASE
                WHEN excluded.alerted_at IS NOT NULL THEN excluded.alerted_at
                ELSE ticket_status.alerted_at
            END,
            notified_at = CASE
                WHEN excluded.status != 'on_sale' THEN NULL
                ELSE ticket_status.notified_at
            END,
            tier = excluded.tier,
            next_check_at = excluded.next_check_at
        """,
        {
            "slug": slug,
            "status": status,
            "status_date": result.date if result else None,
            "on_sale_theaters": json.dumps(result.on_sale_theaters) if result else None,
            "showtimes_only_theaters": json.dumps(result.showtimes_only_theaters) if result else None,
            "now": now,
            "alerted_at": now if alerted else None,
            "tier": tier,
            "next_check_at": next_check_at,
        },
    )
    conn.commit()


def get_unnotified_on_sale(conn):
    """Films currently on_sale that haven't been successfully notified about
    yet. Deliberately scoped to the whole DB, not just films touched in the
    current scheduler run - a film whose SMS failed to send stays here (and
    gets retried) on every subsequent call, on the notification layer's own
    cadence, independent of that film's ticket-check tier interval."""
    import fandango  # local import: avoids a module-load-order dependency for the common case

    rows = conn.execute(
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
            # status can only be on_sale via a matched film, so fandango_slug should
            # always be set here - the fallback to the Letterboxd url is defensive,
            # not an expected path.
            ticket_url=f"{fandango.BASE_URL}/{row['fandango_slug']}/movie-overview" if row["fandango_slug"] else row["url"],
            poster_url=row["poster_url"],
        )
        for row in rows
    ]


def mark_notified(conn, slug):
    """Records that an SMS was successfully sent for this film's current
    on_sale episode, so get_unnotified_on_sale stops returning it - until
    status leaves on_sale and comes back for a later, distinct episode."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE ticket_status SET notified_at = ? WHERE letterboxd_slug = ?", (now, slug))
    conn.commit()


if __name__ == "__main__":
    import sys

    from letterboxd import get_watchlist

    if len(sys.argv) != 2:
        print(f"Usage: python3 {sys.argv[0]} <letterboxd_username>")
        sys.exit(1)

    username = sys.argv[1]
    conn = connect()
    films = get_watchlist(username)
    added, removed = sync_watchlist(conn, films)

    print(f"Synced {len(films)} films for {username}.")
    if added:
        print(f"\nAdded ({len(added)}):")
        for f in added:
            print(f"  + {f.title} ({f.year})")
    if removed:
        print(f"\nRemoved ({len(removed)}):")
        for f in removed:
            print(f"  - {f.title} ({f.year})")
    if not added and not removed:
        print("No changes since last sync.")
