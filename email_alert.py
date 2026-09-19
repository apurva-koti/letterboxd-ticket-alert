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

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def send_email(subject, plain_body, html_body=None):
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


def format_alert(notification):
    """Returns (subject, plain_body, html_body). Subject is deliberately
    loud/distinctive - the whole reason for wanting alerts outside Fandango's
    own email notifications, earlier in this project, was that email is easy
    to miss in a busy inbox. Filter this to Primary + a phone push-notification
    rule to close that gap."""
    theaters = ", ".join(notification.on_sale_theaters)
    subject = f"🎟️ TICKETS ON SALE: {notification.film.title} ({notification.film.year})"
    plain_body = (
        f"{notification.film.title} ({notification.film.year})\n\n"
        f"On sale at: {theaters}\n\n"
        f"Get tickets: {notification.ticket_url}"
    )
    return subject, plain_body, _render_html(notification)


def _render_html(notification):
    """Table-based layout (not flexbox/grid) for the poster+content split -
    the reliably email-client-safe way to do multi-column layout, unlike
    modern CSS which Gmail and others support unevenly."""
    theaters_html = "".join(
        f'<li style="margin:0 0 5px;">{theater}</li>' for theater in notification.on_sale_theaters
    )
    text_content = f"""\
  <div style="font-size:13px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;
              color:#9c6b00;margin:0 0 12px;">
    🎟️ Tickets on sale
  </div>
  <h1 style="font-size:28px;line-height:1.3;margin:0 0 4px;color:#1b231f;font-weight:700;">
    {notification.film.title}
  </h1>
  <div style="font-size:16px;color:#6b6863;margin:0 0 28px;">{notification.film.year}</div>
  <div style="font-size:12px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;
              color:#8a8272;margin:0 0 10px;">
    Playing at
  </div>
  <ul style="margin:0 0 32px;padding-left:22px;color:#33302a;font-size:16px;line-height:1.7;">
    {theaters_html}
  </ul>
  <a href="{notification.ticket_url}"
     style="display:inline-block;background:#1b231f;color:#ffffff;text-decoration:none;
            font-size:16px;font-weight:600;padding:14px 28px;border-radius:8px;">
    Get Tickets &rarr;
  </a>"""

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
