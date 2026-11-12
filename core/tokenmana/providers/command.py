"""Any command printing TokenMana JSON (your own provider).

The account's ``command`` prints one JSON object on stdout (lines before it
are ignored)::

    {"plan": "Team budget", "identity": "me@example.com", "vendor": "bedrock",
     "metrics": [{"key": "budget", "label": "Budget", "percent": 42, "text": "$42 / $100",
                  "resets_at": "2026-12-01T00:00:00Z"}],
     "notes": ["Spent today: $3.20"]}

or, when it cannot get the data::

    {"error": "Credentials expired", "hint": "Run … to sign in"}

The README has the full format.
"""
from __future__ import annotations

import json
import shlex
import subprocess

from ..constants import HOME
from ..errors import ProviderError
from ..metrics import metric
from ..util import tool_env, which


def fetch_command(acc):
    """Run the account's command and read the TokenMana JSON it prints.

    Lines printed before the JSON object are ignored. The format is shown in
    the comment above and documented in the README.

    Parameters
    ----------
    acc : dict
        Account config of type ``"command"``, with ``command`` (split like a
        shell command line, but not run through a shell) and optionally
        ``login_hint``.

    Returns
    -------
    dict
        ``plan``, ``identity``, ``vendor``, ``metrics`` and ``notes``.

    Raises
    ------
    ProviderError
        When the command is missing, not found, times out (90 s) or fails, or
        prints ``{"error": ...}`` or no valid JSON object.
    """
    if not acc.get("command"):
        raise ProviderError("No command set for this account", setup=True)
    argv = shlex.split(acc["command"])
    exe = which(argv[0])
    if not exe:
        raise ProviderError(f"`{argv[0]}` not found on this device", hint=acc.get("login_hint"), setup=True)
    try:
        p = subprocess.run([exe] + argv[1:], capture_output=True, text=True, timeout=90,
                           env=tool_env(), cwd=HOME, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ProviderError(f"`{argv[0]}` timed out") from None
    out = p.stdout.strip()
    start = 0 if out.startswith("{") else out.find("\n{") + 1
    try:
        d = json.loads(out[start:]) if out.startswith("{") or start > 0 else None
    except ValueError:
        d = None
    if isinstance(d, dict) and d.get("error"):
        raise ProviderError(str(d["error"]), hint=acc.get("login_hint") or d.get("hint"), setup=bool(d.get("setup")))
    if p.returncode != 0:
        msg = (p.stderr.strip() or out or f"exit code {p.returncode}").splitlines()[0]
        raise ProviderError(f"`{argv[0]}` failed: {msg}", hint=acc.get("login_hint"))
    if not isinstance(d, dict):
        raise ProviderError(f"`{argv[0]}` did not print a JSON object")
    try:
        metrics = [metric(str(m.get("key") or f"metric{i}"), str(m.get("label") or m.get("key") or ""),
                          m.get("percent"), text=m.get("text"), resets_at=m.get("resets_at"),
                          window=m.get("window_seconds"), idle=m.get("idle", False))
                   for i, m in enumerate(d.get("metrics") or []) if isinstance(m, dict)]
    except (TypeError, ValueError) as e:
        raise ProviderError(f"`{argv[0]}` printed an invalid metric: {e}") from None
    return {"plan": d.get("plan"), "identity": d.get("identity"), "vendor": d.get("vendor"),
            "metrics": metrics, "notes": [str(n) for n in d.get("notes") or []]}
