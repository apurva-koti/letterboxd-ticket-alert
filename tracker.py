"""Orchestrates one tracking pass: sync the watchlist, then check whatever's due."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone

import matcher
from domain import FandangoListing, Film, RunResult, Tier, TicketStatus, TrackedFilm
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

    def run(self, username: str, zip_code: str, hype_list_url: str | None = None) -> RunResult:
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
                self._process_film(fandango, film, tracked, is_hype, zip_code, now, result)
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
        now: datetime,
        result: RunResult,
    ) -> None:
        today = now.date()
        anchor = tracked.anchor_date
        listing = tracked.fandango
        tier = compute_tier(anchor, today, is_hype)

        if tier == Tier.RETIRED and listing:
            self._retire(film, anchor, today, next_check_at(Tier.RETIRED, now))
            return

        refreshing = tier in (Tier.UNKNOWN, Tier.RETIRED) or is_hype
        if refreshing:
            outcome = self._refresh_from_letterboxd(fandango, film, anchor, listing, is_hype, now)
            if outcome is None:
                return  # already scheduled + logged inside
            anchor, listing, tier = outcome

        if listing is None:
            # No Fandango listing to check yet. Still schedule a real
            # recheck (same tier's own interval) instead of leaving
            # next_check_at unset - unmatched films are otherwise "always
            # due" and, in enough numbers, can starve everything behind them.
            self.repo.save_status(film.slug, TicketStatus.NONE, None, [], [], False, tier, next_check_at(tier, now))
            if not refreshing:  # _refresh_from_letterboxd already logged its own outcome
                logger.info(f"{film.title} ({film.year}) [{_describe(tier, anchor, today)}]: still no Fandango listing")
            return

        self._check_showtimes(fandango, film, listing, tracked.status, anchor, tier, today, zip_code, now, result)

    def _retire(self, film: Film, anchor: date | None, today: date, next_check: datetime) -> None:
        self.repo.clear_fandango_binding(film.slug)
        self.repo.reset_for_retirement(film.slug, next_check)
        desc = _describe(Tier.RETIRED, anchor, today)
        logger.info(f"{film.title} ({film.year}) [{desc}]: dropped stale Fandango listing")

    def _refresh_from_letterboxd(
        self,
        fandango: FandangoClient,
        film: Film,
        anchor: date | None,
        listing: FandangoListing | None,
        is_hype: bool,
        now: datetime,
    ) -> tuple[date | None, FandangoListing | None, Tier] | None:
        """Returns (anchor, listing, tier), or None if fully handled (saved + logged) already."""
        today = now.date()
        release = self.letterboxd.fetch_release(film.slug)

        if DOCUMENTARY_GENRE in release.genres and not is_hype:
            self.repo.save_match(film.slug, None, None, excluded_reason=DOCUMENTARY_GENRE)
            logger.info(f"{film.title} ({film.year}): excluded, documentary")
            return None

        new_anchor = pick_anchor(release.us_dates, today)
        anchor_changed = new_anchor != anchor
        tier = compute_tier(new_anchor, today, is_hype)
        desc = _describe(tier, new_anchor, today)

        if new_anchor is None and not is_hype:
            self.repo.save_match(film.slug, None, None, poster_url=release.poster_url)
            self.repo.save_status(film.slug, TicketStatus.NONE, None, [], [], False, tier, next_check_at(tier, now))
            logger.info(f"{film.title} ({film.year}) [{desc}]: no US release date on Letterboxd yet")
            return None

        if new_anchor is not None and tier == Tier.RETIRED:
            self.repo.save_match(film.slug, None, new_anchor, poster_url=release.poster_url)
            self.repo.reset_for_retirement(film.slug, next_check_at(tier, now))
            logger.info(f"{film.title} ({film.year}) [{desc}]: no active listing")
            return None

        if listing is None or anchor_changed:
            if new_anchor is not None:
                listing = matcher.find_anchored_match(fandango, film.title, new_anchor, release.director)
            if listing is None and is_hype:
                listing = matcher.find_match_by_title(fandango, film.title, release.director)
            self.repo.save_match(film.slug, listing, new_anchor, poster_url=release.poster_url)
            found = f"matched '{listing.title}' ({listing.year})" if listing else "no Fandango listing found"
            logger.info(f"{film.title} ({film.year}) [{desc}]: {found}")

        return new_anchor, listing, tier

    def _check_showtimes(
        self,
        fandango: FandangoClient,
        film: Film,
        listing: FandangoListing,
        previous_status: TicketStatus,
        anchor: date | None,
        tier: Tier,
        today: date,
        zip_code: str,
        now: datetime,
        result: RunResult,
    ) -> None:
        check = fandango.check_showtimes(listing.fandango_id, listing.slug, zip_code)
        new_status = check.status if check else TicketStatus.NONE
        should_alert = new_status == TicketStatus.ON_SALE and previous_status != TicketStatus.ON_SALE

        self.repo.save_status(
            film.slug,
            new_status,
            check.checked_date if check else None,
            check.on_sale_theaters if check else [],
            check.showtimes_only_theaters if check else [],
            should_alert,
            tier,
            next_check_at(tier, now),
        )

        alert_note = f", ALERT at {', '.join(check.on_sale_theaters)}" if should_alert else ""
        desc = _describe(tier, anchor, today)
        logger.info(f"{film.title} ({film.year}) [{desc}]: {previous_status} -> {new_status}{alert_note}")

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
