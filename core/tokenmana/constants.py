"""Version, platform, file locations and limit window lengths.

Locations follow XDG; ``TOKENMANA_CONFIG_DIR`` and ``TOKENMANA_CACHE_DIR``
override them.
"""
from __future__ import annotations

import os
import sys


VERSION = "0.2.0"
IS_MAC = sys.platform == "darwin"
HOME = os.path.expanduser("~")

CONFIG_DIR = os.environ.get("TOKENMANA_CONFIG_DIR") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "tokenmana")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
SECRETS_PATH = os.path.join(CONFIG_DIR, "secrets.json")
CACHE_DIR = os.environ.get("TOKENMANA_CACHE_DIR") or os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.join(HOME, ".cache"), "tokenmana")
STATE_PATH = os.path.join(CACHE_DIR, "state.json")
SCHEDULE_PATH = os.path.join(CACHE_DIR, "autoping.json")

# GUI apps / shell extensions start with a minimal PATH; add the usual places
# where claude, codex & co. get installed.
EXTRA_PATH = [
    "/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "~/.claude/local",
    "~/.npm-global/bin", "~/.bun/bin", "~/.volta/bin", "~/.cargo/bin", "/snap/bin",
    "~/.nvm/versions/node/*/bin", "~/.local/share/fnm/aliases/default/bin",
]

DEFAULT_REFRESH_MIN = 5
SESSION_WINDOW = 5 * 3600
WEEK_WINDOW = 7 * 86400
