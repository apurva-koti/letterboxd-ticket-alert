"""Orchestrates one tracking pass: sync the watchlist, then check whatever's due."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone

import matcher
from domain import Film, RunResult, Tier, TicketStatus, TrackedFilm
from fandango_client import FandangoClient
from letterboxd_client import LetterboxdClient, pick_anchor
from repository import FilmRepository
from tiering import compute_tier, is_due, next_check_at

logger = logging.getLogger("letterboxd-ticket-alert")

MAX_PROCESSED_PER_RUN = 20
REQUEST_DELAY_SECONDS = 0.3
DOCUMENTARY_GENRE = "documentary"

TIER_MEANINGS = {
    Tier.MUST_WATCH: "must-watch, checked every run",
    Tier.HOT: "hot, checked every ~3h",
    Tier.RECENT: "recent, checked every ~8h",
    Tier.FAR_FUTURE: "far future, checked every ~24h",
    Tier.UNKNOWN: "no release date yet, checked every ~24h",
    Tier.RETIRED: "retired, checked every ~7d",
}


def _describe(tier: Tier, anchor: date | None, today: date) -> str:
    meaning = TIER_MEANINGS[tier]
    if anchor is None:
        return meaning
    days = (anchor - today).days
    if days > 0:
        when = f"releases {anchor}, {_humanize(days)} away"
    elif days == 0:
        when = f"releases {anchor}, today"
    else:
        when = f"released {anchor}, {_humanize(-days)} ago"
    return f"{when} ({meaning})"


def _humanize(days: int) -> str:
    if days >= 365:
        return f"~{days // 365}y"
    if days >= 30:
        return f"~{days // 30}mo"
    return f"{days}d"


class Tracker:
    def __init__(self, repo: FilmRepository, letterboxd: LetterboxdClient) -> None:
        self.repo = repo
        self.letterboxd = letterboxd

    def run(
        self,
        username: str,
        zip_code: str,
        hype_list_url: str | None = None,
        blacklisted_theaters: frozenset[str] = frozenset(),
    ) -> RunResult:
        result = RunResult()
        now = datetime.now(timezone.utc)

        fandango = self._new_fandango_session()

        watchlist = self._fetch_watchlist(username)
        hype_films = self._fetch_hype_list(hype_list_url)
        hype_slugs = {f.slug for f in hype_films}

        if watchlist is not None:
            by_slug = {f.slug: f for f in watchlist}
            for f in hype_films:
                by_slug.setdefault(f.slug, f)
            result.added, result.removed = self.repo.sync_watchlist(list(by_slug.values()))

        if fandango is None:
            return result

        processed = 0
        for film in self.repo.all_films():
            is_hype = film.slug in hype_slugs
            tracked = self.repo.get(film.slug)
            assert tracked is not None  # just came from all_films()

            if tracked.excluded_reason and not is_hype:
                continue
            if not is_due(tracked.next_check_at, now):
                continue
            if not is_hype and processed >= MAX_PROCESSED_PER_RUN:
                continue
            processed += 1

            time.sleep(REQUEST_DELAY_SECONDS)
            try:
                self._process_film(fandango, film, tracked, is_hype, zip_code, blacklisted_theaters, now, result)
            except Exception:
                logger.warning(f"{film.title} ({film.year}): check failed, will retry next run", exc_info=True)

        return result

    def _process_film(
        self,
        fandango: FandangoClient,
        film: Film,
        tracked: TrackedFilm,
        is_hype: bool,
        zip_code: str,
        blacklisted_theaters: frozenset[str],
        now: datetime,
        result: RunResult,
    ) -> None:
        today = now.date()
        anchor = tracked.anchor_date
        director = tracked.director
        runtime = tracked.runtime
        poster_url = tracked.poster_url
        tier = compute_tier(anchor, today, is_hype)

        if tier in (Tier.UNKNOWN, Tier.RETIRED) or is_hype:
            refreshed = self._refresh_from_letterboxd(film, is_hype, now)
            if refreshed is None:
                return  # excluded as documentary - already saved + logged
            anchor, director, runtime, poster_url, tier = refreshed

        self._check_fandango(
            fandango,
            film,
            director,
            runtime,
            poster_url,
            tracked.status,
            tier,
            anchor,
            today,
            zip_code,
            blacklisted_theaters,
            now,
            result,
        )

    def _refresh_from_letterboxd(
        self, film: Film, is_hype: bool, now: datetime
    ) -> tuple[date | None, str | None, int | None, str | None, Tier] | None:
        """Returns (anchor, director, runtime, poster_url, tier), or None if
        the film was excluded (saved + logged already)."""
        today = now.date()
        release = self.letterboxd.fetch_release(film.slug)

        if DOCUMENTARY_GENRE in release.genres and not is_hype:
            self.repo.save_match(film.slug, None, None, excluded_reason=DOCUMENTARY_GENRE)
            logger.info(f"{film.title} ({film.year}): excluded, documentary")
            return None

        anchor = pick_anchor(release.us_dates, today)
        tier = compute_tier(anchor, today, is_hype)
        return anchor, release.director, release.runtime, release.poster_url, tier

    def _check_fandango(
        self,
        fandango: FandangoClient,
        film: Film,
        director: str | None,
        runtime: int | None,
        poster_url: str | None,
        previous_status: TicketStatus,
        tier: Tier,
        anchor: date | None,
        today: date,
        zip_code: str,
        blacklisted_theaters: frozenset[str],
        now: datetime,
        result: RunResult,
    ) -> None:
        """Checks every Fandango listing that could plausibly be this film,
        not just one picked in advance - a re-release can live in a brand
        new listing (Moonlight's 10th anniversary) or the original one
        reused without its stored date ever updating (American Psycho) -
        no single "best" candidate covers both, so every one gets checked."""
        candidates = matcher.find_candidates(fandango, film.title, director, runtime, film.year)

        on_sale, showtimes_only, active = set(), set(), None
        for c in candidates:
            check = fandango.check_showtimes(c.fandango_id, c.slug, zip_code)
            if not check:
                continue
            on_sale.update(check.on_sale_theaters)
            showtimes_only.update(check.showtimes_only_theaters)
            if active is None or check.status == TicketStatus.ON_SALE:
                active = c

        # A showing only at a blacklisted theater is treated as if it were
        # never on sale at all - not just "don't email about it" - so the
        # status itself stays clear of ON_SALE and a real alert still fires
        # the moment it reaches anywhere else, instead of being suppressed
        # forever because the status already flipped once.
        alertable_on_sale = on_sale - blacklisted_theaters
        alertable_showtimes_only = showtimes_only - blacklisted_theaters
        if alertable_on_sale:
            new_status = TicketStatus.ON_SALE
        elif alertable_showtimes_only:
            new_status = TicketStatus.SHOWTIMES_ANNOUNCED
        else:
            new_status = TicketStatus.NONE
        should_alert = new_status == TicketStatus.ON_SALE and previous_status != TicketStatus.ON_SALE

        self.repo.save_match(film.slug, active, anchor, director=director, runtime=runtime, poster_url=poster_url)
        self.repo.save_status(
            film.slug, new_status, today, sorted(on_sale), sorted(showtimes_only), should_alert, tier, next_check_at(tier, now)
        )

        if should_alert:
            note = f", ALERT at {', '.join(sorted(on_sale))}"
        elif not candidates:
            note = ", no Fandango listing found"
        else:
            note = ""
        desc = _describe(tier, anchor, today)
        logger.info(f"{film.title} ({film.year}) [{desc}]: {previous_status} -> {new_status}{note}")

        result.checked.append(film)
        if should_alert:
            result.alerted.append(film)

    def _new_fandango_session(self) -> FandangoClient | None:
        try:
            return FandangoClient()
        except Exception:
            logger.warning("Fandango session failed - skipping ticket checks this run", exc_info=True)
            return None

    def _fetch_watchlist(self, username: str) -> list[Film] | None:
        try:
            return self.letterboxd.fetch_watchlist(username)
        except Exception:
            logger.warning("Watchlist fetch failed - skipping resync this run", exc_info=True)
            return None

    def _fetch_hype_list(self, hype_list_url: str | None) -> list[Film]:
        if not hype_list_url:
            return []
        try:
            return self.letterboxd.fetch_list(hype_list_url)
        except Exception:
            return []
