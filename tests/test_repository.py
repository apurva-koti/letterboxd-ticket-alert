from datetime import date, datetime, timezone

from domain import Config, Tier, TicketStatus
from repository import FilmRepository

from conftest import make_film, make_listing


def repo():
    return FilmRepository.connect(":memory:")


def test_get_config_seeds_from_defaults_on_first_read():
    r = repo()
    defaults = Config(letterboxd_username="dave", zip_code="94158", hype_list_url=None, blacklisted_theaters=frozenset())
    assert r.get_config(defaults) == defaults
    assert r.get_config() == defaults  # seeded - a second read needs no defaults


def test_reapply_blacklist_clears_a_film_alerted_only_at_now_blacklisted_theaters():
    r = repo()
    r.sync_watchlist([make_film(slug="singin-in-the-rain", title="Singin' in the Rain", year=1952)])
    now = datetime.now(timezone.utc)
    r.save_status(
        "singin-in-the-rain",
        TicketStatus.ON_SALE,
        date(2026, 10, 7),
        ["The New Parkway"],
        [],
        True,
        Tier.RETIRED,
        now,
        alerted_theaters=["The New Parkway"],
    )

    cleared = r.reapply_blacklist(frozenset({"The New Parkway"}))

    assert [f.slug for f in cleared] == ["singin-in-the-rain"]
    tracked = r.get("singin-in-the-rain")
    assert tracked.status == TicketStatus.NONE
    assert tracked.alerted_at is None
    assert tracked.notified_at is None


def test_reapply_blacklist_leaves_a_film_alone_if_any_alerted_theater_survives():
    """Getting one good alert already satisfied it - a second theater
    (blacklisted or not) joining later, with no new alert of its own,
    doesn't undo that."""
    r = repo()
    r.sync_watchlist([make_film(slug="pickpocket")])
    now = datetime.now(timezone.utc)
    r.save_status(
        "pickpocket",
        TicketStatus.ON_SALE,
        date(2026, 10, 7),
        ["AMC Mercado 20", "AMC Metreon 16"],
        [],
        True,
        Tier.HOT,
        now,
        alerted_theaters=["AMC Mercado 20", "AMC Metreon 16"],
    )

    cleared = r.reapply_blacklist(frozenset({"AMC Mercado 20"}))

    assert cleared == []
    tracked = r.get("pickpocket")
    assert tracked.status == TicketStatus.ON_SALE
    assert tracked.alerted_at is not None


def test_reapply_blacklist_ignores_theaters_that_joined_after_the_alert():
    """on_sale_theaters keeps getting overwritten with whatever's live and
    must NOT be what this checks - a theater that merged in after the
    alert fired (no new alert of its own) is not part of what justified
    it, so it shouldn't rescue a film whose actual alerted theater is now
    blacklisted."""
    r = repo()
    r.sync_watchlist([make_film(slug="pickpocket")])
    now = datetime.now(timezone.utc)
    r.save_status(
        "pickpocket",
        TicketStatus.ON_SALE,
        date(2026, 10, 7),
        ["AMC Mercado 20", "AMC Metreon 16"],  # live picture: Metreon joined later
        [],
        True,
        Tier.HOT,
        now,
        alerted_theaters=["AMC Mercado 20"],  # but only Mercado ever actually alerted
    )

    cleared = r.reapply_blacklist(frozenset({"AMC Mercado 20"}))

    assert [f.slug for f in cleared] == ["pickpocket"]


def test_save_config_round_trips_blacklisted_theaters():
    r = repo()
    config = Config(
        letterboxd_username="dave",
        zip_code="94158",
        hype_list_url="https://letterboxd.com/dave/list/hype/",
        blacklisted_theaters=frozenset({"AMC Mercado 20", "Cinemark Century San Mateo 12"}),
    )
    r.save_config(config)
    assert r.get_config() == config


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


def test_get_unnotified_only_returns_on_sale_without_notification():
    r = repo()
    r.sync_watchlist([make_film(slug="a"), make_film(slug="b")])
    r.save_match("a", make_listing(slug="a-slug"), date(2026, 1, 1))
    now = datetime.now(timezone.utc)
    r.save_status("a", TicketStatus.ON_SALE, date(2026, 1, 1), ["Theater A"], [], True, Tier.HOT, now, alerted_theaters=["Theater A"])
    r.save_status("b", TicketStatus.SHOWTIMES_ANNOUNCED, date(2026, 1, 1), [], ["Theater B"], False, Tier.HOT, now)

    pending = r.get_unnotified()
    assert [p.film.slug for p in pending] == ["a"]
    assert pending[0].alerted_theaters == ["Theater A"]


def test_mark_notified_removes_from_unnotified():
    r = repo()
    r.sync_watchlist([make_film(slug="a")])
    r.save_match("a", make_listing(), date(2026, 1, 1))
    r.save_status("a", TicketStatus.ON_SALE, date(2026, 1, 1), ["X"], [], True, Tier.HOT, datetime.now(timezone.utc))

    r.mark_notified("a")
    assert r.get_unnotified() == []
