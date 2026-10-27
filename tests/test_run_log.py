import logging
from datetime import date, datetime, timezone

import run_log


def test_each_run_writes_its_own_file_under_its_utc_day(tmp_path):
    logger = logging.getLogger("test-run-log")
    logger.setLevel(logging.INFO)
    handler = run_log.start(tmp_path, datetime(2026, 10, 27, 4, 45, tzinfo=timezone.utc), logger)
    logger.info("Run starting: username=x zip=94158")
    run_log.stop(handler, logger)

    text = (tmp_path / "2026-10-27" / "04-45.log").read_text()
    assert "INFO Run starting: username=x zip=94158" in text
    assert handler not in logger.handlers


def test_prune_removes_only_day_folders_past_retention(tmp_path):
    for name in ["2026-09-26", "2026-09-27", "2026-10-27", "not-a-date"]:
        (tmp_path / name).mkdir()
    removed = run_log.prune(tmp_path, date(2026, 10, 27), keep_days=30)
    assert removed == ["2026-09-26"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-09-27", "2026-10-27", "not-a-date"]


def test_prune_with_no_log_dir_yet_is_a_no_op(tmp_path):
    assert run_log.prune(tmp_path / "missing", date(2026, 10, 27)) == []
