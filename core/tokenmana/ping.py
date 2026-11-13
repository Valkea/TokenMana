"""Pings: a tiny prompt that starts an account's 5 h session timer, by hand or on schedule."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import subprocess
import tempfile
import time

from .config import enabled_here
from .constants import SCHEDULE_PATH, STATE_PATH
from .errors import ProviderError
from .providers.claude import claude_cli_env
from .providers.codex import codex_home
from .schedule import AUTO_PING_RETRY, auto_ping_conf, auto_ping_step, next_slot, schedule_state
from .status import collect
from .util import iso, read_json, tool_env, utcnow, which


PING_PROMPT = "Health check. Reply with the single word: pong"


def ping_command(acc):
    """Build the vendor CLI command that sends a ping prompt.

    Uses the smallest model and effort (overridable in the account's
    ``ping`` config) and no tools, MCP servers or session persistence.

    Parameters
    ----------
    acc : dict
        Account config of type ``"claude"`` or ``"codex"``.

    Returns
    -------
    argv : list of str
        Command line.
    env : dict of str to str
        Environment, without API keys so that the subscription is used.

    Raises
    ------
    ProviderError
        When the CLI is not found or the type has no session to start.
    """
    t = acc["type"]
    p = acc.get("ping") or {}
    prompt = p.get("prompt") or PING_PROMPT
    if t == "claude":
        exe = which("claude", acc.get("claude_bin"))
        if not exe:
            raise ProviderError("claude CLI not found")
        argv = [exe, "-p", "--model", p.get("model", "haiku")]
        if p.get("effort", "low"):
            argv += ["--effort", p.get("effort", "low")]
        argv += ["--tools", "", "--strict-mcp-config", "--no-session-persistence",
                 "--disable-slash-commands", "--setting-sources", "project"] + list(p.get("extra_args", [])) + [prompt]
        return argv, claude_cli_env(acc)
    if t == "codex":
        exe = which("codex", acc.get("codex_bin"))
        if not exe:
            raise ProviderError("codex CLI not found")
        argv = [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                "-m", p.get("model", "gpt-5.1-codex-mini"),
                "-c", f'model_reasoning_effort="{p.get("effort", "low")}"'] + list(p.get("extra_args", [])) + [prompt]
        return argv, tool_env(CODEX_HOME=codex_home(acc), OPENAI_API_KEY=None)
    raise ProviderError("This account type has no session timer to start")


def ping_account(acc):
    """Send a ping prompt to start the account's 5 h session window.

    Runs in a temporary directory with a 180 s timeout.

    Parameters
    ----------
    acc : dict
        Account config.

    Returns
    -------
    dict
        ``id``, ``ok`` and ``seconds``; ``output`` (last line, truncated)
        when the CLI ran; ``error`` on failure.
    """
    started = time.time()
    try:
        argv, env = ping_command(acc)
    except ProviderError as e:
        return {"id": acc["id"], "ok": False, "error": str(e), "seconds": 0}
    with tempfile.TemporaryDirectory(prefix="mana-ping-") as cwd:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=180, env=env, cwd=cwd,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return {"id": acc["id"], "ok": False, "error": "timed out", "seconds": round(time.time() - started, 1)}
    out = (p.stdout or "").strip().splitlines()
    err = (p.stderr or "").strip().splitlines()
    res = {"id": acc["id"], "ok": p.returncode == 0, "seconds": round(time.time() - started, 1),
           "output": out[-1][:200] if out else ""}
    if p.returncode != 0:
        res["error"] = ((err or out or ["exit code %d" % p.returncode])[-1])[:300]
    return res


def run_auto_pings(cfg):
    """Run the auto pings that are due and report the missed ones.

    Accounts pinged successfully are refreshed afterwards. A failed ping
    becomes a missed-ping question in times mode, or is retried after
    ``AUTO_PING_RETRY`` in interval mode.

    Parameters
    ----------
    cfg : dict
        Loaded config.

    Returns
    -------
    dict
        ``pinged``: `ping_account` results with the ``slot`` they were for;
        ``missed``: pending questions (``id``, ``label``, ``slot``, ``error``,
        ``next``).
    """
    accs = {}
    for a in cfg["accounts"]:
        conf = auto_ping_conf(a) if enabled_here(a) else None
        if conf:
            accs[a["id"]] = (a, conf)
    state = read_json(STATE_PATH, {}) or {}
    now = utcnow()
    due = {}
    if accs:
        with schedule_state() as s:
            for i, (a, conf) in accs.items():
                slot = auto_ping_step(conf, state.get(i) or {}, s.setdefault(i, {}), now)
                if slot:
                    due[i] = slot
    results = []
    if due:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(ping_account, [accs[i][0] for i in due]))
        done = utcnow()
        with schedule_state() as s:
            for r in results:
                e = s.setdefault(r["id"], {})
                r["slot"] = iso(due[r["id"]])
                if r["ok"]:
                    e.update(last_ping=iso(done), last_auto=iso(done), missed=None, error=None, retry_at=None)
                elif accs[r["id"]][1]["mode"] == "times":  # ask: try again now, or wait for the next time
                    e.update(missed=r["slot"], error=r.get("error"))
                else:
                    e.update(error=r.get("error"), retry_at=iso(done + dt.timedelta(seconds=AUTO_PING_RETRY)))
        ok = [r["id"] for r in results if r["ok"]]
        if ok:
            time.sleep(2)
            collect(cfg, ids=ok, force=True)
    sched = read_json(SCHEDULE_PATH, {}) or {}
    now = utcnow()
    missed = [{"id": i, "label": a.get("label") or i, "slot": sched[i]["missed"], "error": sched[i].get("error"),
               "next": iso(next_slot(conf["times"], now)) if conf["mode"] == "times" else None}
              for i, (a, conf) in accs.items() if (sched.get(i) or {}).get("missed")]
    return {"pinged": results, "missed": missed}
