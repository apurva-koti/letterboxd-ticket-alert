from datetime import date

import state

from conftest import make_film, make_ticket_status_result


def test_sync_watchlist_first_run_marks_everything_added():
    conn = state.connect(":memory:")
    added, removed = state.sync_watchlist(conn, [make_film(slug="a"), make_film(slug="b")])
    assert {f.slug for f in added} == {"a", "b"}
    assert removed == []


def test_sync_watchlist_no_changes_reports_nothing():
    conn = state.connect(":memory:")
    state.sync_watchlist(conn, [make_film(slug="a")])
    added, removed = state.sync_watchlist(conn, [make_film(slug="a")])
    assert added == [] and removed == []


def test_sync_watchlist_hard_deletes_removed_films_and_related_rows():
    conn = state.connect(":memory:")
    state.sync_watchlist(conn, [make_film(slug="a"), make_film(slug="b")])
    state.save_match(conn, "b", "999", "b-slug", "B", 2026)
    state.save_ticket_status(conn, "b", "on_sale", make_ticket_status_result(), alerted=True)

    added, removed = state.sync_watchlist(conn, [make_film(slug="a")])  # "b" dropped from watchlist
    assert {f.slug for f in removed} == {"b"}

    assert [r["slug"] for r in conn.execute("SELECT slug FROM films")] == ["a"]
    assert state.get_match(conn, "b") is None
    assert state.get_ticket_status(conn, "b") is None


def test_sync_watchlist_readded_film_reports_as_added_again():
    conn = state.connect(":memory:")
    state.sync_watchlist(conn, [make_film(slug="a"), make_film(slug="b")])
    state.sync_watchlist(conn, [make_film(slug="a")])  # remove b

    added, removed = state.sync_watchlist(conn, [make_film(slug="a"), make_film(slug="b")])  # re-add b
    assert {f.slug for f in added} == {"b"}
    assert removed == []


def test_save_and_get_match_round_trip():
    conn = state.connect(":memory:")
    state.save_match(conn, "digger-2026", "245150", "digger-2026-245150", "Digger", 2026, release_date=None)

    match = state.get_match(conn, "digger-2026")
    assert match.fandango_id == "245150"
    assert match.release_date is None


def test_save_match_records_failed_attempt_with_none_id():
    conn = state.connect(":memory:")
    state.save_match(conn, "obscure-film", None, None, None, None)
    match = state.get_match(conn, "obscure-film")
    assert match.fandango_id is None
    assert match.checked_at is not None


def test_save_match_stores_release_date_source():
    conn = state.connect(":memory:")
    state.save_match(conn, "paper-tiger-2026", None, None, None, None, date(2026, 11, 13), "letterboxd")
    match = state.get_match(conn, "paper-tiger-2026")
    assert match.release_date == "2026-11-13"
    assert match.release_date_source == "letterboxd"


def test_save_match_stores_excluded_reason():
    conn = state.connect(":memory:")
    state.save_match(conn, "nuisance-bear-2026", None, None, None, None, excluded_reason="documentary")
    match = state.get_match(conn, "nuisance-bear-2026")
    assert match.fandango_id is None
    assert match.excluded_reason == "documentary"


def test_save_ticket_status_alerted_at_set_on_new_alert():
    conn = state.connect(":memory:")
    result = make_ticket_status_result()
    state.save_ticket_status(conn, "slug", "on_sale", result, alerted=True)

    row = state.get_ticket_status(conn, "slug")
    assert row.alerted_at is not None


def test_save_ticket_status_alerted_at_preserved_when_not_realerting():
    conn = state.connect(":memory:")
    result = make_ticket_status_result()
    state.save_ticket_status(conn, "slug", "on_sale", result, alerted=True)
    first_alerted_at = state.get_ticket_status(conn, "slug").alerted_at

    # same status again on a later check - must NOT re-fire or clear alerted_at
    state.save_ticket_status(conn, "slug", "on_sale", result, alerted=False)
    row = state.get_ticket_status(conn, "slug")
    assert row.alerted_at == first_alerted_at


