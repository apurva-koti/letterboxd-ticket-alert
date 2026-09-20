from datetime import date, datetime, timedelta, timezone

import pytest

import fandango
import letterboxd
import scheduler
import state
import ticket_checker

from conftest import make_film, make_match
from models import TicketStatusResult


TODAY = date(2026, 9, 19)


@pytest.mark.parametrize(
    "release_date,expected",
    [
        (None, scheduler.TIER_UNKNOWN),
        (TODAY + timedelta(days=1), scheduler.TIER_HOT),
        (TODAY + timedelta(days=90), scheduler.TIER_HOT),
        (TODAY + timedelta(days=91), scheduler.TIER_FAR_FUTURE),
        (TODAY, scheduler.TIER_RECENT),
        (TODAY - timedelta(days=14), scheduler.TIER_RECENT),
        (TODAY - timedelta(days=15), scheduler.TIER_RETIRED),
        (TODAY - timedelta(days=3650), scheduler.TIER_RETIRED),
    ],
)
def test_compute_tier_boundaries(release_date, expected):
    assert scheduler.compute_tier(release_date, today=TODAY) == expected


@pytest.mark.parametrize("release_date", [None, TODAY - timedelta(days=3650), TODAY + timedelta(days=500)])
def test_compute_tier_is_hype_overrides_everything(release_date):
    """A Hype-list film is always TIER_MUST_WATCH regardless of what the
    release-date math alone would say - unknown, ancient, or years away."""
    assert scheduler.compute_tier(release_date, today=TODAY, is_hype=True) == scheduler.TIER_MUST_WATCH


def test_next_check_time_stays_within_jitter_bounds():
    now = datetime(2026, 9, 19, tzinfo=timezone.utc)
    interval = scheduler.INTERVALS[scheduler.TIER_HOT]
    low = now + interval * (1 - scheduler.JITTER_FRACTION)
    high = now + interval * (1 + scheduler.JITTER_FRACTION)

    for _ in range(50):
        result = scheduler.next_check_time(scheduler.TIER_HOT, now)
        assert low <= result <= high


def test_is_due_true_when_never_checked():
    assert scheduler.is_due(None, datetime.now(timezone.utc)) is True


def test_is_due_false_before_scheduled_time():
    now = datetime.now(timezone.utc)
    future = (now + timedelta(hours=1)).isoformat()
    assert scheduler.is_due(future, now) is False


def test_is_due_true_after_scheduled_time():
    now = datetime.now(timezone.utc)
    past = (now - timedelta(hours=1)).isoformat()
    assert scheduler.is_due(past, now) is True


# ---- full run() integration, with the network layer mocked ----

REAL_MATCHES = {
    "coyote-vs-acme": {"fandango_id": "246329", "slug": "coyote-vs-acme-246329", "release_date": date(2026, 8, 28)},
    "your-mother-x3": {"fandango_id": "246407", "slug": "your-mother-x3", "release_date": date(2026, 9, 25)},
    "digger-2026": {"fandango_id": "245150", "slug": "digger-2026-245150", "release_date": date(2026, 10, 2)},
}
REAL_STATUSES = {
    "coyote-vs-acme": TicketStatusResult(date="2026-09-19", status="on_sale", on_sale_theaters=["AMC 34th"], showtimes_only_theaters=[]),
    "your-mother-x3": TicketStatusResult(date="2026-09-27", status="on_sale", on_sale_theaters=["Alamo"], showtimes_only_theaters=[]),
    "digger-2026": TicketStatusResult(date="2026-10-01", status="showtimes_announced", on_sale_theaters=[], showtimes_only_theaters=["AMC Metreon"]),
}


def _fake_get_or_match(conn, session, film, is_hype=False):
    m = REAL_MATCHES.get(film.slug)
    if not m:
        return make_match(letterboxd_slug=film.slug, fandango_id=None)
    return make_match(
        letterboxd_slug=film.slug,
        fandango_id=m["fandango_id"],
        fandango_slug=m["slug"],
        release_date=m["release_date"].isoformat(),
    )


def _fake_check_ticket_status(session, fandango_id, fandango_slug, zip_code, release_date=None, days_ahead=14):
    for slug, m in REAL_MATCHES.items():
        if m["fandango_id"] == fandango_id:
            return REAL_STATUSES[slug]
    return None


