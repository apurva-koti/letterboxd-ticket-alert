"""Modal deployment: runs scheduler.run() on a cron, with state.db persisted
on a Modal Volume so matches/tiers/alert history survive between runs.

Deploy with: modal deploy modal_app.py
Logs/runs are visible in the Modal dashboard under this app's name.

Cost note (verified against modal.com/pricing, not assumed): the dominant
per-run cost is scheduler.run()'s full Letterboxd watchlist resync (~9 paginated
requests at a 0.5s politeness delay each, done on every invocation regardless
of whether anything's due) - roughly 7-9s wall time. At the 15-min cron below
(~2,880 runs/month) and Modal's $0.0000131/core-second rate (0.125-core
minimum per container), that's on the order of $0.05-$0.20/month - a rounding
error against the $30/month free compute credit. state.db is under 1MB, far
under the 1 TiB/month free Volume storage.
"""

import logging
import time

import modal

DB_PATH = "/data/state.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("letterboxd-ticket-alert")

app = modal.App("letterboxd-ticket-alert")

image = (
    modal.Image.debian_slim()
    .uv_pip_install("requests", "beautifulsoup4")
    .add_local_python_source(
        "letterboxd",
        "fandango",
        "boxofficemojo",
        "state",
        "ticket_checker",
        "scheduler",
        "models",
        "email_alert",
        "config",
    )
)

volume = modal.Volume.from_name("letterboxd-ticket-alert-db", create_if_missing=True)

# Created via: modal secret create gmail-credentials GMAIL_ADDRESS=... GMAIL_APP_PASSWORD=...
gmail_secret = modal.Secret.from_name("gmail-credentials")
# Created via: modal secret create app-config LETTERBOXD_USERNAME=... ZIP_CODE=... HYPE_LIST_URL=...
# Same keys config.py reads locally from local_config.json - this is that
# file's remote equivalent, so the repo carries no personal data either way.
app_config_secret = modal.Secret.from_name("app-config")


@app.function(
    image=image,
    volumes={"/data": volume},
    secrets=[gmail_secret, app_config_secret],
    schedule=modal.Cron("*/15 * * * *"),
    timeout=600,
    # Fandango/Letterboxd/Box Office Mojo are all US-facing sites; pinning to
    # "us" (broad, not a narrower sub-region, for better resource
    # availability - see Modal's own region-selection guidance) keeps
    # outgoing requests looking like ordinary US traffic, which plausibly
    # reduces how often Fandango's bot-management blocks them in the first
    # place - a complement to the retry-with-backoff in fandango.py, not a
    # replacement for it, since a block is possible from any region.
    region="us",
)
def run_scheduler():
    import config
    import email_alert
    import state
    import scheduler

    start = time.monotonic()
    logger.info(f"Run starting: username={config.LETTERBOXD_USERNAME} zip={config.ZIP_CODE}")

    try:
        result = scheduler.run(
            config.LETTERBOXD_USERNAME, config.ZIP_CODE, hype_list_url=config.HYPE_LIST_URL, db_path=DB_PATH
        )

        # Sending is separate from ticket-checking: a film only counts as
        # "notified" once its email actually sends, so an SMTP failure here
        # gets retried on the next run instead of being silently lost - see
        # state.get_unnotified_on_sale's docstring for why this is split out
        # from should_alert/alerted_at.
        conn = state.connect(DB_PATH)
        pending = state.get_unnotified_on_sale(conn)
        for notification in pending:
            try:
                subject, plain_body, html_body = email_alert.format_alert(notification)
                email_alert.send_email(subject, plain_body, html_body)
                state.mark_notified(conn, notification.film.slug)
                theaters = ", ".join(notification.on_sale_theaters)
                logger.info(f"Email sent: {notification.film.title} ({notification.film.year}) - {theaters}")
            except Exception:
                logger.exception(f"Email failed for {notification.film.title} - will retry next run")
    except Exception:
        logger.exception("Run failed")
        # A crashed run wouldn't otherwise be visible anywhere but the Modal
        # dashboard - best-effort email it directly too, since "just email me
        # when something breaks" is the actual thing worth having, simpler
        # than a separate CI pipeline. Wrapped so a failing send here doesn't
        # mask the real error below.
        try:
            email_alert.send_email(
                "⚠️ Ticket alert run FAILED",
                "scheduler.run() raised an exception this run. Check the Modal dashboard "
                "(app: letterboxd-ticket-alert) for the full traceback.",
            )
        except Exception:
            logger.exception("Also failed to send the failure-alert email")
        raise  # let Modal mark this invocation as failed/errored in the dashboard too
    finally:
        volume.commit()  # persist whatever state.db writes happened, even on failure

    duration = time.monotonic() - start
    logger.info(
        f"Run complete in {duration:.1f}s: checked={len(result['checked'])} "
        f"alerts={len(result['alerts'])} notified={len(pending)} "
        f"added={len(result['added'])} removed={len(result['removed'])}"
    )

    if result["checked"]:
        tier_counts = {}
        for outcome in result["checked"]:
            tier_counts[outcome.tier] = tier_counts.get(outcome.tier, 0) + 1
        logger.info(f"Tier breakdown this run: {tier_counts}")
        for outcome in result["checked"]:
            logger.info(f"  Checked: {outcome.film.title} ({outcome.film.year}) [{outcome.tier}] -> {outcome.new_status}")

    for f in result["added"]:
        logger.info(f"Watchlist added: {f.title} ({f.year})")
    for f in result["removed"]:
        logger.info(f"Watchlist removed: {f.title} ({f.year})")

    if not pending:
        logger.info("No pending on-sale notifications this run.")


@app.local_entrypoint()
def main():
    """For a manual one-off run during setup/testing: modal run modal_app.py"""
    run_scheduler.remote()
