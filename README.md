# letterboxd-ticket-alert

Watches a Letterboxd watchlist, figures out when a film's tickets go on sale
near your zip code, and emails you. Runs unattended on
[Modal](https://modal.com).

## Why

Fandango's own ticket alerts are email-only and easy to miss. This instead:

- Uses Letterboxd's release table as the source of truth for whether/when a
  film has a US release - including re-releases, which get their own dated
  entry there
- Matches that date to the right Fandango listing (a re-release is often a
  separate, oddly-titled listing, like "Moonlight 10th Anniversary
  Remastered")
- Polls Fandango on a tiered schedule and tells "showtimes listed" apart
  from "tickets actually purchasable"
- Emails a loud alert with the poster the moment tickets go on sale, and
  retries if the send fails

## Layout

| File | Does |
|---|---|
| `domain.py` | Shared types (`Tier`, `TicketStatus`, `Film`, ...) |
| `tiering.py` | How often a film gets checked |
| `letterboxd_client.py` | Scrapes the watchlist, lists, and release tables |
| `fandango_client.py` | Fandango search, lookups, live showtimes |
| `matcher.py` | Picks the right Fandango listing for a given date |
| `repository.py` | SQLite persistence (`state.db`) |
| `tracker.py` | One run: sync, match, check, alert |
| `email_alert.py` | Gmail SMTP, HTML email with poster |
| `config.py` | Your personal config (first-run defaults; see Settings below) |
| `web_app.py` | The settings form served at `/config/<token>` |
| `modal_app.py` | The deployed cron + the settings-form web endpoint |
| `why.py` | Diagnostic: why is/isn't a film being tracked |

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 1. Personal config

Nothing personal is committed to this repo.

```bash
cp local_config.example.json local_config.json
```

Fill in:
- `LETTERBOXD_USERNAME`
- `ZIP_CODE`
- `HYPE_LIST_URL` (optional, see Hype below)

`config.py` checks environment variables first, then `local_config.json` -
same values work locally and on Modal.

### 2. Gmail

Alerts send via Gmail SMTP with an [App
Password](https://myaccount.google.com/apppasswords) - not your real
password. Needs 2-Step Verification turned on.

### 3. Deploy

```bash
pip install modal
modal setup

modal secret create gmail-credentials \
  GMAIL_ADDRESS=you@gmail.com \
  GMAIL_APP_PASSWORD="your 16-character app password"

modal secret create app-config \
  LETTERBOXD_USERNAME=your-username \
  ZIP_CODE=10001 \
  HYPE_LIST_URL="https://letterboxd.com/you/list/hype/share/token/"

modal secret create config-ui \
  CONFIG_ACCESS_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"

modal deploy modal_app.py
```

Runs on a 15-minute cron from there. `modal run modal_app.py` triggers one
manual run if you want to check setup first.

## Settings

After the first run, your settings (Letterboxd username, zip, Hype list,
blacklisted theaters) live in the database, not `config.py` - `config.py`'s
values are only the one-time seed. Edit them at:

```
https://<your-modal-app-url>/config/<CONFIG_ACCESS_TOKEN>
```

The token in the URL stands in for a login - there's no account system yet
(single-user, see "Blacklisted theaters" below), so don't share the link.
Find your app's URL and the token with:

```bash
modal app list                       # shows the config_ui endpoint's URL
modal secret list                    # confirms config-ui exists
```

### Blacklisted theaters

Comma-separated theater names (matching Fandango's exactly, e.g. "AMC
Mercado 20, Cinemark Century San Mateo 12") you'd never actually go to. A
showing that's only at blacklisted theaters is treated as if it were never
on sale at all - not just "don't email about it" - so a real alert still
fires the moment tickets reach anywhere else, instead of being suppressed
forever because the status already flipped once. The run log notes this
each time it happens: `on sale only at blacklisted theater(s), skipping: ...`.

Saving the list also retroactively cleans up alerts that are now
worthless: a film stays alerted as long as at least one of the theaters
that *actually triggered its alert* is still not blacklisted - getting one
good alert already satisfied it, so a second theater joining later
(blacklisted or not) doesn't undo that. Only a film whose alerted
theater(s) are now *all* blacklisted gets cleared, making it eligible to
alert again once it reaches a real one. The form shows which films, if
any, got cleared; the same gets logged server-side as
`Blacklist updated: cleared N now-worthless alert(s): ...`.

## How tracking works

Letterboxd decides *whether and when* a film is really releasing in the US.
Fandango only gets involved once Letterboxd has a date. Fandango's own
listings are matched by title, director, synopsis, runtime, and year, in
that order - never by Fandango's own stored release date, which can go
stale on a reused listing. Every listing that plausibly matches gets
checked for live showtimes; there's rarely just one "right" listing for a
re-release.

Tier is recomputed every check, so a film moves between tiers on its own as
its date approaches and passes:

| Tier | When | Checked |
|---|---|---|
| `must_watch` | On the Hype list | every run |
| `hot` | Releasing within 90 days | every ~3h |
| `recent` | Released within the last 30 days | every ~8h |
| `far_future` / `unknown` | Releasing >90 days out, or no date yet | every ~24h |
| `retired` | Released over 30 days ago | every ~7 days |

A `retired` or `unknown` film re-checks Letterboxd first (in case a new
release date showed up), then searches Fandango fresh regardless - a
re-release can surface on Fandango before Letterboxd lists a new date for
it, so neither source is trusted blindly over the other. Other tiers trust
the anchor date they already have and skip the Letterboxd re-check, but
still search Fandango fresh every time - nothing is cached long-term, since
a new matching listing can appear on Fandango at any point.

Documentaries are skipped entirely (Fandango doesn't track them reliably)
unless the film is on Hype.

### Hype

For films whose tickets go on sale with almost no notice - `hot`'s 3-hour
cadence isn't fast enough. Make a Letterboxd list (any name) and add films
to it:

- Tracked even if not on your watchlist
- Documentaries not excluded
- Checked (and re-matched) every single run, even before Letterboxd has a
  confirmed date

Can be a private list - use its share link as `HYPE_LIST_URL`. Only the
first page (~28 films) of a private list is reachable, so keep it small.

## Resilience

Runs every 15 minutes, so tolerating flaky sites matters:

- Every request retries with backoff against timeouts, 5xxs, and (for
  Fandango) its intermittent bot-block page.
- A failed watchlist fetch just skips that run's resync instead of failing
  the whole run. A failed Fandango session skips ticket checks but still
  syncs the watchlist. One film failing doesn't stop the rest from being
  checked.
- Only actionable failures email you - a timeout or 5xx that self-heals on
  its own doesn't.

## Diagnosing a film

```bash
python3 why.py "Dune: Part Three"
```

Prints its anchor date, Fandango match, status, tier, and next check time.

## Testing

```bash
python3 -m pytest          # fast, no network (fixtures are real saved HTML)
python3 -m pytest -m live  # hits the real sites - run after touching a parser
```

## Known limitations

- Fandango/Letterboxd can change their HTML and silently break a parser.
  `pytest -m live` catches this, but only if you run it.
- Fandango doesn't cover every theater, especially small/independent ones.
- Once a film has alerted, a *second* real theater adding tickets later
  doesn't trigger a second alert - one good alert is treated as enough.
  `why.py` or the logs show the full live picture if you want to check
  whether more theaters have since joined.
