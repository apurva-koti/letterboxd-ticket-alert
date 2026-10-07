"""Modal deployment: runs Tracker.run() on a cron, with state.db persisted on
a Modal Volume so matches/tiers/alert history survive between runs.

Deploy with: modal deploy modal_app.py
"""

import logging
import time

import modal
import requests

DB_PATH = "/data/state.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("letterboxd-ticket-alert")


def _env_config_defaults():
    """The one-time seed for the `config` DB table, read from env vars /
    the app-config Secret. Shared by run_scheduler and config_ui so
    whichever happens to run first seeds the same real values, not blank
    ones (confirmed in production: loading the form before the scheduler's
    first-ever run seeded the row empty)."""
    import config as env_config
    from domain import Config

    return Config(
        letterboxd_username=env_config.LETTERBOXD_USERNAME,
        zip_code=env_config.ZIP_CODE,
        hype_list_url=env_config.HYPE_LIST_URL,
        blacklisted_theaters=env_config.BLACKLISTED_THEATERS,
    )


def _is_transient_failure(exc: Exception) -> bool:
    """A connectivity hiccup or a 5xx from the site's own server - both
    confirmed in production to self-heal within a cron cycle or two, so not
    worth an email. A 4xx or anything else usually means something on our
    end needs fixing, and still emails."""
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return 500 <= exc.response.status_code < 600
    return False


app = modal.App("letterboxd-ticket-alert")

image = (
    modal.Image.debian_slim()
    .uv_pip_install("requests", "beautifulsoup4", "fastapi", "python-multipart")
    .add_local_python_source(
        "domain",
        "tiering",
        "letterboxd_client",
        "fandango_client",
        "matcher",
        "repository",
        "tracker",
        "email_alert",
        "config",
        "web_app",
    )
)

volume = modal.Volume.from_name("letterboxd-ticket-alert-db", create_if_missing=True)
gmail_secret = modal.Secret.from_name("gmail-credentials")
app_config_secret = modal.Secret.from_name("app-config")
config_ui_secret = modal.Secret.from_name("config-ui")


@app.function(
    image=image,
    volumes={"/data": volume},
    secrets=[gmail_secret, app_config_secret],
    schedule=modal.Cron("*/15 * * * *"),
    timeout=600,
    region="us",
)
def run_scheduler() -> None:
    import email_alert
    from letterboxd_client import LetterboxdClient
    from repository import FilmRepository
    from tracker import Tracker

    start = time.monotonic()

    repo = FilmRepository.connect(DB_PATH)
    # Seeds from the env-var config on the very first run only - from then
    # on the DB (editable via the /config web form) is the source of truth.
    cfg = repo.get_config(defaults=_env_config_defaults())
    logger.info(f"Run starting: username={cfg.letterboxd_username} zip={cfg.zip_code}")

    pending = []
    try:
        result = Tracker(repo, LetterboxdClient()).run(
            cfg.letterboxd_username,
            cfg.zip_code,
            hype_list_url=cfg.hype_list_url,
            blacklisted_theaters=cfg.blacklisted_theaters,
        )

        pending = repo.get_unnotified()
        for notification in pending:
            try:
                subject, plain_body, html_body = email_alert.format_alert(notification)
                email_alert.send_email(subject, plain_body, html_body)
                repo.mark_notified(notification.film.slug)
                logger.info(f"Email sent: {notification.film.title} ({notification.film.year})")
            except Exception:
                logger.exception(f"Email failed for {notification.film.title} - will retry next run")
    except Exception as e:
        if _is_transient_failure(e):
            logger.warning(f"Run failed on a transient error, not emailing: {e!r}")
            raise
        logger.exception("Run failed")
        try:
            email_alert.send_email(
                "⚠️ Ticket alert run FAILED",
                "Tracker.run() raised an exception this run. Check the Modal dashboard "
                "(app: letterboxd-ticket-alert) for the full traceback.",
            )
        except Exception:
            logger.exception("Also failed to send the failure-alert email")
        raise
    finally:
        volume.commit()

    duration = time.monotonic() - start
    logger.info(
        f"Run complete in {duration:.1f}s: added={len(result.added)} removed={len(result.removed)} "
        f"checked={len(result.checked)} alerted={len(result.alerted)} notified={len(pending)}"
    )
    for f in result.added:
        logger.info(f"Watchlist added: {f.title} ({f.year})")
    for f in result.removed:
        logger.info(f"Watchlist removed: {f.title} ({f.year})")


@app.function(image=image, volumes={"/data": volume}, secrets=[config_ui_secret])
@modal.asgi_app()
def config_ui():
    """The settings form at <app-url>/config/<CONFIG_ACCESS_TOKEN> - see
    web_app.py. Single-user for now; the access token stands in for a login
    since there's no account system yet."""
    import os

    from repository import FilmRepository
    from web_app import create_app

    repo = FilmRepository.connect(DB_PATH)
    # Not on_read=volume.reload: the connection opened above keeps state.db
    # open for this container's lifetime, and reload() refuses to run while
    # any file on the volume is open (confirmed in production - 500s every
    # request). Not needed for correctness anyway - the scheduler opens its
    # own fresh connection every run and always sees the latest commit.
    return create_app(
        repo,
        os.environ["CONFIG_ACCESS_TOKEN"],
        on_saved=volume.commit,
        seed_defaults=_env_config_defaults(),
    )


@app.local_entrypoint()
def main() -> None:
    """For a manual one-off run during setup/testing: modal run modal_app.py"""
    run_scheduler.remote()
