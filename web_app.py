"""A tiny config-editing form, hosted on Modal alongside the scheduler.

Single-user for now: access is gated by a secret URL token (like a
Letterboxd share link, not a login), since there's no account system yet.
Comparing an unguessable token is enough for "a few friends," not a real
auth system - see modal_app.py for the multi-user plan this is step one of.
"""

from __future__ import annotations

import html
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from domain import Config
from repository import FilmRepository


def create_app(
    repo: FilmRepository,
    access_token: str,
    on_saved=lambda: None,
    on_read=lambda: None,
    seed_defaults: Config | None = None,
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
    fired seeded the row empty)."""
    app = FastAPI()

    def _check_token(token: str) -> None:
        if token != access_token:
            raise HTTPException(status_code=404)

    @app.get("/config/{token}", response_class=HTMLResponse)
    def show_form(token: str, saved: bool = False, cleared: str = "") -> str:
        _check_token(token)
        on_read()
        cleared_titles = cleared.split("\x1f") if cleared else []
        return _render_form(token, repo.get_config(seed_defaults), saved, cleared_titles)

    @app.post("/config/{token}")
    def save_form(
        token: str,
        letterboxd_username: str = Form(...),
        zip_code: str = Form(...),
        hype_list_url: str = Form(""),
        blacklisted_theaters: str = Form(""),
    ) -> RedirectResponse:
        _check_token(token)
        new_blacklist = frozenset(t.strip() for t in blacklisted_theaters.split(",") if t.strip())
        repo.save_config(
            Config(
                letterboxd_username=letterboxd_username.strip(),
                zip_code=zip_code.strip(),
                hype_list_url=hype_list_url.strip() or None,
                blacklisted_theaters=new_blacklist,
            )
        )
        # A film stays alerted unless EVERY on-sale theater it has is now
        # blacklisted - getting one good alert already satisfied it, so a
        # second theater joining later (blacklisted or not) doesn't undo
        # that. Only a now-worthless alert gets cleared, making that film
        # eligible to alert again once it reaches a real theater.
        cleared = repo.reapply_blacklist(new_blacklist)
        on_saved()
        cleared_param = quote("\x1f".join(f.title for f in cleared))
        return RedirectResponse(url=f"/config/{token}?saved=1&cleared={cleared_param}", status_code=303)

    return app


def _render_form(token: str, config: Config, saved: bool, cleared_titles: list[str] | None = None) -> str:
    banner = '<p class="saved">Saved.</p>' if saved else ""
    if cleared_titles:
        items = "".join(f"<li>{html.escape(title)}</li>" for title in cleared_titles)
        banner += (
            '<p class="cleared">These were only on sale at theaters you just blacklisted - '
            f"cleared, so you'll be alerted again once they reach a real theater:</p><ul>{items}</ul>"
        )
    blacklist_text = ", ".join(sorted(config.blacklisted_theaters))
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Ticket alert settings</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 480px; margin: 3rem auto; color: #222; }}
  label {{ display: block; margin-top: 1.2rem; font-weight: 600; }}
  input, textarea {{ width: 100%; padding: 0.5rem; margin-top: 0.3rem; font-size: 1rem; box-sizing: border-box; }}
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

  <label>Blacklisted theaters</label>
  <textarea name="blacklisted_theaters" rows="3">{blacklist_text}</textarea>
  <div class="hint">Comma-separated, matching Fandango's theater names exactly (e.g. "AMC Mercado 20, Cinemark Century San Mateo 12"). A showing only at these theaters won't alert you.</div>

  <button type="submit">Save</button>
</form>
</body>
</html>"""