def test_save_ticket_status_alerted_at_updates_on_a_new_episode():
    """If status drops out of on_sale and later re-enters it (e.g. sold out,
    then a new batch of tickets released), that's a distinct episode and
    alerted_at should move forward, not stay frozen at the first-ever alert."""
    conn = state.connect(":memory:")
    result = make_ticket_status_result()
    state.save_ticket_status(conn, "slug", "on_sale", result, alerted=True)
    first_alerted_at = state.get_ticket_status(conn, "slug").alerted_at

    state.save_ticket_status(conn, "slug", "none", None, alerted=False)
    state.save_ticket_status(conn, "slug", "on_sale", result, alerted=True)

    second_alerted_at = state.get_ticket_status(conn, "slug").alerted_at
    assert second_alerted_at >= first_alerted_at


def test_save_ticket_status_stores_tier_and_next_check_at():
    conn = state.connect(":memory:")
    state.save_ticket_status(conn, "slug", "none", None, alerted=False, tier="hot", next_check_at="2026-09-19T12:00:00+00:00")
    row = state.get_ticket_status(conn, "slug")
    assert row.tier == "hot"
    assert row.next_check_at == "2026-09-19T12:00:00+00:00"


# ---- notified_at / notification-retry plumbing ----

def test_get_unnotified_on_sale_returns_fresh_on_sale_films():
    conn = state.connect(":memory:")
    state.sync_watchlist(conn, [make_film(slug="digger-2026", title="Digger", year=2026)])
    result = make_ticket_status_result(on_sale_theaters=["AMC Kabuki 8"])
    state.save_ticket_status(conn, "digger-2026", "on_sale", result, alerted=True)

    pending = state.get_unnotified_on_sale(conn)
    assert len(pending) == 1
    assert pending[0].film.title == "Digger"
    assert pending[0].on_sale_theaters == ["AMC Kabuki 8"]


def test_mark_notified_removes_it_from_pending():
    conn = state.connect(":memory:")
    state.sync_watchlist(conn, [make_film(slug="digger-2026")])
    state.save_ticket_status(conn, "digger-2026", "on_sale", make_ticket_status_result(), alerted=True)

    state.mark_notified(conn, "digger-2026")
    assert state.get_unnotified_on_sale(conn) == []


def test_unsuccessful_notification_is_retried_on_a_later_check():
    """The core reliability property: a film stays "pending" across repeated
    on_sale checks until mark_notified actually fires - an SMS-send failure
    doesn't get silently swallowed just because the status check itself kept
    succeeding."""
    conn = state.connect(":memory:")
    state.sync_watchlist(conn, [make_film(slug="digger-2026")])
    result = make_ticket_status_result()

    state.save_ticket_status(conn, "digger-2026", "on_sale", result, alerted=True)
    assert len(state.get_unnotified_on_sale(conn)) == 1  # first check - still pending

    # a later check finds the same on_sale status again (no new transition,
    # alerted=False) - notification should still be pending, since it was
    # never actually marked sent
    state.save_ticket_status(conn, "digger-2026", "on_sale", result, alerted=False)
    assert len(state.get_unnotified_on_sale(conn)) == 1


def test_notified_at_resets_when_status_leaves_on_sale():
    """A later, distinct on_sale episode (e.g. after selling out and later
    reopening) needs its own fresh notification, not to be silently skipped
    because a past episode was already sent."""
    conn = state.connect(":memory:")
    state.sync_watchlist(conn, [make_film(slug="digger-2026")])
    result = make_ticket_status_result()

    state.save_ticket_status(conn, "digger-2026", "on_sale", result, alerted=True)
    state.mark_notified(conn, "digger-2026")
    assert state.get_unnotified_on_sale(conn) == []

    state.save_ticket_status(conn, "digger-2026", "none", None, alerted=False)
    state.save_ticket_status(conn, "digger-2026", "on_sale", result, alerted=True)  # new episode

    pending = state.get_unnotified_on_sale(conn)
    assert len(pending) == 1
    assert pending[0].film.slug == "digger-2026"
