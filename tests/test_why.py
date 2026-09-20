import state
import why

from conftest import make_film


def test_find_film_exact_slug_match():
    conn = state.connect(":memory:")
    state.sync_watchlist(conn, [make_film(slug="digger-2026", title="Digger")])

    row = why.find_film(conn, "digger-2026")
    assert row["slug"] == "digger-2026"


def test_find_film_fuzzy_title_match():
    conn = state.connect(":memory:")
    state.sync_watchlist(conn, [make_film(slug="dune-part-three", title="Dune: Part Three")])

    row = why.find_film(conn, "Dune Part Three")  # no colon - not an exact title match
    assert row["slug"] == "dune-part-three"


def test_find_film_returns_none_when_nothing_close_enough():
    conn = state.connect(":memory:")
    state.sync_watchlist(conn, [make_film(slug="digger-2026", title="Digger")])

    assert why.find_film(conn, "Completely Unrelated Film Title") is None


def test_find_film_returns_none_on_empty_db():
    conn = state.connect(":memory:")
    assert why.find_film(conn, "anything") is None
