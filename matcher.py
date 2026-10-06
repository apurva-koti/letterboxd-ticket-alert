"""Finds every Fandango listing that could plausibly be a given film.

Fandango's own "release date" field is not used at all - verified
unreliable in production: a listing can be reused for a re-release without
that field ever being updated (American Psycho's listing still shows its
2000 release for a real 2026 re-release), while a different re-release
(Moonlight's 10th anniversary) gets an entirely separate listing instead.
Neither case has one "best" candidate to pick in advance by any property of
the listing itself - every surviving candidate has to actually be checked
for live showtimes by the caller.
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


def find_candidates(client: FandangoClient, title: str, director: str | None) -> list[FandangoListing]:
    """Title alone is a weak signal, so with a director to check against,
    the title floor stays low and director does the real filtering - any
    candidate whose director doesn't actively contradict is kept, since a
    missing director (on either side) is a data gap, not a contradiction.
    Without a director to check at all, title is the only signal left, so
    it has to be held to a much stricter bar instead."""
    floor = MIN_TITLE_SCORE if director else TITLE_ONLY_MATCH_THRESHOLD
    plausible = [c for c in client.search(title) if _similarity(title, c.title) >= floor]
    if not director:
        return plausible

    survivors = []
    for c in plausible:
        c.director = client.fetch_director(c.slug)
        if c.director and _similarity(director, c.director) < DIRECTOR_MATCH_THRESHOLD:
            continue
        survivors.append(c)
    return survivors


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text)).strip()
