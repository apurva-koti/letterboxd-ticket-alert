import requests

import modal_app


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
