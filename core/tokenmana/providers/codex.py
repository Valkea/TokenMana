"""OpenAI Codex (ChatGPT Plus / Pro / Business via the Codex CLI login)."""
from __future__ import annotations

import datetime as dt
import glob
import json
import os

from ..constants import SESSION_WINDOW, WEEK_WINDOW
from ..errors import ApiError, ProviderError
from ..metrics import metric, window_label
from ..net import http_json, jwt_claims
from ..util import expand, fmt_num, parse_time, read_json, utcnow


def codex_home(acc):
    """Return the account's Codex CLI home (``CODEX_HOME``).

    Parameters
    ----------
    acc : dict
        Account config.

    Returns
    -------
    str
        Absolute path; ``~/.codex`` unless ``codex_home`` is set.
    """
    return expand(acc.get("codex_home") or "~/.codex")


def codex_rollout_limits(home):
    """Read the last rate-limit snapshot from the Codex CLI session logs.

    Fallback for when the usage API cannot be reached: scans the end of the
    five most recent ``sessions/**/rollout-*.jsonl`` files.

    Parameters
    ----------
    home : str
        Codex home directory.

    Returns
    -------
    metrics : list of dict or None
        Metrics built with `metric`, or None when no snapshot is found.
    at : datetime.datetime or None
        When the snapshot was written.
    """
    files = glob.glob(os.path.join(home, "sessions", "**", "rollout-*.jsonl"), recursive=True)
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for path in files[:5]:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-400:]
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            payload = ev.get("payload") or ev.get("msg") or ev
            rl = payload.get("rate_limits") if isinstance(payload, dict) else None
            if not isinstance(rl, dict):
                continue
            at = parse_time(ev.get("timestamp")) or dt.datetime.fromtimestamp(os.path.getmtime(path), dt.timezone.utc)
            metrics = []
            for key in ("primary", "secondary"):
                w = rl.get(key)
                if not isinstance(w, dict) or w.get("used_percent") is None:
                    continue
                secs = (w.get("window_minutes") or 0) * 60 or None
                reset = parse_time(w.get("resets_at"))
                if not reset and w.get("resets_in_seconds") is not None:
                    reset = at + dt.timedelta(seconds=w["resets_in_seconds"])
                label = window_label(secs, key.title())
                metrics.append(metric("session" if secs == SESSION_WINDOW else ("weekly" if secs == WEEK_WINDOW else key),
                                      label, w["used_percent"], resets_at=reset, window=secs))
            if metrics:
                return metrics, at
    return None, None


def fetch_codex(acc):
    """Fetch the plan limits of a Codex (ChatGPT plan) account.

    Uses the Codex CLI login (``auth.json``). When the usage API fails, falls
    back to `codex_rollout_limits` and reports the error as partial.

    Parameters
    ----------
    acc : dict
        Account config of type ``"codex"``.

    Returns
    -------
    dict
        ``plan``, ``identity``, ``metrics`` and ``notes``, plus
        ``_partial_error`` when the data comes from the session logs.

    Raises
    ------
    ProviderError
        When not signed in, signed in with an API key, or the API fails and
        no session log has limits.
    """
    home = codex_home(acc)
    auth = read_json(os.path.join(home, "auth.json"))
    if not auth:
        raise ProviderError("Not signed in on this device", hint=f"mana login {acc['id']}", setup=True)
    tokens = auth.get("tokens") or {}
    access = tokens.get("access_token")
    claims = jwt_claims(tokens.get("id_token") or "")
    oai = claims.get("https://api.openai.com/auth") or {}
    account_id = tokens.get("account_id") or oai.get("chatgpt_account_id")
    plan = oai.get("chatgpt_plan_type")
    identity = claims.get("email")
    if not access:
        raise ProviderError("Signed in with an API key — no plan limits", setup=True)

    base = (acc.get("chatgpt_base_url") or "https://chatgpt.com/backend-api").rstrip("/")
    headers = {"Authorization": f"Bearer {access}", "User-Agent": "codex_cli_rs/0.50 tokenmana",
               "originator": "codex_cli_rs"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    try:
        d = http_json(base + "/wham/usage", headers=headers)
    except (ApiError, ProviderError) as e:
        metrics, at = codex_rollout_limits(home)
        expired = isinstance(e, ApiError) and e.status in (401, 403)
        msg = "Login expired (401)" if expired else str(e)
        if metrics:
            return {"plan": (plan or "").title() or None, "identity": identity, "metrics": metrics,
                    "notes": [f"From Codex session log of {at.astimezone().strftime('%d %b %H:%M')}"],
                    "_partial_error": msg}
        raise ProviderError(msg, hint=f"Run codex once to refresh, or mana login {acc['id']}" if expired else None) from None

    metrics = []
    rl = d.get("rate_limit") or {}
    for key in ("primary_window", "secondary_window"):
        w = rl.get(key)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        secs = w.get("limit_window_seconds")
        reset = parse_time(w.get("reset_at"))
        if not reset and w.get("reset_after_seconds") is not None:
            reset = utcnow() + dt.timedelta(seconds=w["reset_after_seconds"])
        mkey = "session" if secs == SESSION_WINDOW else ("weekly" if secs and abs(secs - WEEK_WINDOW) < 3600 else key)
        metrics.append(metric(mkey, window_label(secs, key.replace("_", " ").title()), w["used_percent"],
                              resets_at=reset, window=secs))
    notes = []
    if rl.get("limit_reached"):
        notes.append("Rate limit reached")
    credits = d.get("credits") or {}
    if credits.get("unlimited"):
        notes.append("Credits: unlimited")
    elif credits.get("has_credits") or (credits.get("balance") not in (None, "", "0", 0)):
        try:
            notes.append(f"Credits balance: {fmt_num(float(credits.get('balance')))}")
        except (TypeError, ValueError):
            pass
    plan = d.get("plan_type") or plan
    return {"plan": (plan or "").title() or None, "identity": d.get("email") or identity,
            "metrics": metrics, "notes": notes}
