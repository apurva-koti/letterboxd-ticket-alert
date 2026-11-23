"""Sends email alerts via Gmail SMTP, authenticated with an App Password (a
Google Account setting - myaccount.google.com/apppasswords - not a
third-party signup or business form). Sends from and to the same address, so
only one Gmail account is needed.

Named email_alert.py, not email.py, so it doesn't shadow the stdlib `email`
package that MIMEText/MIMEMultipart below come from.

Needs two environment variables - locally via your shell, in production via
a Modal Secret attached to the scheduled function:
  GMAIL_ADDRESS      - your Gmail address, used as both sender and recipient
  GMAIL_APP_PASSWORD - the 16-character App Password, not your real password
"""

import html
import os
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from domain import AlertContext, AlertedListing, PendingNotification

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def send_email(subject: str, plain_body: str, html_body: str | None = None) -> None:
    address = os.environ["GMAIL_ADDRESS"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]

    if html_body:
        message = MIMEMultipart("alternative")
        # Plain part first: email clients use the LAST part they can render,
        # so HTML (the one we actually want shown) has to come second.
        message.attach(MIMEText(plain_body, "plain", "utf-8"))
        message.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        message = MIMEText(plain_body, "plain", "utf-8")

    message["Subject"] = subject
    message["From"] = address
    message["To"] = address

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(address, app_password)
        server.sendmail(address, [address], message.as_string())


def format_alert(notification: PendingNotification) -> tuple[str, str, str]:
    """Returns (subject, plain_body, html_body). Subject is deliberately
    loud/distinctive - the whole reason for wanting alerts outside Fandango's
    own email notifications, earlier in this project, was that email is easy
    to miss in a busy inbox. Filter this to Primary + a phone push-notification
    rule to close that gap.

    One section per Fandango listing, each with its own link: a film's
    showings can be split across listings (the original plus an anniversary
    re-release), and each listing's page only shows its own theaters
    (confirmed in production: It's a Wonderful Life's email listed Metreon
    from the 80th Anniversary listing but linked the 1946 listing, which
    only showed the Lark)."""
    film = notification.film
    subject = f"🎟️ TICKETS ON SALE: {film.title} ({film.year})"
    sections = []
    for listing in notification.listings:
        heading = listing.title + (f" - {describe_dates(listing.dates)}" if listing.dates else "")
        sections.append(
            f"{heading}\nOn sale at: {', '.join(listing.theaters)}\nGet tickets: {listing.url or film.url}"
        )
    plain_body = f"{film.title} ({film.year})\n\n" + "\n\n".join(sections)
    notes = next_steps(notification.context)
    if notes:
        plain_body += "\n\n" + "\n".join(notes)
    return subject, plain_body, _render_html(notification)


def _theater_list(theaters: list[str]) -> str:
    return ", ".join(theaters)


def next_steps(context: AlertContext | None) -> list[str]:
    """Why this email was sent and what can still send another one."""
    if context is None:
        return []
    lines = []
    if context.kind == "preferred":
        earlier = f" ({context.previous_alerted_at:%b} {context.previous_alerted_at.day})" if context.previous_alerted_at else ""
        lines.append(f"Now on sale at your preferred theater(s): {_theater_list(context.preferred_on_sale)}.")
        if context.previous_theaters:
            lines.append(f"Your earlier email{earlier} only listed: {_theater_list(context.previous_theaters)}.")
        lines.append("This is the last email for this film while it stays on sale.")
    elif context.preferred_on_sale:
        lines.append(f"Includes your preferred theater(s): {_theater_list(context.preferred_on_sale)}.")
        lines.append("You won't get another email for this film while it stays on sale.")
    elif context.preferred_missing:
        lines.append("None of your preferred theaters have tickets yet.")
        lines.append(f"You'll get one more email if tickets reach any of: {_theater_list(context.preferred_missing)}.")
    else:
        lines.append("You won't get another email for this film while it stays on sale.")
    lines.append("If it goes off sale and comes back later (a re-release, say), you'll get a new email.")
    return lines


