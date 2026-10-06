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

Runtime is checked one-sided: a candidate noticeably SHORTER than the
original is suspicious, but longer is expected and never penalized - a
re-release event adds an intro or retrospective featurette, it doesn't cut
the film (confirmed in production: Moonlight's real 10th-anniversary
listing runs 17 minutes longer than the film itself, and its own synopsis
explains why - "...an exclusive conversation with director Barry Jenkins
... to follow the feature").
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from domain import FandangoListing
from fandango_client import FandangoClient

MIN_TITLE_SCORE = 0.3
TITLE_ONLY_MATCH_THRESHOLD = 0.85
DIRECTOR_MATCH_THRESHOLD = 0.6
RUNTIME_SHORTER_TOLERANCE_MINUTES = 5


def find_candidates(
    client: FandangoClient, title: str, director: str | None, runtime: int | None = None, year: int | None = None
) -> list[FandangoListing]:
    plausible = [c for c in client.search(title) if _similarity(title, c.title) >= MIN_TITLE_SCORE]
    plausible = [c for c in plausible if not _year_too_early(year, c.year)]
    return [c for c in plausible if _is_match(client, c, title, director, runtime)]


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


def _is_match(client: FandangoClient, c: FandangoListing, title: str, director: str | None, runtime: int | None) -> bool:
    c.director, c.runtime, synopsis = client.fetch_details(c.slug) if (director or runtime) else (None, None, None)

    if director and c.director:
        return _directors_match(director, c.director)
    if director and synopsis and _mentions_director(synopsis, director):
        return True
    if runtime and c.runtime:
        return not _runtime_too_short(runtime, c.runtime)
    return _similarity(title, c.title) >= TITLE_ONLY_MATCH_THRESHOLD


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
