from datetime import datetime, timedelta, timezone

import health
from domain import RunResult
from health import HealthState, RunHealth

from conftest import make_film

T0 = datetime(2026, 11, 3, 7, 45, tzinfo=timezone.utc)


def _broken():
    return RunResult(failed=["Werwulf: UnrecognizedSearchPage: search for 'Werwulf': neither results nor a 'No results' page"])


def test_assess_all_checks_failing_is_broken():
    run_health, problem = health.assess(_broken())
    assert run_health == RunHealth.BROKEN
    assert "all 1 film check(s) failed, e.g. Werwulf: UnrecognizedSearchPage" in problem


def test_assess_one_failure_among_successes_is_ok():
    result = RunResult(checked=[make_film()], failed=["Broken Film: Exception: boom"])
    assert health.assess(result) == (RunHealth.OK, None)


def test_assess_unknown_showtime_type_is_broken_even_when_checks_succeed():
    result = RunResult(checked=[make_film()], anomalies=["Werwulf: werwulf-2026: unknown showtime type 'presale' at AMC Metreon 16 2026-12-25"])
    run_health, problem = health.assess(result)
    assert run_health == RunHealth.BROKEN
    assert "unknown showtime type 'presale'" in problem


def test_assess_no_fandango_session_is_broken():
    assert health.assess(RunResult(fandango_unavailable=True))[0] == RunHealth.BROKEN


def test_assess_nothing_due_is_no_signal():
    assert health.assess(RunResult()) == (RunHealth.NO_SIGNAL, None)


def _run_for(hours, step=timedelta(minutes=15)):
    """Simulates broken runs every 15 min for `hours`; returns (state, emails)."""
    state, emails, now = HealthState(), [], T0
    while now <= T0 + timedelta(hours=hours):
        state, email = health.update(state, RunHealth.BROKEN, "all checks failed", now, "logs/x.log")
        if email:
            emails.append((now, email))
        now += step
    return state, emails


def test_a_four_hour_outage_like_nov_3_sends_nothing():
    state, emails = _run_for(4)
    assert emails == []
    assert state.failing_since == T0


def test_six_hours_broken_sends_exactly_one_email():
    state, emails = _run_for(10)
    assert len(emails) == 1
    sent_at, email = emails[0]
    assert sent_at == T0 + timedelta(hours=6)
    assert "may be missing alerts" in email.subject
    assert "logs/x.log" in email.body
    assert state.alerted_at == sent_at


def test_recovery_after_an_alert_sends_one_email_and_resets():
    state, _ = _run_for(7)
    state, email = health.update(state, RunHealth.OK, None, T0 + timedelta(hours=8), None)
    assert "working again" in email.subject
    assert state == HealthState()


def test_recovery_without_an_alert_sends_nothing():
    state, _ = _run_for(2)
    state, email = health.update(state, RunHealth.OK, None, T0 + timedelta(hours=3), None)
    assert email is None
    assert state == HealthState()


def test_no_signal_runs_dont_reset_or_extend_the_state():
    state, _ = _run_for(2)
    same, email = health.update(state, RunHealth.NO_SIGNAL, None, T0 + timedelta(hours=3), None)
    assert same == state and email is None
