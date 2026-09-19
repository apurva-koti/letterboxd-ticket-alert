import email
from email import policy

import email_alert

from conftest import make_film
from models import PendingNotification


def _decoded_parts(message_string):
    """Parses a raw MIME message and returns {content_type: decoded_text} -
    content is base64-encoded for UTF-8 parts, so tests need to actually
    decode rather than substring-match the raw wire format."""
    parsed = email.message_from_string(message_string, policy=policy.default)
    if parsed.is_multipart():
        return {part.get_content_type(): part.get_content() for part in parsed.iter_parts()}
    return {parsed.get_content_type(): parsed.get_content()}


def test_format_alert_subject_is_loud_and_includes_title_year():
    notification = PendingNotification(
        film=make_film(title="Digger", year=2026, url="https://letterboxd.com/film/digger-2026/"),
        on_sale_theaters=["AMC Kabuki 8"],
        ticket_url="https://www.fandango.com/digger-2026-245150/movie-overview",
    )
    subject, plain_body, html_body = email_alert.format_alert(notification)

    assert "TICKETS ON SALE" in subject
    assert "Digger" in subject
    assert "2026" in subject

    assert "AMC Kabuki 8" in plain_body
    assert "https://www.fandango.com/digger-2026-245150/movie-overview" in plain_body

    assert "Digger" in html_body
    assert "AMC Kabuki 8" in html_body
    assert "https://www.fandango.com/digger-2026-245150/movie-overview" in html_body
    assert "<html" not in html_body.lower()  # a fragment, not a full document - MIMEMultipart wraps it


def test_render_html_includes_poster_image_when_present():
    notification = PendingNotification(
        film=make_film(title="Digger", year=2026),
        on_sale_theaters=["AMC Kabuki 8"],
        ticket_url="https://www.fandango.com/digger-2026-245150/movie-overview",
        poster_url="https://a.ltrbxd.com/resized/film-poster/1/1/3/2/6/8/8/1132688-digger-2026-0-600-0-900-crop.jpg",
    )
    _, _, html_body = email_alert.format_alert(notification)

    assert '<img src="https://a.ltrbxd.com/resized/film-poster/1/1/3/2/6/8/8/1132688-digger-2026-0-600-0-900-crop.jpg"' in html_body
    assert "<table" in html_body  # poster present - table layout for the two-column split


def test_render_html_omits_image_tag_when_no_poster():
    notification = PendingNotification(
        film=make_film(title="Digger", year=2026),
        on_sale_theaters=["AMC Kabuki 8"],
        ticket_url="https://www.fandango.com/digger-2026-245150/movie-overview",
        poster_url=None,
    )
    _, _, html_body = email_alert.format_alert(notification)

    assert "<img" not in html_body
    assert "<table" not in html_body  # no poster - falls back to the single-column div layout


def test_format_alert_html_lists_every_theater():
    notification = PendingNotification(
        film=make_film(title="Primetime", year=2026),
        on_sale_theaters=["AMC Kabuki 8", "AMC Metreon 16", "Landmark Opera Plaza"],
        ticket_url="https://www.fandango.com/primetime/movie-overview",
    )
    _, _, html_body = email_alert.format_alert(notification)

    assert html_body.count("<li") == 3
    for theater in notification.on_sale_theaters:
        assert theater in html_body


def test_send_email_logs_in_and_sends_multipart_from_and_to_same_address(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "user@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")

    calls = {}

    class FakeSMTP:
        def __init__(self, host, port):
            calls["host"] = host
            calls["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def login(self, address, password):
            calls["login"] = (address, password)

        def sendmail(self, from_addr, to_addrs, message_string):
            calls["from_addr"] = from_addr
            calls["to_addrs"] = to_addrs
            calls["message_string"] = message_string

    monkeypatch.setattr(email_alert.smtplib, "SMTP_SSL", FakeSMTP)
    email_alert.send_email("Test Subject", "Plain fallback", "<div>HTML version</div>")

    assert calls["host"] == "smtp.gmail.com"
    assert calls["login"] == ("user@gmail.com", "abcd efgh ijkl mnop")
    assert calls["from_addr"] == "user@gmail.com"
    assert calls["to_addrs"] == ["user@gmail.com"]
    assert "Test Subject" in calls["message_string"]

    parts = _decoded_parts(calls["message_string"])
    assert "Plain fallback" in parts["text/plain"]
    assert "HTML version" in parts["text/html"]


def test_send_email_without_html_falls_back_to_plain(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "user@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")

    calls = {}

    class FakeSMTP:
        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def login(self, address, password):
            pass

        def sendmail(self, from_addr, to_addrs, message_string):
            calls["message_string"] = message_string

    monkeypatch.setattr(email_alert.smtplib, "SMTP_SSL", FakeSMTP)
    email_alert.send_email("Plain Only", "Just plain text")

    parts = _decoded_parts(calls["message_string"])
    assert "Just plain text" in parts["text/plain"]
