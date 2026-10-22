"""Finds every Fandango listing that could plausibly be a given film.

Fandango's own "release date" field is never used - verified unreliable in
production: a listing can be reused for a re-release without that field
ever being updated (American Psycho's listing still shows its 2000 release
for a real 2026 re-release), while a different re-release (Moonlight's 10th
anniversary) gets an entirely separate listing instead. Release year has
the same problem, since it's derived from the same stale metadata.

Signals are checked in a fixed hierarchy, most to least discriminating:
director, then synopsis, then runtime, then title alone. Each level only
decides anything when it actually has data on both sides - missing data
falls through to the next level, but an actual contradiction at any level
is a hard stop, never second-guessed by a weaker signal below it (confirmed
necessary in production: letting runtime override a director contradiction
would risk reopening the exact false match "Casino Royale" made against
"Casino"). Every surviving candidate still has to be checked for live
showtimes by the caller - no single property of a listing reliably picks
one "best" match in advance.

Before any of that, the candidate's title has to actually CONTAIN the
real title as a substring, once normalized - not just score high enough
on fuzzy similarity. A re-release's title only ever adds words around the
original ("Moonlight" -> "Moonlight 10th Anniversary Remastered"), it
never replaces them, so requiring literal containment costs nothing for a
real match. It's not redundant with the fuzzy floor below: two titles can
share enough characters to clear a low fuzzy threshold while one doesn't
contain the other at all (confirmed in production: "The Metropolitan
Opera: Macbeth" scored 0.450 against "Metropolis" - higher than
Moonlight's real anniversary listing's own 0.391 - purely from the shared
"Metropoli" prefix, even though "Metropolis" never appears in that title).

Runtime is checked one-sided: a candidate noticeably SHORTER than the
original is suspicious, but longer is expected and never penalized - a
re-release event adds an intro or retrospective featurette, it doesn't cut
the film (confirmed in production: Moonlight's real 10th-anniversary
listing runs 17 minutes longer than the film itself, and its own synopsis
explains why - "...an exclusive conversation with director Barry Jenkins
... to follow the feature").
"""

from __future__ import annotations

import logging
import re
import unicodedata
from difflib import SequenceMatcher

from domain import FandangoListing
from fandango_client import FandangoClient

logger = logging.getLogger("letterboxd-ticket-alert")

MIN_TITLE_SCORE = 0.3
TITLE_ONLY_MATCH_THRESHOLD = 0.85
DIRECTOR_MATCH_THRESHOLD = 0.6
RUNTIME_SHORTER_TOLERANCE_MINUTES = 5


def find_candidates(
    client: FandangoClient, title: str, director: str | None, runtime: int | None = None, year: int | None = None
) -> list[FandangoListing]:
    results = client.search(title)
    logger.info(
        f"[match] {title!r} (dir={director!r}, runtime={runtime}, year={year}): "
        f"search returned {len(results)}: {[f'{c.title} ({c.year}) {c.slug}' for c in results]}"
    )
    survivors = []
    for c in results:
        label = f"{c.title} ({c.year}) {c.slug}"
        if not _title_contains(title, c.title):
            logger.info(f"[match]   drop {label}: title doesn't contain {title!r}")
            continue
        score = _similarity(title, c.title)
        if score < MIN_TITLE_SCORE:
            logger.info(f"[match]   drop {label}: title similarity {score:.3f} < {MIN_TITLE_SCORE}")
            continue
        if _year_too_early(year, c.year):
            logger.info(f"[match]   drop {label}: year {c.year} predates {year}")
            continue
        keep, reason = _is_match(client, c, title, director, runtime)
        logger.info(f"[match]   {'keep' if keep else 'drop'} {label}: {reason}")
        if keep:
            survivors.append(c)
    return survivors


def _title_contains(title: str, candidate_title: str) -> bool:
    return _normalize(title) in _normalize(candidate_title)


def _year_too_early(target_year: int | None, candidate_year: int | None) -> bool:
    """A re-release always happens after the film's own production year,
    never before - a candidate whose own displayed year predates the
    tracked film's cannot be a legitimate re-release of it, no matter how
    similar the title. This is what separates a brand-new remake from an
    unrelated older film's re-release listings sharing the same title
    (confirmed necessary: a 2026 remake's candidates must not be polluted
    by the original 1995 film's "30th Anniversary (2025)" listing - 2025
    predates 2026, so it's rejected before director/runtime even matter)."""
    if target_year is None or candidate_year is None:
        return False
    return candidate_year < target_year


def _is_match(
    client: FandangoClient, c: FandangoListing, title: str, director: str | None, runtime: int | None
) -> tuple[bool, str]:
    """Returns (keep, reason) - the reason is logged for every candidate."""
    c.director, c.runtime, synopsis = client.fetch_details(c.slug) if (director or runtime) else (None, None, None)

    if director and c.director:
        ok = _directors_match(director, c.director)
        return ok, f"director {c.director!r} {'matches' if ok else 'contradicts'} {director!r}"
    if director and synopsis and _mentions_director(synopsis, director):
        return True, f"no director listed, synopsis mentions {director!r}"
    if runtime and c.runtime:
        too_short = _runtime_too_short(runtime, c.runtime)
        return not too_short, f"no director signal, runtime {c.runtime} vs {runtime} ({'too short' if too_short else 'ok'})"
    score = _similarity(title, c.title)
    ok = score >= TITLE_ONLY_MATCH_THRESHOLD
    return ok, f"no director/runtime signal, title-only similarity {score:.3f} vs {TITLE_ONLY_MATCH_THRESHOLD}"


def _mentions_director(synopsis: str, director: str) -> bool:
    """Event listings with no director metadata at all almost always
    describe the event in prose instead - confirmed in production:
    Moonlight's anniversary listing has no director field, but its
    synopsis says "...an exclusive conversation with director Barry
    Jenkins..." """
    surname_tokens = _normalize(director).split()
    if not surname_tokens:
        return False
    return surname_tokens[-1] in _normalize(synopsis)


def _runtime_too_short(target: int, candidate: int) -> bool:
    return candidate < target - RUNTIME_SHORTER_TOLERANCE_MINUTES


def _directors_match(a: str, b: str) -> bool:
    """Surname is the primary signal, not the full name - a shared first
    name can inflate plain character-sequence similarity enough to clear
    the threshold on its own (confirmed in production: "Martin Scorsese"
    vs "Martin Campbell", Casino Royale's director, scored 0.6 - exactly
    the threshold - from the shared "Martin" alone). But surname alone
    isn't safe either, since real director families share one (Wes
    Anderson vs Paul Thomas Anderson) - the first name's initial has to
    agree too."""
    tokens_a, tokens_b = _normalize(a).split(), _normalize(b).split()
    if not tokens_a or not tokens_b:
        return True
    if _similarity(tokens_a[-1], tokens_b[-1]) < DIRECTOR_MATCH_THRESHOLD:
        return False
    return tokens_a[0][0] == tokens_b[0][0]


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text)).strip()
