# letterboxd-ticket-alert

Watches a Letterboxd watchlist (plus an optional "must-watch" list), figures
out when a film's tickets actually go on sale near a given zip code, and
emails an alert. Runs unattended on a schedule via [Modal](https://modal.com).

## Why this exists

Fandango's own ticket alerts are email-only and easy to miss. This instead:

- Matches each watchlist film to Fandango (falling back to Letterboxd's own
  release-date data, then Box Office Mojo, when Fandango hasn't cataloged a
  film yet)
- Polls Fandango's real showtime data on a tiered schedule - more often for
  films close to release, less often otherwise - distinguishing "showtimes
  listed" from "tickets actually purchasable"
- Emails a loud, poster-included alert the moment tickets go on sale, retried
  automatically if the send itself fails

## How it's organized

| File | Does |
|---|---|
| `letterboxd.py` | Scrapes a public watchlist and lists (both are HTML, no API) |
| `fandango.py` | Matches titles to Fandango and checks real showtime status |
| `boxofficemojo.py` | Fallback US release-date lookup |
| `state.py` | SQLite persistence (`state.db`) |
| `ticket_checker.py` | Per-film orchestration: match, then check status |
| `scheduler.py` | Tiering logic + the main per-run loop |
| `email_alert.py` | Gmail SMTP, HTML email with poster |
| `config.py` | Reads your personal config (see below) |
| `modal_app.py` | The deployed cron + email-on-failure |
| `why.py` | Diagnostic: why is/isn't a film being tracked right now |

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 1. Personal config

Nothing personal is committed to this repo. Copy the template and fill in
your own values:

```bash
cp local_config.example.json local_config.json
```

- `LETTERBOXD_USERNAME` - your Letterboxd username
- `ZIP_CODE` - where "near me" means, for theater matching
- `HYPE_LIST_URL` - see "Must-watch films" below (optional - leave blank/omit to skip it)

`config.py` reads environment variables first, falling back to
`local_config.json` - that's so the exact same values work locally (via the
file) and on Modal (via a Secret, since the file itself is never deployed).

### 2. Gmail

Alerts send via Gmail SMTP using an [App
Password](https://myaccount.google.com/apppasswords) (not your real
password, no business/API forms). Requires 2-Step Verification enabled on
the account.

### 3. Deploy to Modal

```bash
pip install modal
modal setup   # one-time browser login

modal secret create gmail-credentials \
  GMAIL_ADDRESS=you@gmail.com \
  GMAIL_APP_PASSWORD="your 16-character app password"

modal secret create app-config \
  LETTERBOXD_USERNAME=your-username \
  ZIP_CODE=10001 \
  HYPE_LIST_URL="https://letterboxd.com/you/list/hype/share/token/"

modal deploy modal_app.py
```

That's it - `modal_app.py` runs on a 15-minute cron from there. `modal run
modal_app.py` triggers one manual run, useful for confirming setup before
trusting the cron to it.

## How polling frequency works

A film's tier is recomputed fresh every time it's actually checked - it's not
a fixed label, so a film moves between tiers automatically as its release
date approaches and passes.

| Tier | When | Checked |
|---|---|---|
| `must_watch` | On the Hype list | every run (as often as the cron fires) |
| `hot` | Unreleased, ≤90 days out | every ~3h |
| `recent` | Released ≤14 days ago | every ~8h |
| `far_future` / `unknown` | Unreleased >90 days out, or no release date found yet | every ~24h |
| `retired` | Released >14 days ago | every ~180 days (effectively dormant) |

Films whose Letterboxd year is more than 2 years old are excluded from
tracking entirely (assumed to be a revival screening, not an original
release, if anything's playing) - **unless** they're on the Hype list, which
overrides this. Documentaries are excluded too (Fandango/Box Office Mojo
don't track them reliably), with the same Hype override.

### Must-watch films ("Hype")

Some films' tickets go on sale with almost no notice (Dune 3-style), and
`hot` tier's 3-hour cadence isn't enough for those. The fix: make a
Letterboxd list (any name; `HYPE_LIST_URL` just needs to point at it) and add
films to it. Anything in that list:

- Is tracked even if it's not on your watchlist
- Bypasses the 2-year-old cutoff and the documentary exclusion
- Gets checked on literally every scheduled run, not just every few hours

The list can be private - use its share link (the `boxd.it` short link, or
the full `letterboxd.com/.../share/<token>/` URL it redirects to) as
`HYPE_LIST_URL`. One real constraint: a private share link only serves its
**first page** (~28 films) - `/page/2/` 403s on that URL specifically (a
plain public list doesn't have this limit). Keep Hype small and curated and
this never matters.

## Resilience

Fandango (via Akamai's bot-management) can intermittently serve an HTML
block page - or a plain empty body - instead of a real response, on any of
its endpoints this project calls. Confirmed transient in production, not a
persistent IP ban: a blocked request succeeded cleanly moments later with no
other change. Every Fandango request retries through one shared helper
(`fandango._get_with_retry`) with exponential backoff (1.5s, 3s) before
giving up on that one request - never treated as a silent "nothing found,"
since that would be worse than crashing for a project whose whole point is
not missing an on-sale event. `modal_app.py` also pins the deployed function
to `region="us"`, since these are all US-facing sites and requests that look
like ordinary US traffic are plausibly less likely to trip that block in the
first place - a complement to the retry logic, not a replacement for it.

## Diagnosing "why isn't X being tracked/alerted"

```bash
python3 why.py "Dune: Part Three"
```

Prints whether it's in scope, matched, excluded, its current tier, and next
check time - reads real state, no guessing needed.

## Testing

```bash
python3 -m pytest              # fast, network-free (fixtures are real saved HTML)
python3 -m pytest -m live      # hits the actual sites - run after touching a parser
```

## Known limitations

- **Site markup can drift.** Fandango/Letterboxd/Box Office Mojo could change
  their HTML at any time and silently break a parser (no crash, just wrong or
  empty results). `pytest -m live` catches this if you run it; there's no
  automated schedule for it currently - a crashed *run* emails you (see
  `modal_app.py`), but silent parsing degradation, by definition, doesn't
  crash. Re-running the live tests periodically is the mitigation for now.
- **The 2-year cutoff is a proxy, not a guarantee.** Letterboxd's listed year
  can be a festival year rather than the theatrical one, so a film could
  legitimately be excluded a bit early. Add it to Hype to override.
- **Fandango doesn't cover every theater**, especially small/independent
  arthouse chains - matching only works for films Fandango actually
  catalogs.
