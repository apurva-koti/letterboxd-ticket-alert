from datetime import date, datetime, timedelta, timezone

import pytest

import matcher
import tracker as tracker_module
from domain import LetterboxdRelease, Tier, TicketStatus
from repository import FilmRepository
from tracker import Tracker

from conftest import make_film, make_listing, make_showtime_check


def test_describe_includes_release_date_and_tier_meaning():
    desc = tracker_module._describe(Tier.HOT, date(2026, 10, 2), date(2026, 9, 20))
    assert "2026-10-02" in desc
    assert "12d away" in desc
    assert "hot" in desc
    assert "3h" in desc


def test_describe_humanizes_large_spans():
    desc = tracker_module._describe(Tier.RETIRED, date(1986, 5, 16), date(2026, 9, 20))
    assert "~40y ago" in desc


def test_describe_with_no_anchor_still_names_the_tier():
    assert tracker_module._describe(Tier.UNKNOWN, None, date(2026, 9, 20)) == tracker_module.TIER_MEANINGS[Tier.UNKNOWN]


class FakeLetterboxd:
    def __init__(self, watchlist=None, hype=None, releases=None, fail_watchlist=False):
        self.watchlist = watchlist or []
        self.hype = hype or []
        self.releases = releases or {}
        self.fail_watchlist = fail_watchlist

    def fetch_watchlist(self, username):
        if self.fail_watchlist:
            raise Exception("boom")
        return self.watchlist

    def fetch_list(self, url):
        return self.hype

    def fetch_release(self, slug):
        return self.releases.get(slug, LetterboxdRelease(director=None, genres=[], poster_url=None, us_dates=[]))


class FakeFandango:
    def __init__(self, showtimes=None):
        self.showtimes = showtimes or {}
        self.calls = []

    def check_showtimes(self, fandango_id, slug, zip_code):
        self.calls.append(fandango_id)
        return self.showtimes.get(fandango_id)


@pytest.fixture
def repo():
    return FilmRepository.connect(":memory:")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(tracker_module.time, "sleep", lambda s: None)


def _install_fandango(monkeypatch, fake):
    monkeypatch.setattr(tracker_module, "FandangoClient", lambda: fake)


def test_run_matches_and_checks_a_new_upcoming_film(monkeypatch, repo):
    film = make_film(slug="digger-2026", title="Digger", year=2026)
    release = LetterboxdRelease(director="Some Director", genres=[], poster_url=None, us_dates=[date(2026, 10, 2)])
    lb = FakeLetterboxd(watchlist=[film], releases={"digger-2026": release})

    listing = make_listing(fandango_id="245150")
    monkeypatch.setattr(matcher, "find_anchored_match", lambda *a, **k: listing)
    fandango = FakeFandango(showtimes={"245150": make_showtime_check(status=TicketStatus.ON_SALE)})
    _install_fandango(monkeypatch, fandango)

    result = Tracker(repo, lb).run("user", "94158")

    assert film in result.checked
    assert film in result.alerted
    tracked = repo.get("digger-2026")
    assert tracked.fandango.fandango_id == "245150"
    assert tracked.status == TicketStatus.ON_SALE


def test_alert_fires_only_on_transition_into_on_sale(monkeypatch, repo):
    film = make_film(slug="digger-2026")
    release = LetterboxdRelease(director=None, genres=[], poster_url=None, us_dates=[date(2026, 10, 2)])
    lb = FakeLetterboxd(watchlist=[film], releases={"digger-2026": release})
    listing = make_listing(fandango_id="245150")
    monkeypatch.setattr(matcher, "find_anchored_match", lambda *a, **k: listing)

    fandango = FakeFandango(showtimes={"245150": make_showtime_check(status=TicketStatus.ON_SALE)})
    _install_fandango(monkeypatch, fandango)
    Tracker(repo, lb).run("user", "94158")

    conn = repo.conn
    conn.execute("UPDATE ticket_status SET next_check_at = ?", ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),))
    conn.commit()
    result = Tracker(repo, lb).run("user", "94158")

    assert film not in result.alerted  # still on_sale, not a new transition


def test_documentary_is_excluded_and_never_matched(monkeypatch, repo):
    film = make_film(slug="nuisance-bear")
    release = LetterboxdRelease(director="A Director", genres=["documentary"], poster_url=None, us_dates=[date(2026, 1, 1)])
    lb = FakeLetterboxd(watchlist=[film], releases={"nuisance-bear": release})
    called = []
    monkeypatch.setattr(matcher, "find_anchored_match", lambda *a, **k: called.append(1))
    _install_fandango(monkeypatch, FakeFandango())

    Tracker(repo, lb).run("user", "94158")

    assert called == []
    assert repo.get("nuisance-bear").excluded_reason == "documentary"


def test_unmatched_film_is_rescheduled_not_starved(monkeypatch, repo):
    """Regression test: an unmatched film used to never get a next_check_at,
    making it permanently "due" and able to starve every film behind it once
    enough of them accumulated."""
    film = make_film(slug="obscure-film")
    release = LetterboxdRelease(director=None, genres=[], poster_url=None, us_dates=[date.today() + timedelta(days=30)])
    lb = FakeLetterboxd(watchlist=[film], releases={"obscure-film": release})
    monkeypatch.setattr(matcher, "find_anchored_match", lambda *a, **k: None)
    _install_fandango(monkeypatch, FakeFandango())

    Tracker(repo, lb).run("user", "94158")

    tracked = repo.get("obscure-film")
    assert tracked.fandango is None
    assert tracked.next_check_at is not None
    assert tracked.next_check_at > datetime.now(timezone.utc)


