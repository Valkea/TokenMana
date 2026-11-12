"""Fetching accounts, caching the results and building the status document the UIs read."""
from __future__ import annotations

import concurrent.futures
import os

from .config import enabled_here
from .constants import CONFIG_PATH, DEFAULT_REFRESH_MIN, SCHEDULE_PATH, STATE_PATH
from .errors import ProviderError
from .metrics import decay
from .providers import provider
from .providers.claude import claude_is_default_dir
from .providers.copilot import copilot_host
from .schedule import auto_ping_info
from .util import expand, iso, parse_time, read_json, utcnow, write_json


def refresh_seconds(acc, cfg):
    """Return how often an account is refreshed.

    Parameters
    ----------
    acc : dict
        Account config.
    cfg : dict
        Loaded config.

    Returns
    -------
    float
        Seconds, from the account, its provider, the config or the default,
        in that order; at least 60.
    """
    m = acc.get("refresh_minutes") or provider(acc).get("refresh_minutes") \
        or cfg.get("refresh_minutes") or DEFAULT_REFRESH_MIN
    return max(1, float(m)) * 60


def fetch_into_state(acc, state):
    """Fetch one account and store the outcome in the cache.

    Never raises: errors are recorded in the entry so that one provider
    cannot break the others.

    Parameters
    ----------
    acc : dict
        Account config.
    state : dict
        Cache by account id (``state.json``), updated in place. The entry gets
        ``fetched_at``, ``status`` (``ok``, ``stale``, ``error`` or ``setup``),
        ``error``, ``hint`` and, on success, ``data`` and ``ok_at``. Previous
        data is kept on error.
    """
    entry = state.get(acc["id"]) or {}
    entry["fetched_at"] = iso(utcnow())
    try:
        data = provider(acc)["fetch"](acc)
        partial = data.pop("_partial_error", None)
        entry.update(data=data, error=partial, hint=None, status="stale" if partial else "ok")
        if not partial:
            entry["ok_at"] = entry["fetched_at"]
    except ProviderError as e:
        entry.update(error=str(e), hint=e.hint, status="setup" if e.setup else "error")
    except Exception as e:  # never let one provider break the others
        entry.update(error=f"{type(e).__name__}: {e}", hint=None, status="error")
    state[acc["id"]] = entry


def cli_command(acc):
    """Return the shell command that uses this account from a terminal.

    Parameters
    ----------
    acc : dict
        Account config.

    Returns
    -------
    str or None
        E.g. ``"CLAUDE_CONFIG_DIR=~/.claude-work claude"``; None for types
        without a CLI.
    """
    if acc["type"] == "claude":
        return "claude" if claude_is_default_dir(acc) else f"CLAUDE_CONFIG_DIR={acc['config_dir']} claude"
    if acc["type"] == "codex":
        home = acc.get("codex_home") or "~/.codex"
        return "codex" if os.path.realpath(expand(home)) == os.path.realpath(expand("~/.codex")) else f"CODEX_HOME={home} codex"
    return None


def build_account(acc, entry, now, sched=None):
    """Build one account in the normalized output shape.

    Parameters
    ----------
    acc : dict
        Account config.
    entry : dict
        The account's cached fetch from ``state.json``.
    now : datetime.datetime
        Used to decay metrics whose window has ended.
    sched : dict, optional
        Contents of ``autoping.json``.

    Returns
    -------
    dict
        The account with its ``status`` (``pending``, ``ok``, ``stale``,
        ``error`` or ``setup``), capabilities, links, ``metrics``, ``notes``
        and ``auto_ping``.
    """
    prov = provider(acc)
    data = entry.get("data") or {}
    status = entry.get("status") or "pending"
    if status in ("error", "setup") and data:
        status = "stale" if status == "error" else status
    return {
        "id": acc["id"], "label": acc.get("label") or acc["id"], "type": acc["type"],
        "vendor": acc.get("vendor") or data.get("vendor") or prov["vendor"],
        "plan": acc.get("plan") or data.get("plan"), "identity": data.get("identity"),
        "status": status, "error": entry.get("error"), "hint": entry.get("hint"),
        "fetched_at": entry.get("fetched_at"), "ok_at": entry.get("ok_at"),
        "can_ping": bool(prov["ping"]) and acc.get("ping") is not False,
        "can_login": bool(prov["login"]),
        "cli": cli_command(acc),
        "dashboard_url": acc.get("dashboard_url", f"https://{copilot_host(acc)}/settings/copilot"
                                 if acc["type"] == "copilot" else prov["dashboard"]),
        "host": copilot_host(acc) if acc["type"] == "copilot" else None,
        "group": (acc.get("group") or "").strip() or None,
        "metrics": [decay(m, now) for m in data.get("metrics", [])] if status != "setup" else [],
        "notes": data.get("notes", []) if status != "setup" else [],
        "auto_ping": auto_ping_info(acc, (sched or {}).get(acc["id"]), entry, now),
    }