def test_run_skips_unmatched_films_hot_films_alert_retired_films_still_checked_once(monkeypatch, tmp_path):
    """Legacy films (Vertigo, Close-Up) are tracked like any other watchlist
    film now - no age-based cutoff - but still drop out of this run's
    checked set because _fake_get_or_match has no Fandango match for them
    (fandango_id=None), same as any other still-unmatched film would."""
    films = [
        make_film(title="Coyote vs. Acme", year=2024, slug="coyote-vs-acme"),  # retired tier
        make_film(title="Your Mother x3", year=2026, slug="your-mother-x3"),  # hot
        make_film(title="Digger", year=2026, slug="digger-2026"),  # hot, announced only
        make_film(title="Vertigo", year=1958, slug="vertigo"),  # legacy, unmatched
        make_film(title="Close-Up", year=1990, slug="close-up"),  # legacy, unmatched
    ]
    monkeypatch.setattr(letterboxd, "get_watchlist", lambda username, **kw: films)
    monkeypatch.setattr(fandango, "new_session", lambda: None)
    monkeypatch.setattr(ticket_checker, "get_or_match", _fake_get_or_match)
    monkeypatch.setattr(fandango, "check_ticket_status", _fake_check_ticket_status)
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: None)

    db_path = str(tmp_path / "test.db")
    result = scheduler.run("fake_user", "94158", db_path=db_path)

    checked_slugs = {o.film.slug for o in result["checked"]}
    assert checked_slugs == {"coyote-vs-acme", "your-mother-x3", "digger-2026"}
    assert "vertigo" not in checked_slugs  # unmatched, not excluded by scope
    assert "close-up" not in checked_slugs

    alerted_slugs = {o.film.slug for o in result["alerts"]}
    assert alerted_slugs == {"coyote-vs-acme", "your-mother-x3"}

    tiers = {o.film.slug: o.tier for o in result["checked"]}
    assert tiers["coyote-vs-acme"] == scheduler.TIER_RETIRED
    assert tiers["your-mother-x3"] == scheduler.TIER_HOT
    assert tiers["digger-2026"] == scheduler.TIER_HOT


def test_run_caps_films_processed_per_run(monkeypatch, tmp_path):
    """Regression test for a real production incident: removing the 2-year
    cutoff meant every previously-excluded legacy film became due at once (no
    ticket_status row yet - see is_due), and a large batch processed in one
    run blew past Modal's 600s function timeout, which then looped since the
    same backlog was still due on the next run too. MAX_PROCESSED_PER_RUN
    bounds how much work a single run does so it always finishes (and
    persists progress) regardless of backlog size - the excess just waits for
    a later run instead."""
    films = [make_film(title=f"Film {i}", year=2020, slug=f"film-{i}") for i in range(scheduler.MAX_PROCESSED_PER_RUN + 5)]
    monkeypatch.setattr(letterboxd, "get_watchlist", lambda username, **kw: films)
    monkeypatch.setattr(fandango, "new_session", lambda: None)
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: None)

    attempted = []

    def counting_get_or_match(conn, session, film, is_hype=False):
        attempted.append(film.slug)
        return make_match(letterboxd_slug=film.slug, fandango_id=None)

    monkeypatch.setattr(ticket_checker, "get_or_match", counting_get_or_match)

    db_path = str(tmp_path / "test.db")
    scheduler.run("fake_user", "94158", db_path=db_path)

    assert len(attempted) == scheduler.MAX_PROCESSED_PER_RUN  # not all of the backlog in one run


