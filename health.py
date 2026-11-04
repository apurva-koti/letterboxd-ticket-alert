"""Emails once when Fandango checking has been broken for a while - so a
Fandango restyle or a persistent block shows up within hours instead of
as weeks of silently missed alerts. Every failure mode it watches for was
otherwise just a warning in the logs.

A run is BROKEN when Fandango couldn't be used at all: the session
failed, every film check failed, or Fandango sent a showtime type the code
doesn't recognize (a rename of "available" would otherwise make nothing
look on sale, with no error). It's OK when at least one film was checked
with nothing unexpected, and says nothing either way when no film was due.

Broken for longer than BROKEN_ALERT_AFTER (longer than the ~4h overnight
search outage seen on Nov 3, so an ordinary outage doesn't email) sends
one email; the first OK run after that sends one "back to normal" email.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from domain import RunResult

BROKEN_ALERT_AFTER = timedelta(hours=6)


class RunHealth(StrEnum):
    OK = "ok"
    BROKEN = "broken"
    NO_SIGNAL = "no_signal"


@dataclass
class HealthState:
    failing_since: datetime | None = None
    last_problem: str | None = None
    alerted_at: datetime | None = None


@dataclass
class HealthEmail:
    subject: str
    body: str


def assess(result: RunResult) -> tuple[RunHealth, str | None]:
    """(health, problem description) for one run."""
    if result.fandango_unavailable:
        return RunHealth.BROKEN, "couldn't start a Fandango session"
    if result.anomalies:
        return RunHealth.BROKEN, f"unexpected Fandango data: {result.anomalies[0]}"
    if result.failed and not result.checked:
        return RunHealth.BROKEN, f"all {len(result.failed)} film check(s) failed, e.g. {result.failed[0]}"
    if result.checked:
        return RunHealth.OK, None
    return RunHealth.NO_SIGNAL, None


def update(
    state: HealthState, health: RunHealth, problem: str | None, now: datetime, log_file: str | None
) -> tuple[HealthState, HealthEmail | None]:
    """The new state, and the email to send this run, if any."""
    if health == RunHealth.NO_SIGNAL:
        return state, None

    if health == RunHealth.OK:
        email = None
        if state.alerted_at:
            email = HealthEmail(
                subject="✅ Ticket alerts: Fandango checks working again",
                body=(
                    f"Film checks are succeeding again as of {now:%Y-%m-%d %H:%M} UTC, after failing since "
                    f"{state.failing_since:%Y-%m-%d %H:%M} UTC.\n\nLast problem: {state.last_problem}\n\n"
                    "Films that were due during the outage are being checked now."
                ),
            )
        return HealthState(), email

    failing_since = state.failing_since or now
    new_state = HealthState(failing_since=failing_since, last_problem=problem, alerted_at=state.alerted_at)
    if state.alerted_at is None and now - failing_since >= BROKEN_ALERT_AFTER:
        new_state.alerted_at = now
        hours = (now - failing_since).total_seconds() / 3600
        return new_state, HealthEmail(
            subject="⚠️ Ticket alerts: Fandango checks failing - you may be missing alerts",
            body=(
                f"Every run since {failing_since:%Y-%m-%d %H:%M} UTC (~{hours:.0f}h) has failed to check films "
                f"on Fandango, so no ticket alerts can fire.\n\nLatest problem: {problem}\n\n"
                f"This run's log: {log_file or '(not saved)'} on the letterboxd-ticket-alert-db Volume - "
                "it includes the page or data Fandango returned.\n\n"
                "You'll get one more email when checks work again."
            ),
        )
    return new_state, None
