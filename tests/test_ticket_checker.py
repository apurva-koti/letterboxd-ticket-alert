from datetime import date, datetime, timedelta, timezone

import boxofficemojo
import fandango
import letterboxd
import state
import ticket_checker

from conftest import make_fandango_candidate, make_film, make_letterboxd_info, make_match
from models import TicketStatusResult


def test_get_or_match_caches_successful_match(monkeypatch):
    calls = []
    monkeypatch.setattr(letterboxd, "get_film_info", lambda slug: make_letterboxd_info())
    monkeypatch.setattr(
        fandango,
        "match_movie",
        lambda session, title, year=None, director=None: (calls.append(1) or make_fandango_candidate(title=title, year=year), []),
    )
    monkeypatch.setattr(fandango, "get_release_date", lambda session, slug: None)

    conn = state.connect(":memory:")
    ticket_checker.get_or_match(conn, None, make_film())
    ticket_checker.get_or_match(conn, None, make_film())  # second call - should hit cache, not search again

    assert len(calls) == 1


def test_get_or_match_does_not_retry_failed_match_before_backoff(monkeypatch):
    calls = []
    monkeypatch.setattr(letterboxd, "get_film_info", lambda slug: make_letterboxd_info())
    monkeypatch.setattr(
        fandango, "match_movie", lambda session, title, year=None, director=None: (calls.append(1) or None, [])
    )

    conn = state.connect(":memory:")
    ticket_checker.get_or_match(conn, None, make_film())
    ticket_checker.get_or_match(conn, None, make_film())  # immediately again - not due for retry yet

    assert len(calls) == 1


def test_get_or_match_retries_failed_match_after_interval(monkeypatch):
    calls = []
    monkeypatch.setattr(letterboxd, "get_film_info", lambda slug: make_letterboxd_info())
    monkeypatch.setattr(
        fandango, "match_movie", lambda session, title, year=None, director=None: (calls.append(1) or None, [])
    )

    conn = state.connect(":memory:")
    ticket_checker.get_or_match(conn, None, make_film())

    stale = (datetime.now(timezone.utc) - ticket_checker.MATCH_RETRY_INTERVAL - timedelta(minutes=1)).isoformat()
    conn.execute("UPDATE fandango_matches SET checked_at = ?", (stale,))

    ticket_checker.get_or_match(conn, None, make_film())
    assert len(calls) == 2


def test_get_or_match_falls_back_to_letterboxd_when_fandango_has_no_match(monkeypatch):
    monkeypatch.setattr(fandango, "match_movie", lambda session, title, year=None, director=None: (None, []))
    monkeypatch.setattr(letterboxd, "get_film_info", lambda slug: make_letterboxd_info(release_date=date(2026, 11, 13)))

    conn = state.connect(":memory:")
    match = ticket_checker.get_or_match(conn, None, make_film(slug="paper-tiger-2026", title="Paper Tiger"))

    assert match.fandango_id is None
    assert match.release_date == "2026-11-13"
    assert match.release_date_source == "letterboxd"


def test_get_or_match_falls_back_to_boxofficemojo_when_letterboxd_also_empty(monkeypatch):
    import boxofficemojo

    monkeypatch.setattr(fandango, "match_movie", lambda session, title, year=None, director=None: (None, []))
    monkeypatch.setattr(letterboxd, "get_film_info", lambda slug: make_letterboxd_info())
    monkeypatch.setattr(boxofficemojo, "find_release_date", lambda title: date(2026, 8, 28))

    conn = state.connect(":memory:")
    match = ticket_checker.get_or_match(conn, None, make_film(slug="coyote-vs-acme", title="Coyote vs. Acme"))

    assert match.release_date == "2026-08-28"
    assert match.release_date_source == "boxofficemojo"


