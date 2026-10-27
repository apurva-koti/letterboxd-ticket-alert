"""A copy of each scheduler run's log, written as a text file on the Modal
Volume - Modal's own logs only go back ~24h, too short to investigate
anything that happened over several days (confirmed: Misty Green's repeat
emails spanned four days, and most of the evidence had expired by the
time it was looked at).

One file per run, `<dir>/<YYYY-MM-DD>/<HH-MM>.log` (UTC), so no two
containers ever write the same file. Read them with:
    modal volume get letterboxd-ticket-alert-db logs/2026-10-27 .
"""

from __future__ import annotations

import logging
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(message)s"
RETENTION_DAYS = 30


def start(log_dir: str | Path, now: datetime, logger: logging.Logger) -> logging.Handler:
    path = Path(log_dir) / f"{now:%Y-%m-%d}" / f"{now:%H-%M}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(FORMAT))
    logger.addHandler(handler)
    return handler


def stop(handler: logging.Handler, logger: logging.Logger) -> None:
    logger.removeHandler(handler)
    handler.close()


def prune(log_dir: str | Path, today: date, keep_days: int = RETENTION_DAYS) -> list[str]:
    """Deletes day folders older than `keep_days`. Returns what was removed."""
    removed = []
    root = Path(log_dir)
    if not root.exists():
        return removed
    cutoff = today - timedelta(days=keep_days)
    for day_dir in root.iterdir():
        try:
            day = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if day < cutoff:
            shutil.rmtree(day_dir)
            removed.append(day_dir.name)
    return removed
