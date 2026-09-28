import why
from repository import FilmRepository

from conftest import make_film


def test_find_film_exact_slug_match():
    repo = FilmRepository.connect(":memory:")
    repo.sync_watchlist([make_film(slug="digger-2026", title="Digger")])
    assert why.find_film(repo, "digger-2026").slug == "digger-2026"


def test_find_film_fuzzy_title_match():
    repo = FilmRepository.connect(":memory:")
    repo.sync_watchlist([make_film(slug="dune-part-three", title="Dune: Part Three")])
    assert why.find_film(repo, "Dune Part Three").slug == "dune-part-three"


def test_find_film_returns_none_when_nothing_close_enough():
    repo = FilmRepository.connect(":memory:")
    repo.sync_watchlist([make_film(slug="digger-2026", title="Digger")])
    assert why.find_film(repo, "Completely Unrelated Film Title") is None


def test_find_film_returns_none_on_empty_db():
    repo = FilmRepository.connect(":memory:")
    assert why.find_film(repo, "anything") is None