def test_get_or_match_prefers_fandango_release_date_over_fallbacks(monkeypatch):
    """Fandango's own release date, when available, shouldn't be overridden by
    the fallback chain even though letterboxd_info is fetched unconditionally -
    the `if not release_date` guard must actually gate the fallback."""
    import boxofficemojo

    monkeypatch.setattr(letterboxd, "get_film_info", lambda slug: make_letterboxd_info(release_date=date(1999, 1, 1)))
    monkeypatch.setattr(
        fandango,
        "match_movie",
        lambda session, title, year=None, director=None: (make_fandango_candidate(title=title, year=year), []),
    )
    monkeypatch.setattr(fandango, "get_release_date", lambda session, slug: date(2026, 10, 2))
    monkeypatch.setattr(boxofficemojo, "find_release_date", lambda title: date(1999, 1, 1))

    conn = state.connect(":memory:")
    match = ticket_checker.get_or_match(conn, None, make_film())

    assert match.release_date == "2026-10-02"
    assert match.release_date_source == "fandango"


def test_get_or_match_passes_letterboxd_director_to_fandango_for_disambiguation(monkeypatch):
    seen = {}
    monkeypatch.setattr(letterboxd, "get_film_info", lambda slug: make_letterboxd_info(director="Alejandro G. Inarritu"))

    def fake_match_movie(session, title, year=None, director=None):
        seen["director"] = director
        return None, []

    monkeypatch.setattr(fandango, "match_movie", fake_match_movie)

    conn = state.connect(":memory:")
    ticket_checker.get_or_match(conn, None, make_film())
    assert seen["director"] == "Alejandro G. Inarritu"


def test_get_or_match_excludes_documentaries_without_attempting_fandango(monkeypatch):
    fandango_calls = []
    monkeypatch.setattr(letterboxd, "get_film_info", lambda slug: make_letterboxd_info(genres=["documentary", "nature"]))
    monkeypatch.setattr(
        fandango, "match_movie", lambda session, title, year=None, director=None: (fandango_calls.append(1) or (None, []))[1]
    )

    conn = state.connect(":memory:")
    match = ticket_checker.get_or_match(conn, None, make_film(slug="nuisance-bear-2026", title="Nuisance Bear"))

    assert match.fandango_id is None
    assert match.excluded_reason == "documentary"
    assert fandango_calls == []  # never even attempted - excluded before that point


def test_get_or_match_never_retries_a_documentary_exclusion(monkeypatch):
    """excluded_reason is permanent, unlike a plain failed match - it should
    never re-fetch Letterboxd or re-attempt Fandango on a later call, even
    immediately (no backoff wait needed, since it's not the same kind of
    "maybe it'll show up later" gap a missing catalog entry is)."""
    letterboxd_calls = []
    monkeypatch.setattr(
        letterboxd, "get_film_info", lambda slug: letterboxd_calls.append(1) or make_letterboxd_info(genres=["documentary"])
    )

    conn = state.connect(":memory:")
    ticket_checker.get_or_match(conn, None, make_film(slug="nuisance-bear-2026"))
    ticket_checker.get_or_match(conn, None, make_film(slug="nuisance-bear-2026"))

    assert len(letterboxd_calls) == 1


def test_get_or_match_is_hype_bypasses_fresh_documentary_exclusion(monkeypatch):
    """A Hype-flagged film that would otherwise be excluded as a documentary
    proceeds to a real Fandango match attempt instead."""
    monkeypatch.setattr(letterboxd, "get_film_info", lambda slug: make_letterboxd_info(genres=["documentary"]))
    monkeypatch.setattr(
        fandango,
        "match_movie",
        lambda session, title, year=None, director=None: (make_fandango_candidate(title=title, year=year), []),
    )
    monkeypatch.setattr(fandango, "get_release_date", lambda session, slug: None)

    conn = state.connect(":memory:")
    match = ticket_checker.get_or_match(conn, None, make_film(slug="nuisance-bear-2026"), is_hype=True)

    assert match.excluded_reason is None
    assert match.fandango_id == "245150"


