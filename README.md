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
| `config.py` | Your personal config |
| `modal_app.py` | The deployed cron |
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

modal deploy modal_app.py
```

Runs on a 15-minute cron from there. `modal run modal_app.py` triggers one
manual run if you want to check setup first.

## How tracking works

Letterboxd decides *whether and when* a film is really releasing in the US.
Fandango only gets involved once Letterboxd has a date, and is matched by
release-date proximity, not title - titles alone can't tell a re-release
apart, or two different films with the same name.

Tier is recomputed every check, so a film moves between tiers on its own as
its date approaches and passes:

| Tier | When | Checked |
|---|---|---|
| `must_watch` | On the Hype list | every run |
| `hot` | Releasing within 90 days | every ~3h |
| `recent` | Released within the last 30 days | every ~8h |
| `far_future` / `unknown` | Releasing >90 days out, or no date yet | every ~24h |
| `retired` | Released over 30 days ago | every ~7 days |

`retired` films keep no Fandango listing - nothing left to poll, and if a
re-release is coming, Letterboxd shows it first. Each retired check just
looks at Letterboxd again; if a new, later date shows up, that's the signal
to search Fandango fresh and start polling again. Other tiers trust the
Fandango listing they already have and don't re-check Letterboxd.

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
- A `hot`/`recent` film that fails to match Fandango isn't retried until it
  ages into `retired` (or is on Hype) - Letterboxd is only re-checked on the
  slow tiers.
