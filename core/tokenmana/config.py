"""The config file (``config.json``), stored tokens (``secrets.json``) and account lookup."""
from __future__ import annotations

import glob
import os
import re
import sys

from .constants import CONFIG_DIR, CONFIG_PATH, DEFAULT_REFRESH_MIN, HOME, IS_MAC, SECRETS_PATH
from .util import expand, keychain_read, read_json, write_json


def secrets_load():
    """Load the tokens TokenMana stored itself (``secrets.json``).

    Returns
    -------
    dict of str to str
        Token by account id; empty when there is no file.
    """
    return read_json(SECRETS_PATH, {}) or {}


def secrets_save(account_id, token):
    """Store an account's token in ``secrets.json`` (mode 0600).

    Parameters
    ----------
    account_id : str
    token : str
    """
    data = secrets_load()
    data[account_id] = token
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    write_json(SECRETS_PATH, data, mode=0o600)


def autodetect_accounts():
    """Find the accounts already signed in on this device.

    Looks for Claude Code credentials in the default config dir and for
    ``~/.codex*`` homes containing an ``auth.json``.

    Returns
    -------
    list of dict
        Account configs.
    """
    accs = []
    if (IS_MAC and keychain_read("Claude Code-credentials")) or os.path.exists(expand("~/.claude/.credentials.json")):
        accs.append({"id": "claude", "label": "Claude", "type": "claude"})
    for home in sorted(glob.glob(expand("~/.codex*"))):
        if os.path.exists(os.path.join(home, "auth.json")):
            name = os.path.basename(home).lstrip(".").replace("codex-", "") or "codex"
            accs.append({"id": f"codex-{name}" if name != "codex" else "codex", "label": f"Codex ({name})",
                         "type": "codex", "codex_home": home.replace(HOME, "~")})
    return accs


def load_config():
    """Load ``config.json``, creating it from detected accounts if missing.

    Returns
    -------
    dict
        The config, with an ``accounts`` list.

    Raises
    ------
    SystemExit
        When the file exists but is not valid JSON.
    """
    cfg = read_json(CONFIG_PATH)
    if cfg is None:
        if os.path.exists(CONFIG_PATH):
            raise SystemExit(f"mana: {CONFIG_PATH} is not valid JSON")
        cfg = {"refresh_minutes": DEFAULT_REFRESH_MIN, "menubar": "max", "accounts": autodetect_accounts()}
        write_json(CONFIG_PATH, cfg)
    cfg.setdefault("accounts", [])
    return cfg


def update_account(account_id, **fields):
    """Set fields of one account in ``config.json``.

    Parameters
    ----------
    account_id : str
    **fields
        Values to set.
    """
    cfg = read_json(CONFIG_PATH, {}) or {}
    for a in cfg.get("accounts", []):
        if a.get("id") == account_id:
            a.update(fields)
    write_json(CONFIG_PATH, cfg)


def slugify(text):
    """Make an account id from a label.

    Parameters
    ----------
    text : str or None

    Returns
    -------
    str
        Lower-case letters, digits and dashes, at most 32 characters;
        ``"account"`` when nothing is left.
    """
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:32] or "account"


ACCOUNT_TYPES = {
    "claude": {"name": "Claude (Pro / Max)", "dir_key": "config_dir", "default_dir": "~/.claude"},
    "codex": {"name": "Codex (ChatGPT plan)", "dir_key": "codex_home", "default_dir": "~/.codex"},
    "copilot": {"name": "GitHub Copilot"},
    "command": {"name": "Command (JSON)"},
}


def enabled_here(acc):
    """Tell whether an account is enabled on this device.

    Parameters
    ----------
    acc : dict
        Account config; ``"enabled": false`` disables it and ``platforms``
        (e.g. ``["macos"]``) restricts it.

    Returns
    -------
    bool
    """
    if acc.get("enabled") is False:
        return False
    plats = acc.get("platforms")
    here = "macos" if IS_MAC else "linux"
    return not plats or here in plats or sys.platform in plats


def find_account(cfg, account_id):
    """Return an account of the config by id.

    Parameters
    ----------
    cfg : dict
        Loaded config.
    account_id : str

    Returns
    -------
    dict
        The account config (the same object, so it can be edited).

    Raises
    ------
    SystemExit
        When there is no such account.
    """
    for a in cfg["accounts"]:
        if a.get("id") == account_id:
            return a
    raise SystemExit(f"mana: no account {account_id!r} in {CONFIG_PATH}")


def canonical_group(cfg, name):
    """Reuse the existing spelling of a group name (``"work"`` → ``"Work"``).

    Parameters
    ----------
    cfg : dict
        Loaded config.
    name : str or None

    Returns
    -------
    str
        The existing spelling, or `name` stripped when the group is new.
    """
    name = (name or "").strip()
    for g in list(cfg.get("groups") or []) + [a.get("group") for a in cfg["accounts"]]:
        if g and g.casefold() == name.casefold():
            return g
    return name
