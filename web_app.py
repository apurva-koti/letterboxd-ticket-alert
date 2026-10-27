"""A tiny config-editing form, hosted on Modal alongside the scheduler.

Single-user for now: access is gated by a secret URL token (like a
Letterboxd share link, not a login), since there's no account system yet.
Comparing an unguessable token is enough for "a few friends," not a real
auth system - see modal_app.py for the multi-user plan this is step one of.
"""

from __future__ import annotations

import html
import logging
from urllib.parse import quote

from typing import Callable

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from domain import Config
from repository import FilmRepository

logger = logging.getLogger("letterboxd-ticket-alert")

THEATER_RADIUS_MILES = 50

NearbyTheaters = Callable[[str, int], list[tuple[str, float]]]


def create_app(
    repo: FilmRepository,
    access_token: str,
    on_saved=lambda: None,
    on_read=lambda: None,
    seed_defaults: Config | None = None,
    nearby_theaters: NearbyTheaters = lambda zip_code, radius: [],
) -> FastAPI:
    """`on_saved` runs after every successful save - on Modal, this commits
    the Volume so the write is durable and visible to the scheduler's
    separate container. `on_read` is a hook for the equivalent on page
    load (picking up a change from elsewhere), left as a no-op by default:
    Modal's Volume.reload() refuses to run while this container's own
    sqlite connection holds state.db open, and it isn't needed for
    correctness here anyway since the scheduler always opens its own fresh
    connection and sees the latest commit regardless. `seed_defaults`
    matters only on the very first-ever load, before any config row
    exists - pass the same defaults the scheduler seeds from, or whichever
    of the two runs first wins and the other seeds blank (confirmed in
    production: loading this page before the scheduler's first run ever
    fired seeded the row empty). `nearby_theaters(zip, radius)` supplies the
theaters the form lets you mark preferred/blacklisted - Fandango's own
names, so nothing has to be typed to match exactly."""
    app = FastAPI()

    def _check_token(token: str) -> None:
        if token != access_token:
            raise HTTPException(status_code=404)

    @app.get("/config/{token}", response_class=HTMLResponse)
    def show_form(token: str, saved: bool = False, cleared: str = "") -> str:
        _check_token(token)
        on_read()
        cleared_titles = cleared.split("\x1f") if cleared else []
        config = repo.get_config(seed_defaults)
        return _render_form(token, config, _theater_rows(config, nearby_theaters), saved, cleared_titles)

    @app.post("/config/{token}")
    async def save_form(
        token: str,
        request: Request,
        letterboxd_username: str = Form(...),
        zip_code: str = Form(...),
        hype_list_url: str = Form(""),
    ) -> RedirectResponse:
        _check_token(token)
        form = await request.form()
        choices = {name: form.get(f"choice_{i}", "") for i, name in _indexed_theater_names(form)}
        new_blacklist = frozenset(name for name, choice in choices.items() if choice == "blacklisted")
        repo.save_config(
            Config(
                letterboxd_username=letterboxd_username.strip(),
                zip_code=zip_code.strip(),
                hype_list_url=hype_list_url.strip() or None,
                blacklisted_theaters=new_blacklist,
                preferred_theaters=frozenset(name for name, choice in choices.items() if choice == "preferred"),
            )
        )
        # A film stays alerted unless EVERY on-sale theater it has is now
        # blacklisted - getting one good alert already satisfied it, so a
        # second theater joining later (blacklisted or not) doesn't undo
        # that. Only a now-worthless alert gets cleared, making that film
        # eligible to alert again once it reaches a real theater.
        cleared = repo.reapply_blacklist(new_blacklist)
        if cleared:
            titles = ", ".join(f.title for f in cleared)
            logger.info(f"Blacklist updated: cleared {len(cleared)} now-worthless alert(s): {titles}")
        on_saved()
        cleared_param = quote("\x1f".join(f.title for f in cleared))
        return RedirectResponse(url=f"/config/{token}?saved=1&cleared={cleared_param}", status_code=303)

    return app


def _indexed_theater_names(form) -> list[tuple[str, str]]:
    return [(key.removeprefix("theater_"), str(value)) for key, value in form.items() if key.startswith("theater_")]


