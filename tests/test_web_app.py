from fastapi.testclient import TestClient

from domain import Config
from repository import FilmRepository
from web_app import create_app


def _client(token="secret123", on_saved=lambda: None, on_read=lambda: None, seed_defaults=None):
    repo = FilmRepository.connect(":memory:")
    app = create_app(repo, token, on_saved=on_saved, on_read=on_read, seed_defaults=seed_defaults)
    return TestClient(app), repo


def test_wrong_token_is_404_not_a_login_prompt():
    client, _ = _client()
    assert client.get("/config/wrong").status_code == 404
    assert client.post("/config/wrong", data={"letterboxd_username": "x", "zip_code": "1"}).status_code == 404


def test_first_load_shows_blank_form():
    client, _ = _client()
    resp = client.get("/config/secret123")
    assert resp.status_code == 200
    assert "Letterboxd username" in resp.text


def test_first_load_seeds_from_defaults_instead_of_blank():
    """Regression test for a real production incident: loading this page
    before the scheduler's first-ever run seeded the config row empty,
    since the form passed no defaults of its own."""
    defaults = Config(letterboxd_username="dave", zip_code="94158", hype_list_url=None, blacklisted_theaters=frozenset())
    client, repo = _client(seed_defaults=defaults)
    resp = client.get("/config/secret123")
    assert 'value="dave"' in resp.text
    assert repo.get_config() == defaults


def test_saving_round_trips_through_the_form():
    client, repo = _client()
    resp = client.post(
        "/config/secret123",
        data={
            "letterboxd_username": "dave",
            "zip_code": "94158",
            "hype_list_url": "https://letterboxd.com/dave/list/hype/",
            "blacklisted_theaters": "AMC Mercado 20, Cinemark Century San Mateo 12",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/config/secret123?saved=1"

    cfg = repo.get_config()
    assert cfg.letterboxd_username == "dave"
    assert cfg.blacklisted_theaters == {"AMC Mercado 20", "Cinemark Century San Mateo 12"}

    resp = client.get("/config/secret123")
    assert 'value="dave"' in resp.text
    assert "AMC Mercado 20, Cinemark Century San Mateo 12" in resp.text


def test_saving_blank_hype_list_clears_it_not_empty_string():
    client, repo = _client()
    client.post("/config/secret123", data={"letterboxd_username": "dave", "zip_code": "94158", "hype_list_url": "  "})
    assert repo.get_config().hype_list_url is None


def test_save_and_read_hooks_fire():
    """on_saved is wired to the Volume's commit on Modal, so a write is
    durable and visible to the scheduler's separate container. on_read is
    a no-op there in practice (see web_app.create_app's docstring) but
    stays a generic hook here."""
    reads, saves = [], []
    client, _ = _client(on_saved=lambda: saves.append(1), on_read=lambda: reads.append(1))

    client.get("/config/secret123")
    client.post("/config/secret123", data={"letterboxd_username": "dave", "zip_code": "94158"}, follow_redirects=False)

    assert reads == [1]
    assert saves == [1]