def summarize(cfg, accounts):
    """Pick the metric shown in the menu bar.

    Parameters
    ----------
    cfg : dict
        Loaded config; ``menubar`` is ``"max"`` (highest percent of all
        accounts) or ``"ACCOUNT_ID:METRIC_KEY"``.
    accounts : list of dict
        Accounts from `build_account`.

    Returns
    -------
    dict or None
        ``percent``, ``account``, ``metric`` and ``label``; None when no
        metric has a percent.
    """
    pick = cfg.get("menubar") or "max"
    best = None
    for a in accounts:
        if a["status"] not in ("ok", "stale"):
            continue
        for m in a["metrics"]:
            if m.get("percent") is None:
                continue
            if pick != "max" and pick != f"{a['id']}:{m['key']}":
                continue
            if best is None or m["percent"] > best["percent"]:
                best = {"percent": m["percent"], "account": a["id"], "metric": m["key"],
                        "label": f"{a['label']} — {m['label']}"}
    return best


GROUP_PALETTE = ["blue", "green", "purple", "orange", "pink", "teal", "red", "gray"]


def group_colors(cfg, groups):
    """Assign a colour to each group.

    Parameters
    ----------
    cfg : dict
        Loaded config; its ``group_colors`` override the palette.
    groups : list of str
        Groups in display order.

    Returns
    -------
    dict of str to str
        Colour name by group; groups without one get a `GROUP_PALETTE` colour
        by position.
    """
    conf = {k.casefold(): v for k, v in (cfg.get("group_colors") or {}).items()}
    return {g: conf.get(g.casefold()) or GROUP_PALETTE[i % len(GROUP_PALETTE)] for i, g in enumerate(groups)}


def group_rank(cfg, group):
    """Return the sort key of a group.

    Groups listed in the config's ``groups`` come first, in that order, then
    the others alphabetically, then ungrouped accounts.

    Parameters
    ----------
    cfg : dict
        Loaded config.
    group : str or None

    Returns
    -------
    tuple of (int, str)
    """
    order = [g.casefold() for g in cfg.get("groups") or []]
    if not group:
        return (2, "")
    g = group.casefold()
    return (0, f"{order.index(g):04d}") if g in order else (1, g)


def collect(cfg, ids=None, force=False, cached=False):
    """Refresh the accounts that are due and build the status output.

    Parameters
    ----------
    cfg : dict
        Loaded config.
    ids : list of str, optional
        Only these accounts; by default all accounts enabled on this device.
    force : bool, default False
        Refetch even when the cache is fresh.
    cached : bool, default False
        Never fetch; use the cache only.

    Returns
    -------
    dict
        The JSON document read by the UIs: ``version``, ``generated_at``,
        ``refresh_minutes``, ``config_path``, ``summary``, ``groups``,
        ``group_colors`` and ``accounts`` (sorted by group, then label).
    """
    state = read_json(STATE_PATH, {}) or {}
    accounts = [a for a in cfg["accounts"] if enabled_here(a) and (not ids or a["id"] in ids)]
    now = utcnow()
    due = []
    if not cached:
        for a in accounts:
            last = parse_time((state.get(a["id"]) or {}).get("fetched_at"))
            if force or not last or (now - last).total_seconds() >= refresh_seconds(a, cfg) - 5:
                due.append(a)
    if due:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda a: fetch_into_state(a, state), due))
        write_json(STATE_PATH, state)
    now = utcnow()
    sched = read_json(SCHEDULE_PATH, {}) or {}
    out = [build_account(a, state.get(a["id"]) or {}, now, sched) for a in accounts]
    out.sort(key=lambda a: (group_rank(cfg, a["group"]), a["label"].casefold()))
    groups = sorted({a["group"] for a in out if a["group"]} | set(cfg.get("groups") or []),
                    key=lambda g: group_rank(cfg, g))
    return {"version": 1, "generated_at": iso(now), "refresh_minutes": cfg.get("refresh_minutes", DEFAULT_REFRESH_MIN),
            "config_path": CONFIG_PATH, "summary": summarize(cfg, out), "groups": groups,
            "group_colors": group_colors(cfg, groups), "accounts": out}
