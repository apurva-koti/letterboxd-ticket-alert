from datetime import date, datetime, timezone

from fastapi.testclient import TestClient

from domain import Config, Film, Tier, TicketStatus
from repository import FilmRepository
from web_app import create_app


def _now():
    return datetime.now(timezone.utc)


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
    assert resp.headers["location"] == "/config/secret123?saved=1&cleared="

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


def test_saving_a_blacklist_clears_now_worthless_alerts(caplog):
    client, repo = _client()
    repo.sync_watchlist([Film(slug="singin-in-the-rain", title="Singin' in the Rain", year=1952, url=None)])
    repo.save_status(
        "singin-in-the-rain",
        TicketStatus.ON_SALE,
        date(2026, 10, 7),
        ["The New Parkway"],
        [],
        True,
        Tier.RETIRED,
        _now(),
        alerted_theaters=["The New Parkway"],
    )

    with caplog.at_level("INFO"):
        resp = client.post(
            "/config/secret123",
            data={"letterboxd_username": "dave", "zip_code": "94158", "blacklisted_theaters": "The New Parkway"},
            follow_redirects=False,
        )
    assert resp.headers["location"] == "/config/secret123?saved=1&cleared=Singin%27%20in%20the%20Rain"
    assert "cleared 1 now-worthless alert(s): Singin' in the Rain" in caplog.text

    resp = client.get(resp.headers["location"])
    assert "Singin&#x27; in the Rain" in resp.text
    assert repo.get("singin-in-the-rain").status == TicketStatus.NONE


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