def _theater_rows(config: Config, nearby_theaters: NearbyTheaters) -> tuple[list[tuple[str, float | None]], bool]:
    """Fandango's nearby theaters, nearest first, plus any already-saved
    theater that isn't in that list (so saving never silently drops one).
    Returns (rows, fetch_failed)."""
    fetch_failed = False
    try:
        nearby = nearby_theaters(config.zip_code, THEATER_RADIUS_MILES) if config.zip_code else []
    except Exception:
        logger.warning("Nearby theaters lookup failed - showing saved theaters only", exc_info=True)
        nearby, fetch_failed = [], True
    names = {name for name, _ in nearby}
    saved = sorted((config.preferred_theaters | config.blacklisted_theaters) - names)
    rows: list[tuple[str, float | None]] = list(nearby) + [(name, None) for name in saved]
    return rows, fetch_failed


def _render_form(
    token: str,
    config: Config,
    theaters: tuple[list[tuple[str, float | None]], bool],
    saved: bool,
    cleared_titles: list[str] | None = None,
) -> str:
    banner = '<p class="saved">Saved.</p>' if saved else ""
    if cleared_titles:
        items = "".join(f"<li>{html.escape(title)}</li>" for title in cleared_titles)
        banner += (
            '<p class="cleared">These were only on sale at theaters you just blacklisted - '
            f"cleared, so you'll be alerted again once they reach a real theater:</p><ul>{items}</ul>"
        )
    rows, fetch_failed = theaters
    theater_rows = "".join(_render_theater_row(i, name, distance, config) for i, (name, distance) in enumerate(rows))
    theater_note = (
        '<p class="hint">Couldn\'t reach Fandango for nearby theaters - showing only the ones you\'ve already marked.</p>'
        if fetch_failed
        else ""
    )
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Ticket alert settings</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 640px; margin: 3rem auto; color: #222; }}
  label {{ display: block; margin-top: 1.2rem; font-weight: 600; }}
  input:not([type=radio]) {{ width: 100%; padding: 0.5rem; margin-top: 0.3rem; font-size: 1rem; box-sizing: border-box; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 0.5rem; font-size: 0.9rem; }}
  th {{ text-align: left; font-weight: 600; padding: 0.3rem; border-bottom: 1px solid #ccc; }}
  td {{ padding: 0.3rem; border-bottom: 1px solid #eee; }}
  td.dist, td.pick {{ white-space: nowrap; text-align: center; font-variant-numeric: tabular-nums; }}
  .hint {{ color: #666; font-size: 0.85rem; margin-top: 0.2rem; }}
  button {{ margin-top: 1.5rem; padding: 0.6rem 1.2rem; font-size: 1rem; cursor: pointer; }}
  .saved {{ color: #1a7f37; font-weight: 600; }}
  .cleared {{ color: #444; margin-top: 1rem; }}
</style>
</head>
<body>
<h1>Ticket alert settings</h1>
{banner}
<form method="post" action="/config/{token}">
  <label>Letterboxd username</label>
  <input name="letterboxd_username" value="{config.letterboxd_username or ''}" required>

  <label>Zip code</label>
  <input name="zip_code" value="{config.zip_code or ''}" required>

  <label>Hype list URL</label>
  <input name="hype_list_url" value="{config.hype_list_url or ''}">
  <div class="hint">Optional. Films on this list are checked every run and alert on any theater.</div>

  <label>Theaters</label>
  <div class="hint">Within {THEATER_RADIUS_MILES} miles of your zip, nearest first. <b>Preferred</b>: if a film's alert didn't include any of these, you'll get one more alert when one goes on sale. <b>Blacklisted</b>: a showing only at these won't alert you.</div>
  {theater_note}
  <table>
    <tr><th>Theater</th><th>Miles</th><th>Normal</th><th>Preferred</th><th>Blacklisted</th></tr>
    {theater_rows}
  </table>

  <button type="submit">Save</button>
</form>
</body>
</html>"""


def _render_theater_row(i: int, name: str, distance: float | None, config: Config) -> str:
    if name in config.preferred_theaters:
        current = "preferred"
    elif name in config.blacklisted_theaters:
        current = "blacklisted"
    else:
        current = ""
    radios = "".join(
        f'<td class="pick"><input type="radio" id="choice_{i}_{value or 'normal'}" name="choice_{i}" value="{value}"'
        f'{" checked" if value == current else ""} aria-label="{html.escape(label)}"></td>'
        for value, label in (("", "Normal"), ("preferred", "Preferred"), ("blacklisted", "Blacklisted"))
    )
    miles = f"{distance:.1f}" if distance is not None else "?"
    return (
        f'<tr><td>{html.escape(name)}<input type="hidden" name="theater_{i}" value="{html.escape(name)}"></td>'
        f'<td class="dist">{miles}</td>{radios}</tr>'
    )
