"""Picks which Fandango listing corresponds to a Letterboxd-confirmed release.

Ranked by how close a candidate's own release date is to the anchor, not by
title score - a re-release is often worded very differently from the
original ("Moonlight 10th Anniversary Remastered" vs "Moonlight"), so title
similarity is only a low floor to keep unrelated search results out, and
director is a required confirming check on whichever candidate wins.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher

from domain import FandangoListing
from fandango_client import FandangoClient

MIN_TITLE_SCORE = 0.3
MAX_DATE_DISTANCE_DAYS = 45
TITLE_ONLY_MATCH_THRESHOLD = 0.85
AMBIGUITY_GAP = 0.05
DIRECTOR_MATCH_THRESHOLD = 0.6


def find_anchored_match(client: FandangoClient, title: str, anchor: date, director: str | None) -> FandangoListing | None:
    candidates = [c for c in client.search(title) if _similarity(title, c.title) >= MIN_TITLE_SCORE]
    if not candidates:
        return None

    for c in candidates:
        c.release_date = client.fetch_release_date(c.slug)

    best = min(candidates, key=lambda c: _date_distance(c.release_date, anchor))
    if best.release_date is None or _date_distance(best.release_date, anchor) > MAX_DATE_DISTANCE_DAYS:
        return None

    if director:
        best.director = client.fetch_director(best.slug)
        if best.director and _similarity(director, best.director) < DIRECTOR_MATCH_THRESHOLD:
            return None

    return best


def find_match_by_title(client: FandangoClient, title: str, director: str | None) -> FandangoListing | None:
    """Used only when there's no Letterboxd anchor yet to rank by (a Hype
    film Fandango may have catalogued before Letterboxd confirms a date) -
    a stricter title floor than find_anchored_match, since there's no date
    signal to fall back on."""
    scored = [(_similarity(title, c.title), c) for c in client.search(title)]
    if not scored:
        return None

    scored.sort(key=lambda pair: pair[0], reverse=True)
    top_score, top = scored[0]
    if top_score < TITLE_ONLY_MATCH_THRESHOLD:
        return None

    tied = [c for score, c in scored if top_score - score <= AMBIGUITY_GAP]
    if len(tied) > 1:
        return _resolve_tie_by_director(client, tied, director)

    if director:
        top.director = client.fetch_director(top.slug)
        if top.director and _similarity(director, top.director) < DIRECTOR_MATCH_THRESHOLD:
            return None

    return top


def _resolve_tie_by_director(
    client: FandangoClient, tied: list[FandangoListing], director: str | None
) -> FandangoListing | None:
    if not director:
        return None
    matches = []
    for c in tied:
        c.director = client.fetch_director(c.slug)
        if c.director and _similarity(director, c.director) >= DIRECTOR_MATCH_THRESHOLD:
            matches.append(c)
    return matches[0] if len(matches) == 1 else None


def _date_distance(candidate_date: date | None, anchor: date) -> float:
    if candidate_date is None:
        return float("inf")
    return abs((candidate_date - anchor).days)


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text)).strip()
