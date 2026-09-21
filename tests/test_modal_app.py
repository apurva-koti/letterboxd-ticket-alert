import requests

import modal_app


def test_is_transient_network_error_true_for_timeout():
    assert modal_app._is_transient_network_error(requests.exceptions.ReadTimeout("timed out")) is True


def test_is_transient_network_error_true_for_connection_error():
    assert modal_app._is_transient_network_error(requests.exceptions.ConnectionError("refused")) is True


def test_is_transient_network_error_false_for_http_error():
    """A real 4xx/5xx (e.g. a site structure change, a hard block) is not a
    plain connectivity hiccup - it's worth a human actually looking at, so
    this must still email."""
    assert modal_app._is_transient_network_error(requests.exceptions.HTTPError("404")) is False


def test_is_transient_network_error_false_for_unrelated_exception():
    assert modal_app._is_transient_network_error(KeyError("boom")) is False
