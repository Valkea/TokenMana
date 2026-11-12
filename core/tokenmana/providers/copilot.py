"""GitHub Copilot (premium requests)."""
from __future__ import annotations

import os
import re
import subprocess

from ..config import secrets_load
from ..errors import ApiError, ProviderError
from ..metrics import metric
from ..net import http_json
from ..util import expand, fmt_num, read_json, tool_env


COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"  # GitHub Copilot editor-plugin app (device flow)


def normalize_host(value):
    """Reduce a GitHub host URL to its bare lower-case host name.

    Parameters
    ----------
    value : str or None
        E.g. ``"https://mycorp.ghe.com/"``.

    Returns
    -------
    str
        E.g. ``"mycorp.ghe.com"``; empty when `value` is empty.
    """
    return re.sub(r"^[a-z]+://", "", (value or "").strip(), flags=re.I).strip("/").split("/")[0].lower()


def copilot_host(acc):
    """Return the GitHub host of a Copilot account.

    Parameters
    ----------
    acc : dict
        Account config.

    Returns
    -------
    str
        ``"github.com"`` unless ``host`` is set (GitHub Enterprise Cloud).
    """
    return normalize_host(acc.get("host")) or "github.com"


def copilot_token(acc):
    """Find the GitHub OAuth token of a Copilot account.

    Sources, in order: the ``token_env`` environment variable, the output of
    ``token_cmd``, ``secrets.json`` (written by ``mana login``), then the
    editor-plugin files in ``~/.config/github-copilot`` for ``github_user``.

    Parameters
    ----------
    acc : dict
        Account config of type ``"copilot"``.

    Returns
    -------
    str

    Raises
    ------
    ProviderError
        When ``token_cmd`` fails or no token is found (``setup=True``).
    """
    if acc.get("token_env") and os.environ.get(acc["token_env"]):
        return os.environ[acc["token_env"]].strip()
    if acc.get("token_cmd"):
        try:
            p = subprocess.run(acc["token_cmd"], shell=True, capture_output=True, text=True,
                               timeout=20, env=tool_env(), stdin=subprocess.DEVNULL)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip().splitlines()[0]
        except subprocess.TimeoutExpired:
            pass
        raise ProviderError("token_cmd failed")
    tok = secrets_load().get(acc["id"])
    if tok:
        return tok
    # Tokens written by copilot.vim / JetBrains / Copilot CLI plugins.
    user = acc.get("github_user")
    if user:
        for fname in ("apps.json", "hosts.json"):
            data = read_json(expand(f"~/.config/github-copilot/{fname}"), {}) or {}
            for key, val in data.items():
                if key.startswith(copilot_host(acc)) and isinstance(val, dict) and val.get("user") == user:
                    if val.get("oauth_token"):
                        return val["oauth_token"]
    raise ProviderError("Not signed in on this device", hint=f"mana login {acc['id']}", setup=True)


def copilot_api(acc):
    """Return the REST API base URL of the account's GitHub host.

    Parameters
    ----------
    acc : dict
        Account config.

    Returns
    -------
    str
        ``https://api.github.com`` or ``https://api.<host>``.
    """
    host = copilot_host(acc)
    return "https://api.github.com" if host == "github.com" else f"https://api.{host}"


def copilot_user(acc, token):
    """Call the Copilot ``copilot_internal/user`` endpoint.

    Parameters
    ----------
    acc : dict
        Account config.
    token : str
        GitHub OAuth token.

    Returns
    -------
    dict
        Plan, login, quota snapshots and reset date.

    Raises
    ------
    ApiError
        When GitHub refuses the request.
    ProviderError
        On a network error.
    """
    return http_json(copilot_api(acc) + "/copilot_internal/user", headers={
        "Authorization": f"token {token}", "Editor-Version": "vscode/1.105.0",
        "Editor-Plugin-Version": "copilot-chat/0.32.0", "User-Agent": "GitHubCopilotChat/0.32.0",
        "X-GitHub-Api-Version": "2025-04-01"})


def fetch_copilot(acc):
    """Fetch the monthly quotas of a GitHub Copilot account.

    Parameters
    ----------
    acc : dict
        Account config of type ``"copilot"``.

    Returns
    -------
    dict
        ``plan``, ``identity`` (GitHub login), ``metrics`` (premium requests,
        plus chat / completions when limited) and ``notes`` (overage).

    Raises
    ------
    ProviderError
        When no token is found or GitHub refuses it.
    """
    try:
        d = copilot_user(acc, copilot_token(acc))
    except ApiError as e:
        if e.status in (401, 403, 404):
            raise ProviderError(f"GitHub refused the token ({e.status})",
                                hint=f"mana login {acc['id']}") from None
        raise ProviderError(str(e)) from None
    reset = d.get("quota_reset_date_utc") or d.get("quota_reset_date") or d.get("limited_user_reset_date")
    metrics, notes = [], []
    snaps = d.get("quota_snapshots") or {}
    for key, label in (("premium_interactions", "Premium requests"), ("chat", "Chat"),
                       ("completions", "Completions")):
        s = snaps.get(key)
        if not isinstance(s, dict):
            continue
        if s.get("unlimited"):
            if key == "premium_interactions":
                metrics.append(metric(key, label, None, text="unlimited", resets_at=reset))
            continue
        ent = s.get("entitlement")
        rem = s.get("remaining", s.get("quota_remaining"))
        pct_left = s.get("percent_remaining")
        used = (ent - rem) if ent is not None and rem is not None else None
        if key != "premium_interactions" and "premium_interactions" in snaps and not used:
            continue
        pct = 100 - pct_left if pct_left is not None else (used / ent * 100 if used is not None and ent else None)
        text = f"{fmt_num(used)} / {fmt_num(ent)}" if used is not None and ent else None
        metrics.append(metric(key, label, pct, text=text, resets_at=reset))
        if s.get("overage_count"):
            notes.append(f"{label}: {fmt_num(s['overage_count'])} over quota"
                         + (" (billed)" if s.get("overage_permitted") else ""))
    if not snaps and d.get("monthly_quotas"):  # legacy free-plan shape
        for key in ("chat", "completions"):
            ent = (d.get("monthly_quotas") or {}).get(key)
            rem = (d.get("limited_user_quotas") or {}).get(key)
            if ent:
                used = ent - (rem or 0)
                metrics.append(metric(key, key.title(), used / ent * 100,
                                      text=f"{fmt_num(used)} / {fmt_num(ent)}", resets_at=reset))
    plan = d.get("copilot_plan") or d.get("access_type_sku")
    return {"plan": (plan or "").replace("_", " ").title() or None, "identity": d.get("login"),
            "metrics": metrics, "notes": notes}