def test_get_or_match_is_hype_overrides_a_past_documentary_exclusion(monkeypatch):
    """Adding a film to Hype after it was already excluded should un-exclude
    it, not leave it permanently stuck from before the flag existed."""
    conn = state.connect(":memory:")
    state.save_match(conn, "nuisance-bear-2026", None, None, None, None, excluded_reason="documentary")
    assert state.get_match(conn, "nuisance-bear-2026").excluded_reason == "documentary"

    monkeypatch.setattr(letterboxd, "get_film_info", lambda slug: make_letterboxd_info(genres=["documentary"]))
    monkeypatch.setattr(
        fandango,
        "match_movie",
        lambda session, title, year=None, director=None: (make_fandango_candidate(title=title, year=year), []),
    )
    monkeypatch.setattr(fandango, "get_release_date", lambda session, slug: None)

    match = ticket_checker.get_or_match(conn, None, make_film(slug="nuisance-bear-2026"), is_hype=True)
    assert match.excluded_reason is None
    assert match.fandango_id == "245150"


def test_get_or_match_is_hype_bypasses_match_retry_backoff(monkeypatch):
    """A still-unmatched Hype film is retried on every call, not throttled to
    once a day like a normal unmatched film - the point of Hype is catching a
    Fandango listing the moment it appears."""
    calls = []
    monkeypatch.setattr(letterboxd, "get_film_info", lambda slug: make_letterboxd_info())
    monkeypatch.setattr(
        fandango, "match_movie", lambda session, title, year=None, director=None: (calls.append(1) or None, [])
    )

    conn = state.connect(":memory:")
    ticket_checker.get_or_match(conn, None, make_film(), is_hype=True)
    ticket_checker.get_or_match(conn, None, make_film(), is_hype=True)  # immediately again

    assert len(calls) == 2  # both attempted - no backoff applied


def test_get_or_match_proceeds_normally_when_letterboxd_info_unavailable(monkeypatch):
    """If the Letterboxd fetch itself fails (network error, page gone), matching
    should still proceed - just without a director hint or fallback date -
    rather than treating a fetch failure as if it excluded the film."""
    monkeypatch.setattr(letterboxd, "get_film_info", lambda slug: (_ for _ in ()).throw(Exception("boom")))
    monkeypatch.setattr(
        fandango,
        "match_movie",
        lambda session, title, year=None, director=None: (make_fandango_candidate(title=title, year=year), []),
    )
    monkeypatch.setattr(fandango, "get_release_date", lambda session, slug: None)

    conn = state.connect(":memory:")
    match = ticket_checker.get_or_match(conn, None, make_film())
    assert match.fandango_id == "245150"
    assert match.excluded_reason is None


def test_get_or_match_refreshes_release_date_for_retired_tier_match(monkeypatch):
    """A matched film whose release date is now well in the past (RETIRED
    tier) should have its release date re-checked, not just returned as-is -
    this is what lets a legacy film's later-announced re-release (e.g. Top
    Gun) actually get noticed."""
    import scheduler

    conn = state.connect(":memory:")
    old_date = date.today() - timedelta(days=scheduler.RECENT_WINDOW_DAYS + 1000)
    state.save_match(
        conn, "top-gun", "111", "top-gun-111", "Top Gun", 1986, old_date, "letterboxd", poster_url="http://x/poster.jpg"
    )

    new_date = date.today() + timedelta(days=200)  # a newly-announced re-release
    monkeypatch.setattr(fandango, "get_release_date", lambda session, slug: None)
    monkeypatch.setattr(letterboxd, "get_us_release_date", lambda slug: new_date)

    match = ticket_checker.get_or_match(conn, None, make_film(slug="top-gun", title="Top Gun", year=1986))

    assert match.release_date == new_date.isoformat()
    assert match.release_date_source == "letterboxd"
    assert match.fandango_id == "111"  # the match itself is untouched, only the date changed
    assert match.poster_url == "http://x/poster.jpg"


