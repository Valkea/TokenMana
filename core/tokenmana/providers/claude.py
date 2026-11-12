"""Anthropic Claude (Pro / Max subscription via the Claude Code OAuth login)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess

from ..constants import IS_MAC, SESSION_WINDOW, WEEK_WINDOW
from ..errors import ApiError, ProviderError
from ..metrics import metric
from ..net import http_json
from ..util import expand, first_of_next_month, fmt_money, keychain_read, parse_time, read_json, utcnow


CLAUDE_BETA = "oauth-2025-04-20"

CLAUDE_LIMIT_LABELS = {
    "session": "Session · 5h",
    "weekly_all": "Weekly",
    "weekly_opus": "Weekly · Opus",
    "weekly_sonnet": "Weekly · Sonnet",
    "weekly_scoped": "Weekly",
}


def claude_scope_name(scope):
    """Return a readable name for the scope of a Claude limit.

    Parameters
    ----------
    scope : str, dict or None
        A string, an object with ``display_name`` / ``id``, or a dict of such
        values (e.g. ``{"model": {...}, "surface": ...}``) whose names are
        joined with ``" · "``.

    Returns
    -------
    str
        Empty when there is no scope.
    """
    if not scope:
        return ""
    if isinstance(scope, str):
        return scope
    if isinstance(scope, dict):
        if "display_name" in scope or "id" in scope:
            return str(scope.get("display_name") or scope.get("id") or "")
        return " · ".join(n for n in (claude_scope_name(v) for v in scope.values()) if n)
    return str(scope)


def claude_dir(acc):
    """Return the account's Claude Code config dir (``CLAUDE_CONFIG_DIR``).

    Parameters
    ----------
    acc : dict
        Account config.

    Returns
    -------
    str
        Absolute path; ``~/.claude`` unless ``config_dir`` is set.
    """
    return expand(acc.get("config_dir") or "~/.claude")


def claude_is_default_dir(acc):
    """Tell whether the account uses Claude Code's default config dir.

    Parameters
    ----------
    acc : dict
        Account config.

    Returns
    -------
    bool
    """
    return os.path.realpath(claude_dir(acc)) == os.path.realpath(expand("~/.claude"))


def claude_credentials(acc):
    """Read the Claude Code OAuth credentials of an account.

    On macOS the keychain is tried first: the account's ``keychain_service``,
    else the item name Claude Code derives from the config dir. The
    ``.credentials.json`` file in the config dir is the fallback (Linux).

    Parameters
    ----------
    acc : dict
        Account config.

    Returns
    -------
    dict
        The ``claudeAiOauth`` object (``accessToken``, ``expiresAt``,
        ``subscriptionType``...), possibly empty.

    Raises
    ------
    ProviderError
        When no credentials are found (``setup=True``) or they are not valid
        JSON.
    """
    cdir = claude_dir(acc)
    raw = None
    if IS_MAC:
        if acc.get("keychain_service"):
            services = [acc["keychain_service"]]
        elif claude_is_default_dir(acc):
            services = ["Claude Code-credentials"]
        else:
            # Claude Code suffixes the service with a hash of CLAUDE_CONFIG_DIR.
            given = acc.get("config_dir") or cdir
            services = ["Claude Code-credentials-" + hashlib.sha256(s.encode()).hexdigest()[:8]
                        for s in dict.fromkeys([given, cdir])]
        for svc in services:
            raw = keychain_read(svc)
            if raw:
                break
    if not raw:
        try:
            with open(os.path.join(cdir, ".credentials.json"), encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            raw = None
    if not raw:
        raise ProviderError("Not signed in on this device", hint=f"mana login {acc['id']}", setup=True)
    try:
        return json.loads(raw).get("claudeAiOauth") or {}
    except ValueError:
        raise ProviderError("Unreadable Claude credentials") from None


def claude_keychain_services():
    """List the Claude Code credential items in the macOS keychain.

    Returns
    -------
    set of str
        Service names starting with ``"Claude Code-credentials"``; empty off
        macOS or on failure.
    """
    if not IS_MAC:
        return set()
    try:
        out = subprocess.run(["/usr/bin/security", "dump-keychain"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return set(re.findall(r'"svce"<blob>="(Claude Code-credentials[^"]*)"', out))


def claude_identity(acc):
    """Return the email address of the signed-in Claude account.

    Parameters
    ----------
    acc : dict
        Account config.

    Returns
    -------
    str or None
        Read from ``.claude.json`` (``~/.claude.json`` for the default config
        dir).
    """
    path = expand("~/.claude.json") if claude_is_default_dir(acc) else os.path.join(claude_dir(acc), ".claude.json")
    acct = (read_json(path, {}) or {}).get("oauthAccount") or {}
    return acct.get("emailAddress")


def fetch_claude(acc):
    """Fetch the plan limits of a Claude Pro / Max account.

    Calls the OAuth usage endpoint with Claude Code's token. Reads the
    ``limits`` list when present, else the older ``five_hour`` /
    ``seven_day*`` fields, plus the extra-usage spend when enabled.

    Parameters
    ----------
    acc : dict
        Account config of type ``"claude"``.

    Returns
    -------
    dict
        ``plan``, ``identity``, ``metrics`` (see `metric`) and ``notes``.

    Raises
    ------
    ProviderError
        When not signed in, the token is expired or rejected, or the request
        fails.
    """
    tok = claude_credentials(acc)
    access = tok.get("accessToken")
    if not access:
        raise ProviderError("No OAuth token (API-key login?)", setup=True)
    exp = parse_time(tok.get("expiresAt"))
    if exp and exp < utcnow():
        raise ProviderError("Login token expired (Claude Code refreshes it on next use)",
                            hint="Use Claude Code once, or Ping this account")
    try:
        d = http_json("https://api.anthropic.com/api/oauth/usage", headers={
            "Authorization": f"Bearer {access}", "anthropic-beta": CLAUDE_BETA,
            "User-Agent": "claude-code/2.1 tokenmana"})
    except ApiError as e:
        if e.status in (401, 403):
            raise ProviderError("Token rejected (401)", hint="Use Claude Code once to refresh its login") from None
        raise ProviderError(str(e)) from None

    metrics = []
    limits = d.get("limits")
    if isinstance(limits, list) and limits:
        for lim in limits:
            kind, group = lim.get("kind") or "", lim.get("group") or ""
            label = CLAUDE_LIMIT_LABELS.get(kind) or kind.replace("_", " ").title()
            key = kind
            scope = claude_scope_name(lim.get("scope"))
            if scope:
                label += f" · {scope}"
                key += "_" + re.sub(r"[^a-z0-9]+", "_", scope.lower()).strip("_")
            window = SESSION_WINDOW if group == "session" else WEEK_WINDOW if group == "weekly" else None
            pct = lim.get("percent")
            idle = group == "session" and not pct and not lim.get("resets_at")
            metrics.append(metric("session" if kind == "session" else key, label, pct,
                                  resets_at=lim.get("resets_at"), window=window, idle=idle))
    else:
        for key, mkey, label, window in (("five_hour", "session", "Session · 5h", SESSION_WINDOW),
                                         ("seven_day", "weekly_all", "Weekly", WEEK_WINDOW),
                                         ("seven_day_opus", "weekly_opus", "Weekly · Opus", WEEK_WINDOW),
                                         ("seven_day_sonnet", "weekly_sonnet", "Weekly · Sonnet", WEEK_WINDOW)):
            w = d.get(key)
            if isinstance(w, dict) and w.get("utilization") is not None:
                idle = mkey == "session" and not w["utilization"] and not w.get("resets_at")
                metrics.append(metric(mkey, label, w["utilization"], resets_at=w.get("resets_at"),
                                      window=window, idle=idle))

    notes = []
    spend = d.get("spend") or {}
    if spend.get("enabled"):
        def money(o):
            if isinstance(o, dict) and "amount_minor" in o:
                return o["amount_minor"] / 10 ** (o.get("exponent") or 0), o.get("currency")
            return (float(o), None) if isinstance(o, (int, float)) else (None, None)
        used, cur = money(spend.get("used"))
        lim, cur2 = money(spend.get("limit") or spend.get("cap"))
        cur = cur or cur2 or "USD"
        text = f"{fmt_money(used, cur)} / {fmt_money(lim, cur)}" if lim else fmt_money(used, cur)
        metrics.append(metric("extra_usage", "Extra usage", spend.get("percent"), text=text,
                              resets_at=first_of_next_month()))
    plan = (tok.get("subscriptionType") or "").title() or None
    return {"plan": plan, "identity": claude_identity(acc), "metrics": metrics, "notes": notes}
