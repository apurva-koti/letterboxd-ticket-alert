import requests

import modal_app

from conftest import make_film


def _http_error(status_code):
    response = requests.Response()
    response.status_code = status_code
    return requests.exceptions.HTTPError(f"{status_code} error", response=response)


def test_is_transient_failure_true_for_timeout():
    assert modal_app._is_transient_failure(requests.exceptions.ReadTimeout("timed out")) is True


def test_is_transient_failure_true_for_connection_error():
    assert modal_app._is_transient_failure(requests.exceptions.ConnectionError("refused")) is True


def test_is_transient_failure_true_for_server_error():
    """Regression test for a real production case: a plain Letterboxd 500
    still emailed under the original version of this check, which only
    covered Timeout/ConnectionError - a 5xx is the site's own server having a
    problem, just as self-healing and non-actionable as a timeout."""
    assert modal_app._is_transient_failure(_http_error(500)) is True
    assert modal_app._is_transient_failure(_http_error(503)) is True


def test_is_transient_failure_false_for_client_error():
    """A 4xx (blocked, a URL format that changed) usually means something on
    our end needs fixing - unlike a 5xx, this should still email."""
    assert modal_app._is_transient_failure(_http_error(403)) is False
    assert modal_app._is_transient_failure(_http_error(404)) is False


def test_is_transient_failure_false_for_unrelated_exception():
    assert modal_app._is_transient_failure(KeyError("boom")) is False


def _health_setup(monkeypatch, send):
    from datetime import datetime, timedelta, timezone

    from domain import RunResult
    from health import HealthState
    from repository import FilmRepository

    import email_alert

    sent = []
    monkeypatch.setattr(email_alert, "send_email", lambda subject, body, html=None: (send(), sent.append(subject)))
    repo = FilmRepository.connect(":memory:")
    t0 = datetime(2026, 11, 3, 7, 45, tzinfo=timezone.utc)
    repo.save_health(HealthState(failing_since=t0, last_problem="x"))
    broken = RunResult(failed=["Werwulf: UnrecognizedSearchPage: ..."])
    return repo, broken, t0 + timedelta(hours=6), sent


def test_check_health_emails_once_after_six_hours(monkeypatch):
    repo, broken, now, sent = _health_setup(monkeypatch, lambda: None)
    modal_app._check_health(repo, broken, now, "logs/2026-11-03/13-45.log")
    modal_app._check_health(repo, broken, now, "logs/2026-11-03/14-00.log")
    assert len(sent) == 1 and "may be missing alerts" in sent[0]
    assert repo.get_health().alerted_at == now


def test_check_health_retries_the_email_if_sending_fails(monkeypatch):
    def fail():
        raise RuntimeError("smtp down")

    repo, broken, now, sent = _health_setup(monkeypatch, fail)
    modal_app._check_health(repo, broken, now, None)
    assert repo.get_health().alerted_at is None  # not marked sent - next run tries again


def test_check_health_tracks_letterboxd_separately_from_fandango(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from domain import RunResult
    from health import HealthState
    from repository import FilmRepository

    import email_alert

    sent = []
    monkeypatch.setattr(email_alert, "send_email", lambda subject, body, html=None: sent.append(subject))
    repo = FilmRepository.connect(":memory:")
    t0 = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)
    repo.save_health(HealthState(failing_since=t0, last_problem="watchlist: 404 on page 3"), "letterboxd")

    # Fandango checks fine, Letterboxd still failing 6h later: only the Letterboxd email.
    result = RunResult(checked=[make_film()], letterboxd_problems=["watchlist: LetterboxdPageError: 404 on page 3"])
    modal_app._check_health(repo, result, t0 + timedelta(hours=6), None)

    assert sent == ["⚠️ Ticket alerts: your Letterboxd lists aren't loading"]
    assert repo.get_health("fandango") == HealthState()
    assert repo.get_health("letterboxd").alerted_at == t0 + timedelta(hours=6)
