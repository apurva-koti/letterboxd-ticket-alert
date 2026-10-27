"""Everything one film's check saw, collected as it happens and logged as a
single block - so one film's decision can be read top to bottom instead of
stitched together from interleaved lines.

Keeps every field a decision reads (match reasons, calendar dates, each
showing's time and Fandango type, theater distance). Full raw Fandango
responses are only printed when they're likely to matter: the status
changed, or something unexpected came back (see `anomalies`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

KNOWN_SHOWTIME_TYPES = {"available", "pastshowtime"}


@dataclass
class TheaterShowings:
    name: str
    distance: float | None
    open: list[str] = field(default_factory=list)
    past: list[str] = field(default_factory=list)
    sold_out: list[str] = field(default_factory=list)
    other: list[str] = field(default_factory=list)  # "<type> <time>" - anything not recognized

    def describe(self) -> str:
        parts = []
        for label, times in (("open", self.open), ("sold out", self.sold_out), ("past", self.past), ("other", self.other)):
            if times:
                parts.append(f"{label} {' '.join(sorted(times))}")
        miles = f"{self.distance:.1f}mi" if self.distance is not None else "?mi"
        return f"{self.name:<44} {miles:>6}  {' | '.join(parts) or 'no showings'}"


@dataclass
class DateRead:
    day: date
    limit: int
    theaters: list[TheaterShowings] = field(default_factory=list)
    note: str | None = None


@dataclass
class CandidateTrace:
    label: str
    keep: bool | None = None
    reason: str = ""
    calendar: list[date] | None = None
    calendar_error: str | None = None
    dates: list[DateRead] = field(default_factory=list)
    used_date: date | None = None
    preferred_search: str | None = None
    raw: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    def add_raw(self, what: str, status_code: int, body: str) -> None:
        self.raw.append(f"{what} status={status_code} body={body}")


@dataclass
class CheckTrace:
    query: str = ""
    result_count: int = 0
    candidates: dict[str, CandidateTrace] = field(default_factory=dict)

    def candidate(self, slug: str, label: str | None = None) -> CandidateTrace:
        if slug not in self.candidates:
            self.candidates[slug] = CandidateTrace(label=label or slug)
        return self.candidates[slug]

    @property
    def anomalies(self) -> list[str]:
        return [f"{slug}: {a}" for slug, c in self.candidates.items() for a in c.anomalies]

    def format(self, header: str, include_raw: bool) -> str:
        lines = [f"    search {self.query!r}: {self.result_count} result(s)"]
        for slug, c in self.candidates.items():
            verdict = "KEEP" if c.keep else "drop" if c.keep is False else "    "
            lines.append(f"      {verdict} {c.label}  - {c.reason}")
            if not c.keep:
                continue
            if c.calendar_error:
                lines.append(f"        calendar: none (Fandango: {c.calendar_error!r})")
            elif c.calendar is not None:
                span = f", {c.calendar[0]:%m-%d} .. {c.calendar[-1]:%m-%d}" if c.calendar else ""
                lines.append(f"        calendar: {len(c.calendar)} date(s){span}")
            for d in c.dates:
                used = "  <- used" if d.day == c.used_date else ""
                lines.append(f"        {d.day:%m-%d} ({d.limit} nearest){used}{': ' + d.note if d.note else ''}")
                lines.extend(f"          {t.describe()}" for t in d.theaters)
            if c.preferred_search:
                lines.append(f"        preferred search: {c.preferred_search}")
        if self.anomalies:
            lines.append("    UNEXPECTED: " + "; ".join(self.anomalies))
        raw = [r for c in self.candidates.values() for r in c.raw]
        if (include_raw or self.anomalies) and raw:
            lines.append("    raw responses:")
            lines.extend(f"      {r}" for r in raw)
        return header + "\n" + "\n".join(lines)


def showing_time(showtime: dict) -> str:
    local = showtime.get("dateLocal") or ""
    return local[11:16] if len(local) >= 16 else str(showtime.get("date") or "?")