def test_run_cap_does_not_apply_to_hype_films(monkeypatch, tmp_path):
    """A Hype film must still be processed every run even if the cap has
    already been reached by unrelated due watchlist films - Hype's whole
    point is "checked every run, no matter what"."""
    watchlist_films = [
        make_film(title=f"Film {i}", year=2020, slug=f"film-{i}") for i in range(scheduler.MAX_PROCESSED_PER_RUN)
    ]
    hype_films = [make_film(title="Dune: Part Three", year=2026, slug="dune-part-three")]

    attempted = []

    def counting_get_or_match(conn, session, film, is_hype=False):
        attempted.append((film.slug, is_hype))
        return make_match(letterboxd_slug=film.slug, fandango_id=None)

    monkeypatch.setattr(letterboxd, "get_watchlist", lambda username, **kw: watchlist_films)
    monkeypatch.setattr(letterboxd, "get_list", lambda url, **kw: hype_films)
    monkeypatch.setattr(fandango, "new_session", lambda: None)
    monkeypatch.setattr(ticket_checker, "get_or_match", counting_get_or_match)
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: None)

    db_path = str(tmp_path / "test.db")
    scheduler.run("fake_user", "94158", hype_list_url="https://letterboxd.com/x/list/hype/", db_path=db_path)

    assert ("dune-part-three", True) in attempted


def test_run_second_pass_immediately_after_finds_nothing_due(monkeypatch, tmp_path):
    films = [make_film(title="Your Mother x3", year=2026, slug="your-mother-x3")]
    monkeypatch.setattr(letterboxd, "get_watchlist", lambda username, **kw: films)
    monkeypatch.setattr(fandango, "new_session", lambda: None)
    monkeypatch.setattr(ticket_checker, "get_or_match", _fake_get_or_match)
    monkeypatch.setattr(fandango, "check_ticket_status", _fake_check_ticket_status)
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: None)

    db_path = str(tmp_path / "test.db")
    scheduler.run("fake_user", "94158", db_path=db_path)
    result = scheduler.run("fake_user", "94158", db_path=db_path)

    assert result["checked"] == []
    assert result["alerts"] == []


def test_run_tracks_hype_only_film_and_forces_must_watch_tier(monkeypatch, tmp_path):
    """A film in the Hype list but NOT on the watchlist still gets tracked and
    forced to TIER_MUST_WATCH regardless of its year."""
    watchlist_films = [make_film(title="Your Mother x3", year=2026, slug="your-mother-x3")]
    hype_films = [make_film(title="Dune: Part Three", year=1958, slug="dune-part-three")]  # old year on purpose

    def fake_get_or_match(conn, session, film, is_hype=False):
        return make_match(letterboxd_slug=film.slug, fandango_id="999", fandango_slug="dune-3-999", release_date=None)

    monkeypatch.setattr(letterboxd, "get_watchlist", lambda username, **kw: watchlist_films)
    monkeypatch.setattr(letterboxd, "get_list", lambda url, **kw: hype_films)
    monkeypatch.setattr(fandango, "new_session", lambda: None)
    monkeypatch.setattr(ticket_checker, "get_or_match", fake_get_or_match)
    monkeypatch.setattr(fandango, "check_ticket_status", lambda *a, **k: None)
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: None)

    db_path = str(tmp_path / "test.db")
    result = scheduler.run("fake_user", "94158", hype_list_url="https://letterboxd.com/x/list/hype/", db_path=db_path)

    checked_by_slug = {o.film.slug: o for o in result["checked"]}
    assert "dune-part-three" in checked_by_slug  # tracked despite not being on the watchlist
    assert checked_by_slug["dune-part-three"].tier == scheduler.TIER_MUST_WATCH


def test_run_hype_fetch_failure_does_not_break_watchlist_tracking(monkeypatch, tmp_path):
    """If fetching the Hype list itself fails (network error, bad URL), the
    run should still track the watchlist normally rather than failing outright."""
    watchlist_films = [make_film(title="Your Mother x3", year=2026, slug="your-mother-x3")]

    monkeypatch.setattr(letterboxd, "get_watchlist", lambda username, **kw: watchlist_films)
    monkeypatch.setattr(letterboxd, "get_list", lambda url, **kw: (_ for _ in ()).throw(Exception("boom")))
    monkeypatch.setattr(fandango, "new_session", lambda: None)
    monkeypatch.setattr(ticket_checker, "get_or_match", _fake_get_or_match)
    monkeypatch.setattr(fandango, "check_ticket_status", _fake_check_ticket_status)
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: None)

    db_path = str(tmp_path / "test.db")
    result = scheduler.run("fake_user", "94158", hype_list_url="https://letterboxd.com/x/list/hype/", db_path=db_path)

    assert {o.film.slug for o in result["checked"]} == {"your-mother-x3"}