def test_get_or_match_keeps_existing_release_date_when_refresh_finds_nothing_new(monkeypatch):
    """If none of the fallback sources have anything on a re-check, the
    previously-known (past) release date should be kept, not blanked out -
    losing it entirely would knock the film into TIER_UNKNOWN instead of
    TIER_RETIRED."""
    import scheduler

    conn = state.connect(":memory:")
    old_date = date.today() - timedelta(days=scheduler.RECENT_WINDOW_DAYS + 1000)
    state.save_match(conn, "close-up", "222", "close-up-222", "Close-Up", 1990, old_date, "letterboxd")

    monkeypatch.setattr(fandango, "get_release_date", lambda session, slug: None)
    monkeypatch.setattr(letterboxd, "get_us_release_date", lambda slug: None)
    monkeypatch.setattr(boxofficemojo, "find_release_date", lambda title: None)

    match = ticket_checker.get_or_match(conn, None, make_film(slug="close-up", title="Close-Up", year=1990))

    assert match.release_date == old_date.isoformat()
    assert match.release_date_source == "letterboxd"


def test_get_or_match_does_not_refresh_a_still_recent_release_date(monkeypatch):
    """A matched film whose release date is recent (not yet RETIRED tier)
    should be returned from cache untouched - no extra Letterboxd/Fandango
    hit on every non-RETIRED check."""
    conn = state.connect(":memory:")
    recent_date = date.today() - timedelta(days=3)
    state.save_match(conn, "your-mother-x3", "333", "your-mother-x3-333", "Your Mother x3", 2026, recent_date, "fandango")

    calls = []
    monkeypatch.setattr(fandango, "get_release_date", lambda session, slug: calls.append(1) or None)
    monkeypatch.setattr(letterboxd, "get_us_release_date", lambda slug: calls.append(1) or None)

    match = ticket_checker.get_or_match(conn, None, make_film(slug="your-mother-x3", title="Your Mother x3"))

    assert calls == []
    assert match.release_date == recent_date.isoformat()


def test_get_or_match_does_not_refresh_release_date_for_hype_films(monkeypatch):
    """Hype films are checked every run - refreshing a release date that
    often would be pure waste, and isn't what Hype is for (urgent upcoming
    pre-sales, not legacy re-release discovery)."""
    import scheduler

    conn = state.connect(":memory:")
    old_date = date.today() - timedelta(days=scheduler.RECENT_WINDOW_DAYS + 1000)
    state.save_match(conn, "old-hype-film", "444", "old-hype-film-444", "Old Hype Film", 1958, old_date, "letterboxd")

    calls = []
    monkeypatch.setattr(fandango, "get_release_date", lambda session, slug: calls.append(1) or None)
    monkeypatch.setattr(letterboxd, "get_us_release_date", lambda slug: calls.append(1) or None)

    match = ticket_checker.get_or_match(
        conn, None, make_film(slug="old-hype-film", title="Old Hype Film", year=1958), is_hype=True
    )

    assert calls == []
    assert match.release_date == old_date.isoformat()


def test_check_film_alerts_only_on_transition_into_on_sale(monkeypatch):
    monkeypatch.setattr(
        ticket_checker,
        "get_or_match",
        lambda conn, session, film: make_match(fandango_id="1", fandango_slug="s", release_date=None),
    )

    statuses = iter(
        [
            TicketStatusResult(date="d", status="showtimes_announced", on_sale_theaters=[], showtimes_only_theaters=["X"]),
            TicketStatusResult(date="d", status="on_sale", on_sale_theaters=["X"], showtimes_only_theaters=[]),
            TicketStatusResult(date="d", status="on_sale", on_sale_theaters=["X"], showtimes_only_theaters=[]),
        ]
    )
    monkeypatch.setattr(fandango, "check_ticket_status", lambda *a, **k: next(statuses))

    conn = state.connect(":memory:")
    film = make_film()

    r1 = ticket_checker.check_film(conn, None, film, "94158")
    assert r1.should_alert is False  # showtimes_announced isn't an alert trigger

    r2 = ticket_checker.check_film(conn, None, film, "94158")
    assert r2.should_alert is True  # transition into on_sale

    r3 = ticket_checker.check_film(conn, None, film, "94158")
    assert r3.should_alert is False  # still on_sale - no repeat alert


def test_check_film_returns_none_when_unmatched(monkeypatch):
    monkeypatch.setattr(ticket_checker, "get_or_match", lambda conn, session, film: make_match(fandango_id=None))
    conn = state.connect(":memory:")
    assert ticket_checker.check_film(conn, None, make_film(), "94158") is None
