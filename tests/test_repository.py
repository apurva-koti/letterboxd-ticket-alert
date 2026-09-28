from datetime import date, datetime, timezone

from domain import Tier, TicketStatus
from repository import FilmRepository

from conftest import make_film, make_listing


def repo():
    return FilmRepository.connect(":memory:")


def test_sync_watchlist_reports_added_and_removed():
    r = repo()
    added, removed = r.sync_watchlist([make_film(slug="a"), make_film(slug="b")])
    assert {f.slug for f in added} == {"a", "b"}
    assert removed == []

    added, removed = r.sync_watchlist([make_film(slug="a")])
    assert added == []
    assert {f.slug for f in removed} == {"b"}


def test_sync_watchlist_removal_cascades_to_match_and_status():
    r = repo()
    r.sync_watchlist([make_film(slug="a")])
    r.save_match("a", make_listing(), date(2026, 1, 1))
    r.save_status("a", TicketStatus.ON_SALE, date(2026, 1, 1), ["Theater"], [], True, Tier.HOT, datetime.now(timezone.utc))

    r.sync_watchlist([])
    assert r.get("a") is None


def test_get_returns_none_for_untracked_slug():
    assert repo().get("nope") is None


def test_get_returns_defaults_for_never_processed_film():
    r = repo()
    r.sync_watchlist([make_film(slug="a")])
    tracked = r.get("a")
    assert tracked.anchor_date is None
    assert tracked.fandango is None
    assert tracked.status == TicketStatus.NONE
    assert tracked.tier is None
    assert tracked.next_check_at is None


def test_save_match_round_trips_fandango_listing():
    r = repo()
    r.sync_watchlist([make_film(slug="a")])
    r.save_match("a", make_listing(fandango_id="999", slug="some-slug"), date(2026, 5, 1), poster_url="http://x/p.jpg")

    tracked = r.get("a")
    assert tracked.fandango.fandango_id == "999"
    assert tracked.anchor_date == date(2026, 5, 1)
    assert tracked.poster_url == "http://x/p.jpg"


def test_clear_fandango_binding_keeps_anchor_date():
    r = repo()
    r.sync_watchlist([make_film(slug="a")])
    r.save_match("a", make_listing(), date(1986, 5, 16))

    r.clear_fandango_binding("a")
    tracked = r.get("a")
    assert tracked.fandango is None
    assert tracked.anchor_date == date(1986, 5, 16)


def test_save_status_clears_notified_at_when_status_leaves_on_sale():
    r = repo()
    r.sync_watchlist([make_film(slug="a")])
    now = datetime.now(timezone.utc)
    r.save_status("a", TicketStatus.ON_SALE, date(2026, 1, 1), ["X"], [], True, Tier.HOT, now)
    r.mark_notified("a")
    assert r.get("a").notified_at is not None

    r.save_status("a", TicketStatus.NONE, None, [], [], False, Tier.RECENT, now)
    assert r.get("a").notified_at is None


def test_save_status_preserves_alerted_at_across_unrelated_calls():
    r = repo()
    r.sync_watchlist([make_film(slug="a")])
    now = datetime.now(timezone.utc)
    r.save_status("a", TicketStatus.ON_SALE, date(2026, 1, 1), ["X"], [], True, Tier.HOT, now)
    first_alerted_at = r.get("a").alerted_at

    r.save_status("a", TicketStatus.ON_SALE, date(2026, 1, 2), ["X"], [], False, Tier.HOT, now)
    assert r.get("a").alerted_at == first_alerted_at


def test_reset_for_retirement_clears_status_and_alerts():
    r = repo()
    r.sync_watchlist([make_film(slug="a")])
    now = datetime.now(timezone.utc)
    r.save_status("a", TicketStatus.ON_SALE, date(2026, 1, 1), ["X"], [], True, Tier.HOT, now)
    r.mark_notified("a")

    r.reset_for_retirement("a", now)
    tracked = r.get("a")
    assert tracked.status == TicketStatus.NONE
    assert tracked.on_sale_theaters == []
    assert tracked.alerted_at is None
    assert tracked.notified_at is None
    assert tracked.tier == Tier.RETIRED


def test_get_unnotified_only_returns_on_sale_without_notification():
    r = repo()
    r.sync_watchlist([make_film(slug="a"), make_film(slug="b")])
    r.save_match("a", make_listing(slug="a-slug"), date(2026, 1, 1))
    now = datetime.now(timezone.utc)
    r.save_status("a", TicketStatus.ON_SALE, date(2026, 1, 1), ["Theater A"], [], True, Tier.HOT, now)
    r.save_status("b", TicketStatus.SHOWTIMES_ANNOUNCED, date(2026, 1, 1), [], ["Theater B"], False, Tier.HOT, now)

    pending = r.get_unnotified()
    assert [p.film.slug for p in pending] == ["a"]
    assert pending[0].on_sale_theaters == ["Theater A"]


def test_mark_notified_removes_from_unnotified():
    r = repo()
    r.sync_watchlist([make_film(slug="a")])
    r.save_match("a", make_listing(), date(2026, 1, 1))
    r.save_status("a", TicketStatus.ON_SALE, date(2026, 1, 1), ["X"], [], True, Tier.HOT, datetime.now(timezone.utc))

    r.mark_notified("a")
    assert r.get_unnotified() == []
