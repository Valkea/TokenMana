"""Sign-in flows: the GitHub device flow for Copilot, and vendor CLI logins."""
from __future__ import annotations

import json
import re
import subprocess
import time

from .config import secrets_save
from .errors import ApiError
from .net import http_json
from .providers.copilot import COPILOT_CLIENT_ID, copilot_host, copilot_user
from .util import copy_to_clipboard, open_url


def emit(event, as_json, **kw):
    """Report a login event, as a JSON line or as text.

    Parameters
    ----------
    event : str
        ``"code"``, ``"url"``, ``"log"``, ``"info"``, ``"done"`` or ``"error"``.
    as_json : bool
        Print one JSON object per line (for the UIs) instead of text.
    **kw
        Event fields, e.g. ``message``, ``line``, ``user_code``,
        ``verification_uri``.
    """
    if as_json:
        print(json.dumps(dict(event=event, **kw)), flush=True)
    elif event == "code":
        print(f"\n  Open {kw['verification_uri']} and enter code:  {kw['user_code']}"
              + ("  (copied to clipboard)" if kw.get("copied") else "") + "\n", flush=True)
    elif event in ("done", "error", "log", "info"):
        print(kw.get("message") or kw.get("line") or "", flush=True)


def login_copilot(acc, as_json):
    """Sign a Copilot account in with the GitHub device flow.

    Shows (and copies) the user code, opens the verification page, polls for
    the token, checks that the account has Copilot, then stores the token in
    ``secrets.json``.

    Parameters
    ----------
    acc : dict
        Account config of type ``"copilot"``.
    as_json : bool
        Emit JSON events.

    Returns
    -------
    int
        Exit code: 0 when signed in.

    Raises
    ------
    ApiError, ProviderError
        When a device-flow request fails.
    """
    host = copilot_host(acc)
    d = http_json(f"https://{host}/login/device/code", data={"client_id": COPILOT_CLIENT_ID, "scope": "read:user"},
                  form=True)
    copied = copy_to_clipboard(d["user_code"])
    emit("code", as_json, user_code=d["user_code"], verification_uri=d["verification_uri"],
         expires_in=d.get("expires_in"), copied=copied)
    open_url(d["verification_uri"])
    interval = int(d.get("interval") or 5)
    deadline = time.time() + int(d.get("expires_in") or 900)
    while time.time() < deadline:
        time.sleep(interval)
        r = http_json(f"https://{host}/login/oauth/access_token", form=True, data={
            "client_id": COPILOT_CLIENT_ID, "device_code": d["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if r.get("access_token"):
            token = r["access_token"]
            try:
                who = copilot_user(acc, token)
            except ApiError as e:
                emit("error", as_json, message=f"Signed in, but Copilot API refused this account ({e.status}). "
                                               "Is a Copilot seat assigned to it?")
                return 1
            secrets_save(acc["id"], token)
            emit("done", as_json, message=f"Signed in as {who.get('login')} ({who.get('copilot_plan', '?')})",
                 identity=who.get("login"))
            return 0
        err = r.get("error")
        if err == "slow_down":
            interval += 5
        elif err != "authorization_pending":
            emit("error", as_json, message=r.get("error_description") or err or "login failed")
            return 1
    emit("error", as_json, message="Code expired, try again")
    return 1


def login_cli(acc, argv, env, as_json):
    """Run a vendor CLI login and stream its output.

    The CLI opens the browser itself; in JSON mode, URLs found in its output
    are also emitted as ``url`` events.

    Parameters
    ----------
    acc : dict
        Account config (unused).
    argv : list of str
        Login command line.
    env : dict of str to str
        Environment for the command.
    as_json : bool
        Emit JSON events.

    Returns
    -------
    int
        The command's exit code.
    """
    proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            stdin=subprocess.DEVNULL if as_json else None)
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            emit("log", as_json, line=line)
            m = re.search(r"https://\S+", line)
            if m and as_json:
                emit("url", as_json, url=m.group(0))
    rc = proc.wait()
    emit("done" if rc == 0 else "error", as_json, message="Signed in" if rc == 0 else f"login exited with {rc}")
    return rc
