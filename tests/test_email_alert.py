import email
from email import policy

import email_alert

from conftest import make_film
from domain import AlertedListing, PendingNotification


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
        listings=[AlertedListing(slug="digger-2026-245150", title="Digger", dates=[], theaters=["AMC Kabuki 8"])],
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
        listings=[AlertedListing(slug="digger-2026-245150", title="Digger", dates=[], theaters=["AMC Kabuki 8"])],
        poster_url="https://a.ltrbxd.com/resized/film-poster/1/1/3/2/6/8/8/1132688-digger-2026-0-600-0-900-crop.jpg",
    )
    _, _, html_body = email_alert.format_alert(notification)

    assert '<img src="https://a.ltrbxd.com/resized/film-poster/1/1/3/2/6/8/8/1132688-digger-2026-0-600-0-900-crop.jpg"' in html_body
    assert "<table" in html_body  # poster present - table layout for the two-column split


def test_render_html_omits_image_tag_when_no_poster():
    notification = PendingNotification(
        film=make_film(title="Digger", year=2026),
        listings=[AlertedListing(slug="digger-2026-245150", title="Digger", dates=[], theaters=["AMC Kabuki 8"])],
        poster_url=None,
    )
    _, _, html_body = email_alert.format_alert(notification)

    assert "<img" not in html_body
    assert "<table" not in html_body  # no poster - falls back to the single-column div layout


def test_format_alert_html_lists_every_theater():
    notification = PendingNotification(
        film=make_film(title="Primetime", year=2026),
        listings=[
            AlertedListing(
                slug="primetime", title="Primetime", dates=[], theaters=["AMC Kabuki 8", "AMC Metreon 16", "Landmark Opera Plaza"]
            )
        ],
    )
    _, _, html_body = email_alert.format_alert(notification)

    assert html_body.count("<li") == 3
    for theater in notification.alerted_theaters:
        assert theater in html_body


def test_each_listing_gets_its_own_section_dates_and_link():
    """Regression test: It's a Wonderful Life's email combined the 80th
    Anniversary listing's theaters (Metreon, ...) with the 1946 listing's
    (Lark) under one link to the 1946 listing - which only shows the Lark."""
    from datetime import date

    notification = PendingNotification(
        film=make_film(title="It's a Wonderful Life", year=1946),
        listings=[
            AlertedListing(
                slug="its-a-wonderful-life-80th-anniversary-244296",
                title="It's A Wonderful Life 80th Anniversary",
                dates=[date(2026, 12, d) for d in range(18, 25)],
                theaters=["AMC Bay Street 16", "AMC Metreon 16"],
            ),
            AlertedListing(
                slug="its-a-wonderful-life-1946-14197",
                title="It's a Wonderful Life",
                dates=[date(2026, 12, 13), date(2026, 12, 14), date(2026, 12, 23)],
                theaters=["Lark Theater"],
            ),
        ],
    )
    _, plain_body, html_body = email_alert.format_alert(notification)

    anniversary, original = plain_body.split("\n\n")[1:]
    assert anniversary == (
        "It's A Wonderful Life 80th Anniversary - Dec 18–24\n"
        "On sale at: AMC Bay Street 16, AMC Metreon 16\n"
        "Get tickets: https://www.fandango.com/its-a-wonderful-life-80th-anniversary-244296/movie-overview"
    )
    assert original == (
        "It's a Wonderful Life - Dec 13–14, 23\n"
        "On sale at: Lark Theater\n"
        "Get tickets: https://www.fandango.com/its-a-wonderful-life-1946-14197/movie-overview"
    )
    assert html_body.count("Get Tickets") == 2
    assert html_body.index("AMC Metreon 16") < html_body.index("its-a-wonderful-life-80th-anniversary-244296")
    assert html_body.index("its-a-wonderful-life-80th-anniversary-244296") < html_body.index("Lark Theater")


def test_describe_dates_compacts_runs_and_months():
    from datetime import date

    assert email_alert.describe_dates([date(2026, 12, 13)]) == "Dec 13"
    assert email_alert.describe_dates([date(2026, 12, d) for d in (13, 14, 23)]) == "Dec 13–14, 23"
    assert email_alert.describe_dates([date(2026, 11, 30), date(2026, 12, 1), date(2026, 12, 5)]) == "Nov 30–Dec 1, 5"
    assert email_alert.describe_dates([]) == ""


def test_listing_without_a_fandango_link_falls_back_to_the_film_page():
    notification = PendingNotification(
        film=make_film(title="Digger", year=2026, url="https://letterboxd.com/film/digger-2026/"),
        listings=[AlertedListing(slug="", title="Digger", dates=[], theaters=["AMC Kabuki 8"])],
    )
    _, plain_body, html_body = email_alert.format_alert(notification)
    assert "Get tickets: https://letterboxd.com/film/digger-2026/" in plain_body
    assert 'href="https://letterboxd.com/film/digger-2026/"' in html_body


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


def _notification(context):
    from domain import AlertContext  # noqa: F401

    return PendingNotification(
        film=make_film(title="Werwulf", year=2026),
        listings=[AlertedListing(slug="werwulf-2026-246368", title="Werwulf", dates=[], theaters=["Alamo Drafthouse Cinema New Mission"])],
        context=context,
    )


def test_first_alert_without_a_preferred_theater_says_which_ones_will_email_again():
    from domain import AlertContext

    context = AlertContext(kind="first", preferred_missing=["AMC Kabuki 8", "AMC Metreon 16"])
    _, plain_body, html_body = email_alert.format_alert(_notification(context))
    assert "None of your preferred theaters have tickets yet." in plain_body
    assert "You'll get one more email if tickets reach any of: AMC Kabuki 8, AMC Metreon 16." in plain_body
    assert "You&#x27;ll get one more email if tickets reach any of: AMC Kabuki 8, AMC Metreon 16." in html_body


def test_first_alert_with_a_preferred_theater_says_no_more_emails():
    from domain import AlertContext

    context = AlertContext(kind="first", preferred_on_sale=["AMC Metreon 16"], preferred_missing=["AMC Kabuki 8"])
    _, plain_body, _ = email_alert.format_alert(_notification(context))
    assert "Includes your preferred theater(s): AMC Metreon 16." in plain_body
    assert "You won't get another email for this film while it stays on sale." in plain_body
    assert "one more email" not in plain_body


def test_preferred_alert_mentions_the_earlier_email():
    from datetime import datetime, timezone

    from domain import AlertContext

    context = AlertContext(
        kind="preferred",
        preferred_on_sale=["AMC Metreon 16"],
        previous_theaters=["Alamo Drafthouse Cinema New Mission"],
        previous_alerted_at=datetime(2026, 11, 3, 14, 15, tzinfo=timezone.utc),
    )
    _, plain_body, _ = email_alert.format_alert(_notification(context))
    assert "Now on sale at your preferred theater(s): AMC Metreon 16." in plain_body
    assert "Your earlier email (Nov 3) only listed: Alamo Drafthouse Cinema New Mission." in plain_body
    assert "This is the last email for this film while it stays on sale." in plain_body


def test_no_preferred_theaters_set_says_no_more_emails():
    from domain import AlertContext

    _, plain_body, _ = email_alert.format_alert(_notification(AlertContext(kind="first")))
    assert "You won't get another email for this film while it stays on sale." in plain_body
    assert "preferred" not in plain_body


def test_old_alert_without_context_has_no_note():
    _, plain_body, html_body = email_alert.format_alert(_notification(None))
    assert "another email" not in plain_body and "re-release" not in html_body