def test_retired_film_drops_fandango_binding_and_resets_status(monkeypatch, repo):
    film = make_film(slug="old-film")
    repo.sync_watchlist([film])
    repo.save_match("old-film", make_listing(), date.today() - timedelta(days=100))
    repo.save_status("old-film", TicketStatus.NONE, None, [], [], False, Tier.RECENT, datetime.now(timezone.utc) - timedelta(hours=1))

    lb = FakeLetterboxd(watchlist=[film])
    _install_fandango(monkeypatch, FakeFandango())
    Tracker(repo, lb).run("user", "94158")

    tracked = repo.get("old-film")
    assert tracked.fandango is None
    assert tracked.tier.value == "retired"


def test_retired_film_picks_up_a_new_anchor_and_rematches(monkeypatch, repo):
    """Moonlight-style: a retired film's Letterboxd anchor moves to a new,
    upcoming re-release date - should rebind to a fresh Fandango listing and
    resume normal polling."""
    film = make_film(slug="moonlight-2016", title="Moonlight")
    repo.sync_watchlist([film])
    repo.reset_for_retirement("moonlight-2016", datetime.now(timezone.utc) - timedelta(hours=1))
    conn = repo.conn
    conn.execute(
        "INSERT INTO fandango_matches (letterboxd_slug, release_date, release_date_source, checked_at) VALUES (?, ?, 'letterboxd', ?)",
        ("moonlight-2016", "2016-10-21", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()

    new_anchor = date.today() + timedelta(days=10)
    release = LetterboxdRelease(director="Barry Jenkins", genres=[], poster_url=None, us_dates=[date(2016, 10, 21), new_anchor])
    lb = FakeLetterboxd(watchlist=[film], releases={"moonlight-2016": release})

    listing = make_listing(fandango_id="999", slug="moonlight-anniversary")
    monkeypatch.setattr(matcher, "find_anchored_match", lambda *a, **k: listing)
    fandango = FakeFandango(showtimes={"999": make_showtime_check(status=TicketStatus.ON_SALE)})
    _install_fandango(monkeypatch, fandango)

    result = Tracker(repo, lb).run("user", "94158")

    tracked = repo.get("moonlight-2016")
    assert tracked.fandango.fandango_id == "999"
    assert film in result.alerted  # fresh binding, none -> on_sale


def test_run_caps_films_processed_per_run(monkeypatch, repo):
    films = [make_film(slug=f"film-{i}", title=f"Film {i}") for i in range(tracker_module.MAX_PROCESSED_PER_RUN + 5)]
    lb = FakeLetterboxd(watchlist=films)
    monkeypatch.setattr(matcher, "find_anchored_match", lambda *a, **k: None)
    monkeypatch.setattr(matcher, "find_match_by_title", lambda *a, **k: None)
    _install_fandango(monkeypatch, FakeFandango())

    Tracker(repo, lb).run("user", "94158")

    processed = sum(1 for f in films if repo.get(f.slug).next_check_at is not None)
    assert processed == tracker_module.MAX_PROCESSED_PER_RUN


def test_hype_film_bypasses_the_cap(monkeypatch, repo):
    films = [make_film(slug=f"film-{i}", title=f"Film {i}") for i in range(tracker_module.MAX_PROCESSED_PER_RUN)]
    hype_film = make_film(slug="dune-part-three", title="Dune: Part Three")
    lb = FakeLetterboxd(watchlist=films, hype=[hype_film])
    monkeypatch.setattr(matcher, "find_anchored_match", lambda *a, **k: None)
    monkeypatch.setattr(matcher, "find_match_by_title", lambda *a, **k: make_listing(fandango_id="1"))
    _install_fandango(monkeypatch, FakeFandango(showtimes={"1": None}))

    Tracker(repo, lb).run("user", "94158", hype_list_url="https://letterboxd.com/x/list/hype/")

    assert repo.get("dune-part-three").fandango is not None


def test_watchlist_fetch_failure_does_not_crash_run(monkeypatch, repo):
    film = make_film(slug="known-film")
    repo.sync_watchlist([film])
    lb = FakeLetterboxd(fail_watchlist=True)
    _install_fandango(monkeypatch, FakeFandango())

    result = Tracker(repo, lb).run("user", "94158")
    assert result.added == []
    assert result.removed == []


def test_fandango_session_failure_still_syncs_watchlist(monkeypatch, repo):
    film = make_film(slug="new-film")
    lb = FakeLetterboxd(watchlist=[film])
    monkeypatch.setattr(tracker_module, "FandangoClient", lambda: (_ for _ in ()).throw(Exception("down")))

    result = Tracker(repo, lb).run("user", "94158")
    assert film in result.added
    assert result.checked == []


def test_one_films_failure_does_not_block_the_rest(monkeypatch, repo):
    broken = make_film(slug="broken")
    fine = make_film(slug="fine")
    release = LetterboxdRelease(director=None, genres=[], poster_url=None, us_dates=[date.today() + timedelta(days=10)])
    lb = FakeLetterboxd(watchlist=[broken, fine], releases={"fine": release})

    def flaky_fetch_release(slug):
        if slug == "broken":
            raise Exception("boom")
        return release

    lb.fetch_release = flaky_fetch_release
    monkeypatch.setattr(matcher, "find_anchored_match", lambda *a, **k: make_listing(fandango_id="1"))
    fandango = FakeFandango(showtimes={"1": make_showtime_check(status=TicketStatus.NONE)})
    _install_fandango(monkeypatch, fandango)

    result = Tracker(repo, lb).run("user", "94158")
    assert fine in result.checked
    assert broken not in result.checked
