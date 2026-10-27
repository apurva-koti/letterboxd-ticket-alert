# letterboxd-ticket-alert

Watches a Letterboxd watchlist, figures out when a film's tickets go on sale
near your zip code, and emails you. Runs unattended on
[Modal](https://modal.com). Built because Fandango's own ticket alerts are
email-only and easy to miss.

This README starts with what to do, then gets progressively deeper into how
it actually works - skip ahead once you've got what you need.

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
- `HYPE_LIST_URL` (optional, see Hype further down)

`config.py` checks environment variables first, then `local_config.json` -
same values work locally and on Modal. (These only matter for the very
first run - see Settings below for how to change them afterward.)

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
blacklisted and preferred theaters) live in the database, not `config.py` - `config.py`'s
values are only the one-time seed. Edit them at:

```
https://<your-modal-app-url>/config/<CONFIG_ACCESS_TOKEN>
```

The token in the URL stands in for a login - there's no account system yet
(single-user for now), so don't share the link.
Find your app's URL and the token with:

```bash
modal app list                       # shows the config_ui endpoint's URL
modal secret list                    # confirms config-ui exists
```

### Theaters

The form lists every theater Fandango has within 50 miles of your zip,
nearest first. Mark each one Normal, Preferred, or Blacklisted.

- **Blacklisted**: theaters you'd never go to. A showing only at these
  won't alert you; the alert fires once a non-blacklisted theater has
  tickets. Blacklisting a theater also clears any alert that was only
  backed by blacklisted theaters, so you'll hear about that film again.
- **Preferred**: theaters you'd rather see a film at. A film normally
  alerts once per run in theaters. If that alert didn't include a
  preferred theater, you get one more alert when a preferred theater has
  tickets (e.g. Alamo first, then AMC Metreon). Until then, each check
  looks through all of the film's showtime dates and all nearby theaters
  for a preferred one, not just the first date's 5 nearest theaters.

### Hype

A Letterboxd list (any name) for films you want checked aggressively -
every single run - instead of waiting on the normal schedule below. Use it
for anything whose tickets might go on sale with almost no notice. Films on
it are tracked even if they're not on your watchlist or Letterboxd has no
confirmed date yet.

Can be a private list - use its share link as the Hype list URL. Only the
first page (~28 films) of a private list is reachable, so keep it small.

## Diagnosing a film

```bash
python3 why.py "Dune: Part Three"
```

Prints its anchor date, Fandango match, status, tier, and next check time -
the quickest way to see what the tracker currently thinks about a film,
without digging into the database by hand.

## Logs

Every run's log is also saved on the Modal Volume for 30 days (Modal's own
logs only go back about a day), one file per run:

```bash
modal volume ls letterboxd-ticket-alert-db logs                 # days available
modal volume get letterboxd-ticket-alert-db logs/2026-10-27 .   # one day's runs
grep -A30 "Misty Green" 2026-10-27/*.log
```

Each film checked gets one block: a summary line (status before -> after,
and why), then everything the decision was based on - every Fandango
listing found and why it was kept or dropped, the dates Fandango lists,
and every showing at each theater on the date that was read, marked
`open`, `sold out`, or `past`. The full raw Fandango responses are added
when the status changed, or when Fandango returned something unexpected.

## Testing

```bash
python3 -m pytest          # fast, no network (fixtures are real saved HTML)
python3 -m pytest -m live  # hits the real sites - run after touching a parser
```

---

Everything below explains how it actually works - useful if you're
debugging something, extending it, or just curious.

## How tracking works

Letterboxd decides *whether and when* a film is really releasing in the US,
using its release table as the source of truth - including re-releases,
which get their own dated entry there. Fandango only gets involved once
Letterboxd has a date, and purely to check live showtimes - its listings are
matched by title, director, synopsis, runtime, and year, in that order,
*never* by Fandango's own stored release date, which can go stale on a
reused listing (a re-release sometimes gets a brand-new, oddly-titled
Fandango listing, like "Moonlight 10th Anniversary Remastered," and
sometimes just reuses the original one without its date ever updating).
Every listing that plausibly matches gets checked for live showtimes, told
apart as "showtimes listed" vs. "tickets actually purchasable" - there's
rarely just one "right" listing for a re-release, so no single one is
picked in advance.

The moment tickets are purchasable, it emails a loud alert with the poster,
and retries if the send fails.

Each film is always in one of three alert states:

| State | What happens on a check |
|---|---|
| Not on sale | Alerts ("first") as soon as a non-blacklisted theater has tickets |
| On sale, alert had no preferred theater | Searches every date for a preferred theater; alerts once more ("preferred") when one has tickets |
| On sale, alert included a preferred theater | Nothing - no more alerts while it stays on sale |

Going off sale returns a film to "not on sale", and its past alert no
longer counts - the next time it's on sale is treated as a new run (e.g. a
re-release) and alerts again. Saving the settings form with every theater
from a film's alert now blacklisted does the same.

Tier controls how often a film gets checked. It's recomputed after every
check, from the Letterboxd date and whether the film is on sale, so a film
moves between tiers on its own:

| Tier | When | Checked |
|---|---|---|
| `must_watch` | On the Hype list | every run |
| `hot` | Releasing within 90 days | every ~3h |
| `recent` | Released within the last 30 days, or released longer ago but still on sale | every ~8h |
| `far_future` / `unknown` | Releasing >90 days out, or no date yet | every ~24h |
| `retired` | Released over 30 days ago and no longer on sale - its run is over | every ~7 days |

Retiring clears the film's alert history. A `retired` or `unknown` film re-checks Letterboxd first (in case a new
release date showed up), then searches Fandango fresh regardless - a
re-release can surface on Fandango before Letterboxd lists a new date for
it, so neither source is trusted blindly over the other. Other tiers trust
the anchor date they already have and skip the Letterboxd re-check, but
still search Fandango fresh every time - nothing is cached long-term, since
a new matching listing can appear on Fandango at any point.

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

## Layout

| File | Does |
|---|---|
| `domain.py` | Shared types (`Tier`, `TicketStatus`, `Film`, ...) |
| `tiering.py` | How often a film gets checked |
| `letterboxd_client.py` | Scrapes the watchlist, lists, and release tables |
| `fandango_client.py` | Fandango search, lookups, live showtimes |
| `matcher.py` | Finds every plausible Fandango listing for a film |
| `repository.py` | SQLite persistence (`state.db`) |
| `tracker.py` | One run: sync, match, check, alert |
| `email_alert.py` | Gmail SMTP, HTML email with poster |
| `config.py` | Your personal config (first-run defaults; see Settings above) |
| `web_app.py` | The settings form served at `/config/<token>` |
| `modal_app.py` | The deployed cron + the settings-form web endpoint |
| `checktrace.py` | Collects what one film's check saw, logged as one block |
| `run_log.py` | Saves each run's log to the Volume for 30 days |
| `why.py` | Diagnostic: why is/isn't a film being tracked |

## Known limitations

- Fandango/Letterboxd can change their HTML and silently break a parser.
  `pytest -m live` catches this, but only if you run it.
- Fandango doesn't cover every theater, especially small/independent ones.
- Once a film has alerted, another theater adding tickets later doesn't
  trigger another alert - unless it's the first preferred theater (see
  Theaters above). `why.py` or the logs show the full live picture.
