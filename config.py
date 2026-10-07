"""Deployment-specific configuration - values that identify *who's* running
this, not committed to git (see .gitignore), so this repo can be handed to
someone else without carrying your personal watchlist/location/list data.

Resolution order: environment variables first (how this is provided in
Modal, via the app-config Secret), falling back to local_config.json (for
running things locally - copy local_config.example.json to local_config.json
and fill in your own values).
"""

import json
import os
from pathlib import Path

_LOCAL_CONFIG_PATH = Path(__file__).parent / "local_config.json"


def _load_local_config() -> dict[str, str]:
    if _LOCAL_CONFIG_PATH.exists():
        return json.loads(_LOCAL_CONFIG_PATH.read_text())
    return {}


_file_config = _load_local_config()


def _get(key: str) -> str | None:
    return os.environ.get(key) or _file_config.get(key)


LETTERBOXD_USERNAME = _get("LETTERBOXD_USERNAME")
ZIP_CODE = _get("ZIP_CODE")
HYPE_LIST_URL = _get("HYPE_LIST_URL")  # a Letterboxd list of must-watch films - see README

# Theaters you'd never actually go to, comma-separated, matched against
# Fandango's own theater names (e.g. "AMC Mercado 20, Cinemark Century San Mateo 12").
# A showing that's only at blacklisted theaters is treated as if it were
# never on sale at all - so a real alert still fires the moment it reaches
# anywhere else, instead of being permanently suppressed.
BLACKLISTED_THEATERS = frozenset(t.strip() for t in (_get("BLACKLISTED_THEATERS") or "").split(",") if t.strip())