def describe_dates(dates: list[date]) -> str:
    """Compact, with consecutive days as ranges: "Dec 13–14, 23", "Dec 18–24"."""
    if not dates:
        return ""
    days = sorted(set(dates))
    runs: list[tuple[date, date]] = []
    for d in days:
        if runs and (d - runs[-1][1]).days == 1:
            runs[-1] = (runs[-1][0], d)
        else:
            runs.append((d, d))
    parts, last_month = [], None
    for first, last in runs:
        start = f"{first:%b} {first.day}" if (first.year, first.month) != last_month else str(first.day)
        if last == first:
            parts.append(start)
        elif last.month == first.month:
            parts.append(f"{start}–{last.day}")
        else:
            parts.append(f"{start}–{last:%b} {last.day}")
        last_month = (last.year, last.month)
    return ", ".join(parts)


def _render_listing_html(listing: AlertedListing, fallback_url: str | None) -> str:
    theaters_html = "".join(f'<li style="margin:0 0 5px;">{html.escape(t)}</li>' for t in listing.theaters)
    dates_html = (
        f'<div style="font-size:14px;color:#6b6863;margin:0 0 8px;">{html.escape(describe_dates(listing.dates))}</div>'
        if listing.dates
        else ""
    )
    return f"""\
  <div style="margin:0 0 28px;">
    <div style="font-size:17px;font-weight:700;color:#1b231f;margin:0 0 2px;">{html.escape(listing.title)}</div>
    {dates_html}
    <ul style="margin:0 0 14px;padding-left:22px;color:#33302a;font-size:16px;line-height:1.7;">
      {theaters_html}
    </ul>
    <a href="{html.escape(listing.url or fallback_url or '')}"
       style="display:inline-block;background:#1b231f;color:#ffffff;text-decoration:none;
              font-size:15px;font-weight:600;padding:12px 24px;border-radius:8px;">
      Get Tickets &rarr;
    </a>
  </div>"""


def _render_next_steps_html(context: AlertContext | None) -> str:
    notes = next_steps(context)
    if not notes:
        return ""
    paragraphs = "".join(f'<p style="margin:0 0 6px;">{html.escape(n)}</p>' for n in notes)
    return f"""
  <div style="border-top:1px solid #e4ded3;padding-top:16px;margin-top:4px;font-size:14px;line-height:1.5;color:#5c574f;">
    {paragraphs}
  </div>"""


def _render_html(notification: PendingNotification) -> str:
    """Table-based layout (not flexbox/grid) for the poster+content split -
    the reliably email-client-safe way to do multi-column layout, unlike
    modern CSS which Gmail and others support unevenly."""
    listings_html = "".join(_render_listing_html(l, notification.film.url) for l in notification.listings)
    text_content = f"""\
  <div style="font-size:13px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;
              color:#9c6b00;margin:0 0 12px;">
    🎟️ Tickets on sale
  </div>
  <h1 style="font-size:28px;line-height:1.3;margin:0 0 4px;color:#1b231f;font-weight:700;">
    {html.escape(notification.film.title)}
  </h1>
  <div style="font-size:16px;color:#6b6863;margin:0 0 28px;">{notification.film.year}</div>
{listings_html}{_render_next_steps_html(notification.context)}"""

    if not notification.poster_url:
        # No image to fill space, so a narrower box - the 600px width below
        # (sized to make room for a poster) looks empty/oversized without one.
        return f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
            max-width:440px;margin:0 auto;background:#faf8f4;padding:32px 28px;border-radius:14px;">
{text_content}
</div>
"""

    # Wider card than the poster alone needs - the extra width goes to the
    # text column, not the poster, so the card grows horizontally without
    # the poster (and thus the card's height) growing along with it.
    #
    # width="100%" (not just max-width) is required here: a <table> shrinks
    # to fit its content by default, unlike a block-level <div> (the
    # no-poster case below), which fills its container - max-width alone
    # never gets reached without an explicit width forcing the table to
    # actually expand into that ceiling.
    return f"""\
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
              width:100%;max-width:780px;margin:0 auto;background:#faf8f4;border-radius:14px;">
  <tr>
    <td style="padding:32px;vertical-align:top;">
{text_content}
    </td>
    <td style="padding:32px 32px 32px 0;width:260px;vertical-align:top;">
      <img src="{notification.poster_url}" alt="{notification.film.title} poster" width="260"
           style="display:block;width:260px;height:auto;border-radius:10px;" />
    </td>
  </tr>
</table>
"""


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print(f"Usage: python3 {sys.argv[0]} '<subject>' '<plain body>'")
        sys.exit(1)

    send_email(sys.argv[1], sys.argv[2])
    print("Sent.")
