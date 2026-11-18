"""The installed version and the check for a newer one.

By default the newest ``vX.Y.Z`` tag of the public repo is the latest version.
A derived distribution (e.g. an internal repo that installs this one with its
own add-ons) points the check at its own repo with a ``distribution.json``
next to this module, which ``TOKENMANA_DISTRIBUTION=/abs/path.json ./install.sh``
installs::

    {
      "name": "TokenMana · internal",
      "update_url": "https://ghe.example.com/api/v3/repos/org/repo/contents/upstream.version?ref=main",
      "update_page": "https://ghe.example.com/org/repo#install",
      "update_token_cmd": "gh auth token --hostname ghe.example.com"
    }

All keys are optional. ``update_url`` returns text, HTML, an Atom feed or JSON:
the highest ``vX.Y.Z`` in it is the latest version (GitHub API ``contents``
URLs return the raw file). ``update_token_cmd`` prints a token sent as
``Authorization: token …``. ``version`` overrides the version shown and
compared, for a distribution with its own numbering.
"""
from __future__ import annotations

import os
import re
import subprocess
import urllib.parse
import urllib.request

from .constants import CACHE_DIR, VERSION
from .util import iso, parse_time, read_json, tool_env, utcnow, write_json

DISTRIBUTION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "distribution.json")
UPDATE_PATH = os.path.join(CACHE_DIR, "update.json")
UPSTREAM = {
    "name": "TokenMana",
    "update_url": "https://github.com/Valkea/TokenMana/tags.atom",  # not the API: its anonymous rate limit
    "update_page": "https://github.com/Valkea/TokenMana#install",
}
CHECK_EVERY = 12 * 3600
RETRY_AFTER = 3600  # after a failed check


def distribution():
    """Return the update source and the installed version.

    Returns
    -------
    dict
        ``name``, ``version``, ``update_url``, ``update_page`` and, if set,
        ``update_token_cmd``: `UPSTREAM` overridden by ``distribution.json``.
    """
    d = read_json(DISTRIBUTION_PATH, None)
    d = {k: v for k, v in d.items() if v} if isinstance(d, dict) else {}
    return {**UPSTREAM, "version": VERSION, **d}


def version_key(v):
    """Sort key of a version string (``"v0.2.10"`` → ``(0, 2, 10)``).

    Parameters
    ----------
    v : str or None

    Returns
    -------
    tuple of int
        Empty when `v` holds no number.
    """
    m = re.match(r"v?(\d+(?:\.\d+)*)", (v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def latest_in(text):
    """Return the highest ``vX.Y.Z`` found in a document, without the ``v``.

    Parameters
    ----------
    text : str

    Returns
    -------
    str or None
    """
    found = re.findall(r"\bv(\d+(?:\.\d+)+)\b", text)
    return max(found, key=version_key) if found else None


class _SameHostAuth(urllib.request.HTTPRedirectHandler):
    """Follows redirects but sends the token to the original host only."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            new.remove_header("Authorization")
        return new


def fetch_latest(dist):
    """Download the update source and find the latest version in it.

    Parameters
    ----------
    dist : dict
        From `distribution`.

    Returns
    -------
    str
        The latest version, without the ``v``.

    Raises
    ------
    Exception
        On a network or HTTP error, a failed token command, or when the page
        holds no version (e.g. a sign-in page).
    """
    headers = {"User-Agent": f"tokenmana/{VERSION}", "Accept": "application/vnd.github.raw, */*"}
    if dist.get("update_token_cmd"):
        p = subprocess.run(dist["update_token_cmd"], shell=True, capture_output=True, text=True,
                           timeout=20, env=tool_env(), stdin=subprocess.DEVNULL)
        if p.returncode != 0 or not p.stdout.strip():
            raise RuntimeError("update_token_cmd failed")
        headers["Authorization"] = "token " + p.stdout.strip().splitlines()[0]
    req = urllib.request.Request(dist["update_url"], headers=headers)
    with urllib.request.build_opener(_SameHostAuth).open(req, timeout=10) as r:
        text = r.read(2_000_000).decode("utf-8", "replace")
    latest = latest_in(text)
    if not latest:
        raise RuntimeError(f"no version found at {dist['update_url']}")
    return latest


def app_info(cfg, check=True):
    """Return the installed version and the latest one, checking when due.

    Checks at most every 12 h (1 h after a failure); the result is cached in
    ``update.json``. Never raises.

    Parameters
    ----------
    cfg : dict
        Loaded config; ``"check_updates": false`` turns the check off.
    check : bool, default True
        False: never hit the network, use the cached result.

    Returns
    -------
    dict
        ``name``, ``version``, ``latest`` (None when unknown or checks are
        off), ``update_available``, ``update_page``, ``checked_at`` and
        ``error`` (of the last check).
    """
    dist = distribution()
    enabled = cfg.get("check_updates") is not False
    state = read_json(UPDATE_PATH, {}) or {}
    if state.get("url") != dist["update_url"]:
        state = {}
    last = parse_time(state.get("checked_at"))
    wait = RETRY_AFTER if state.get("error") else CHECK_EVERY
    if check and enabled and (not last or (utcnow() - last).total_seconds() >= wait):
        state.update(url=dist["update_url"], checked_at=iso(utcnow()))
        try:
            state.update(latest=fetch_latest(dist), error=None)
        except Exception as e:  # keeps the last known latest
            state["error"] = f"{type(e).__name__}: {e}"
        try:
            write_json(UPDATE_PATH, state)
        except OSError:
            pass
    version = str(dist["version"]).lstrip("v")
    latest = state.get("latest") if enabled else None
    return {"name": dist["name"], "version": version, "latest": latest,
            "update_available": bool(latest) and version_key(latest) > version_key(version),
            "update_page": dist["update_page"], "checked_at": state.get("checked_at"), "error": state.get("error")}
