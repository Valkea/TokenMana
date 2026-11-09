#!/usr/bin/env python3
"""ctrack — CreditsTracker core.

Fetches usage / limits / spend for several coding-assistant accounts and prints
them in one normalized JSON shape consumed by the macOS menu-bar app and the
GNOME Shell extension. Stdlib only, Python >= 3.9, macOS + Linux.

  ctrack status [--json] [--force] [--cached] [ACCOUNT_ID ...]
  ctrack ping   [--json] (--all | --idle | ACCOUNT_ID ...)
  ctrack auto-ping [--json] [--dismiss ACCOUNT_ID]   run the scheduled pings that are due
  ctrack login  [--json] ACCOUNT_ID
  ctrack add    TYPE [--label L] [--dir D] [--host H] [--command C]   (claude|codex|copilot|command)
  ctrack edit   ACCOUNT_ID [--label L] [--group G] [--host H] [--auto-ping off|5h|HH:MM,...]
  ctrack remove ACCOUNT_ID
  ctrack init   [--force]          write a starter config (auto-detected)
  ctrack paths                     print config / cache locations

Secrets (OAuth tokens) are read from where each tool already stores them and are
never printed or written to the cache.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import contextlib
import datetime as dt
import fcntl
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser

VERSION = "0.1.0"
IS_MAC = sys.platform == "darwin"
HOME = os.path.expanduser("~")

CONFIG_DIR = os.environ.get("CTRACK_CONFIG_DIR") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "creditstracker")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
SECRETS_PATH = os.path.join(CONFIG_DIR, "secrets.json")
CACHE_DIR = os.environ.get("CTRACK_CACHE_DIR") or os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.join(HOME, ".cache"), "creditstracker")
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

COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"  # GitHub Copilot editor-plugin app (device flow)
CLAUDE_BETA = "oauth-2025-04-20"
PING_PROMPT = "Health check. Reply with the single word: pong"
AUTO_PING_GAP = SESSION_WINDOW + 60   # every-5h mode: 5 h + 1 min after the last ping, so the window has ended
AUTO_PING_GRACE = 5 * 60              # a scheduled time not reached within this is "missed" (device asleep / off)
AUTO_PING_RETRY = 15 * 60             # every-5h mode: wait this long after a failed auto ping


# ───────────────────────────── helpers ──────────────────────────────────────

class ProviderError(Exception):
    def __init__(self, message, hint=None, setup=False):
        super().__init__(message)
        self.hint = hint
        self.setup = setup


class ApiError(Exception):
    def __init__(self, status, detail):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


def expand(p):
    return os.path.abspath(os.path.expandvars(os.path.expanduser(p))) if p else p


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def parse_time(v):
    """Accept ISO strings (with Z / offsets / any fraction length), dates, epoch s/ms."""
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(v / 1000 if v > 1e12 else v, dt.timezone.utc)
    s = str(v).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return parse_time(float(s))
    s = s.replace("Z", "+00:00").replace(" ", "T", 1)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        s += "T00:00:00+00:00"
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", s)
    if m:
        frac = (m.group(2) or "")[:7]
        if frac:
            frac = frac.ljust(7, "0")
        s = m.group(1) + frac + m.group(3)
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def first_of_next_month(t=None):
    t = t or utcnow()
    y, m = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
    return dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)


def fmt_money(v, currency="USD"):
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get((currency or "USD").upper(), "")
    if v is None:
        return "—"
    s = f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.2f}"
    return f"{sym}{s}" if sym else f"{s} {currency}"


def fmt_num(v):
    if v is None:
        return "—"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.1f}"


def search_path():
    dirs = os.environ.get("PATH", "").split(os.pathsep)
    for p in EXTRA_PATH:
        for d in (sorted(glob.glob(expand(p))) if "*" in p else [expand(p)]):
            if d not in dirs and os.path.isdir(d):
                dirs.append(d)
    return dirs


def which(name, override=None):
    if override:
        p = expand(override)
        return p if os.path.isfile(p) and os.access(p, os.X_OK) else None
    if os.sep in name:
        p = expand(name)
        return p if os.access(p, os.X_OK) else None
    return shutil.which(name, path=os.pathsep.join(search_path()))


def tool_env(**overrides):
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(search_path())
    for k, v in overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


def http_json(url, headers=None, data=None, timeout=20, form=False):
    body = None
    hdrs = {"Accept": "application/json", "User-Agent": f"creditstracker/{VERSION}"}
    hdrs.update(headers or {})
    if data is not None:
        if form:
            from urllib.parse import urlencode
            body = urlencode(data).encode()
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            body = json.dumps(data).encode()
            hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace").strip()
        raise ApiError(e.code, detail) from None
    except urllib.error.URLError as e:
        raise ProviderError(f"Network error: {e.reason}") from None
    except (TimeoutError, OSError) as e:
        raise ProviderError(f"Network error: {e}") from None
    return json.loads(raw.decode("utf-8") or "null")


def jwt_claims(token):
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def keychain_read(service):
    if not IS_MAC:
        return None
    try:
        p = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None


def metric(key, label, percent=None, text=None, resets_at=None, window=None, idle=False):
    if percent is not None:
        percent = max(0.0, round(float(percent), 1))
    if text is None:
        text = "idle" if idle else (f"{percent:.0f}%" if percent is not None else "—")
    return {"key": key, "label": label, "percent": percent, "text": text,
            "resets_at": iso(parse_time(resets_at)), "window_seconds": window, "idle": bool(idle)}


def window_label(seconds, fallback):
    if not seconds:
        return fallback
    if abs(seconds - SESSION_WINDOW) < 60:
        return "Session · 5h"
    if abs(seconds - WEEK_WINDOW) < 3600:
        return "Weekly"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}-day window"
    return f"{round(seconds / 3600)}h window"


def secrets_load():
    return read_json(SECRETS_PATH, {}) or {}


def secrets_save(account_id, token):
    data = secrets_load()
    data[account_id] = token
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    write_json(SECRETS_PATH, data, mode=0o600)


def copy_to_clipboard(text):
    for argv in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]):
        exe = which(argv[0])
        if exe:
            try:
                subprocess.run([exe] + argv[1:], input=text, text=True, timeout=5)
                return True
            except (OSError, subprocess.TimeoutExpired):
                pass
    return False


def open_url(url):
    try:
        if IS_MAC:
            subprocess.Popen(["/usr/bin/open", url])
        elif which("xdg-open"):
            subprocess.Popen([which("xdg-open"), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            webbrowser.open(url)
    except OSError:
        pass


# ───────────────────────────── providers ────────────────────────────────────

# -- Anthropic Claude (Pro / Max subscription via Claude Code OAuth) ---------

CLAUDE_LIMIT_LABELS = {
    "session": "Session · 5h",
    "weekly_all": "Weekly",
    "weekly_opus": "Weekly · Opus",
    "weekly_sonnet": "Weekly · Sonnet",
    "weekly_scoped": "Weekly",
}


def claude_scope_name(scope):
    """Readable name of a limit's scope: a string, or {"model": {...}, "surface": ...} objects."""
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
    return expand(acc.get("config_dir") or "~/.claude")


def claude_is_default_dir(acc):
    return os.path.realpath(claude_dir(acc)) == os.path.realpath(expand("~/.claude"))


def claude_credentials(acc):
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
        raise ProviderError("Not signed in on this device", hint=f"ctrack login {acc['id']}", setup=True)
    try:
        return json.loads(raw).get("claudeAiOauth") or {}
    except ValueError:
        raise ProviderError("Unreadable Claude credentials") from None


def claude_keychain_services():
    if not IS_MAC:
        return set()
    try:
        out = subprocess.run(["/usr/bin/security", "dump-keychain"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return set(re.findall(r'"svce"<blob>="(Claude Code-credentials[^"]*)"', out))


def claude_identity(acc):
    path = expand("~/.claude.json") if claude_is_default_dir(acc) else os.path.join(claude_dir(acc), ".claude.json")
    acct = (read_json(path, {}) or {}).get("oauthAccount") or {}
    return acct.get("emailAddress")


def fetch_claude(acc):
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
            "User-Agent": "claude-code/2.1 creditstracker"})
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


# -- Any command printing CreditsTracker JSON (your own provider) ------------
#
# The account's "command" prints one JSON object on stdout (lines before it are ignored):
#   {"plan": "Team budget", "identity": "me@example.com", "vendor": "bedrock",
#    "metrics": [{"key": "budget", "label": "Budget", "percent": 42, "text": "$42 / $100",
#                 "resets_at": "2026-12-01T00:00:00Z"}],
#    "notes": ["Spent today: $3.20"]}
# or, when it cannot get the data:  {"error": "Credentials expired", "hint": "Run … to sign in"}
# The README has the full format.

def fetch_command(acc):
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


# -- OpenAI Codex (ChatGPT Plus / Pro / Business via Codex CLI login) -------

def codex_home(acc):
    return expand(acc.get("codex_home") or "~/.codex")


def codex_rollout_limits(home):
    """Fallback: last rate_limits snapshot written by the Codex CLI in its session logs."""
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
    home = codex_home(acc)
    auth = read_json(os.path.join(home, "auth.json"))
    if not auth:
        raise ProviderError("Not signed in on this device", hint=f"ctrack login {acc['id']}", setup=True)
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
    headers = {"Authorization": f"Bearer {access}", "User-Agent": "codex_cli_rs/0.50 creditstracker",
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
        raise ProviderError(msg, hint=f"Run codex once to refresh, or ctrack login {acc['id']}" if expired else None) from None

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


# -- GitHub Copilot (premium requests) ---------------------------------------

def normalize_host(value):
    """'https://mycorp.ghe.com/' → 'mycorp.ghe.com'"""
    return re.sub(r"^[a-z]+://", "", (value or "").strip(), flags=re.I).strip("/").split("/")[0].lower()


def copilot_host(acc):
    return normalize_host(acc.get("host")) or "github.com"


def copilot_token(acc):
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
    raise ProviderError("Not signed in on this device", hint=f"ctrack login {acc['id']}", setup=True)


def copilot_api(acc):
    host = copilot_host(acc)
    return "https://api.github.com" if host == "github.com" else f"https://api.{host}"


def copilot_user(acc, token):
    return http_json(copilot_api(acc) + "/copilot_internal/user", headers={
        "Authorization": f"token {token}", "Editor-Version": "vscode/1.105.0",
        "Editor-Plugin-Version": "copilot-chat/0.32.0", "User-Agent": "GitHubCopilotChat/0.32.0",
        "X-GitHub-Api-Version": "2025-04-01"})


def fetch_copilot(acc):
    try:
        d = copilot_user(acc, copilot_token(acc))
    except ApiError as e:
        if e.status in (401, 403, 404):
            raise ProviderError(f"GitHub refused the token ({e.status})",
                                hint=f"ctrack login {acc['id']}") from None
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


PROVIDERS = {
    "claude": {"fetch": fetch_claude, "vendor": "anthropic", "ping": True,
               "dashboard": "https://claude.ai/settings/usage"},
    "command": {"fetch": fetch_command, "vendor": "command", "ping": False,
                "dashboard": None, "refresh_minutes": 15},
    "codex": {"fetch": fetch_codex, "vendor": "openai", "ping": True,
              "dashboard": "https://chatgpt.com/codex/settings/usage"},
    "copilot": {"fetch": fetch_copilot, "vendor": "github", "ping": False,
                "dashboard": "https://github.com/settings/copilot"},
}


# ───────────────────────────── ping ─────────────────────────────────────────

def ping_command(acc):
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
        env = tool_env(ANTHROPIC_API_KEY=None, ANTHROPIC_AUTH_TOKEN=None, CLAUDE_CODE_USE_BEDROCK=None,
                       CLAUDE_CODE_USE_VERTEX=None,
                       CLAUDE_CONFIG_DIR=None if claude_is_default_dir(acc) else claude_dir(acc))
        return argv, env
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
    started = time.time()
    try:
        argv, env = ping_command(acc)
    except ProviderError as e:
        return {"id": acc["id"], "ok": False, "error": str(e), "seconds": 0}
    with tempfile.TemporaryDirectory(prefix="ctrack-ping-") as cwd:
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


# ───────────────────────────── auto ping ────────────────────────────────────
#
# Per account in config.json (`ctrack edit ID --auto-ping 5h|08:00,13:01|off`):
#   "auto_ping": {"mode": "interval"}                            5 h 1 min after the last ping
#   "auto_ping": {"mode": "times", "times": ["08:00", "13:01"]}  at these local times
# The UIs run `ctrack auto-ping` every minute (cron works too). A scheduled time the device was
# asleep or off for is not caught up on its own: it becomes a question, "ping now, or wait for the
# next time?". Run state (last ping, handled times, pending question) is kept in autoping.json.

def parse_ping_times(value):
    """'8:00, 13:01' or ['08:00', '13h01'] → ['08:00', '13:01'] (sorted, no duplicates)."""
    items = value if isinstance(value, list) else re.split(r"[\s,;]+", str(value or ""))
    out = set()
    for raw in items:
        s = str(raw).strip()
        if not s:
            continue
        m = re.fullmatch(r"(\d{1,2})(?:[:hH.](\d{2})?)?", s)
        h, mi = (int(m.group(1)), int(m.group(2) or 0)) if m else (99, 0)
        if h > 23 or mi > 59:
            raise ValueError(f"invalid time {s!r} (use HH:MM, 24 h)")
        out.add(f"{h:02d}:{mi:02d}")
    return sorted(out)


def parse_auto_ping(value):
    """CLI value → config value: 'off' → None, '5h' → interval, '08:00,13:01' → times."""
    v = (value or "").strip().lower()
    if v in ("", "off", "none", "no", "false"):
        return None
    if v in ("5h", "interval", "on"):
        return {"mode": "interval"}
    times = parse_ping_times(v)
    return {"mode": "times", "times": times} if times else None


def auto_ping_conf(acc):
    """{"mode": "interval"} | {"mode": "times", "times": [...]} | None (off, or the account has no ping)."""
    c = acc.get("auto_ping")
    if not isinstance(c, dict) or not PROVIDERS[acc["type"]]["ping"] or acc.get("ping") is False:
        return None
    if c.get("mode") == "interval":
        return {"mode": "interval"}
    if c.get("mode") == "times":
        try:
            times = parse_ping_times(c.get("times") or [])
        except ValueError:
            return None
        return {"mode": "times", "times": times} if times else None
    return None


def auto_ping_sig(conf):
    return json.dumps(conf, sort_keys=True)


def local_slots(times, start, end):
    """Scheduled times (local clock) in (start, end], oldest first."""
    out = []
    day, last = start.astimezone().date(), end.astimezone().date()
    while day <= last:
        for t in times:
            h, m = map(int, t.split(":"))
            slot = dt.datetime(day.year, day.month, day.day, h, m).astimezone()  # naive = local time
            if start < slot <= end:
                out.append(slot)
        day += dt.timedelta(days=1)
    return out


def next_slot(times, after):
    slots = local_slots(times, after, after + dt.timedelta(days=2))
    return slots[0] if slots else None


def session_busy_until(entry):
    """End of the account's running 5 h session + 1 min, from its cached fetch; None if none is known."""
    if (entry or {}).get("status") == "setup":
        return None
    for m in ((entry or {}).get("data") or {}).get("metrics") or []:
        if m.get("key") == "session" and not m.get("idle"):
            reset = parse_time(m.get("resets_at"))
            return reset + dt.timedelta(seconds=60) if reset else None
    return None


@contextlib.contextmanager
def schedule_state():
    """Locked read-modify-write of autoping.json (the UI timer, manual pings and cron can overlap)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(SCHEDULE_PATH + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = read_json(SCHEDULE_PATH, {}) or {}
        yield data
        write_json(SCHEDULE_PATH, data)


def auto_ping_step(conf, entry, e, now):
    """Is a ping due for this account? Returns the time it is for, or None.
    entry: the account's cached fetch (state.json); e: its schedule record, updated in place."""
    sig = auto_ping_sig(conf)
    if e.get("sig") != sig:  # new or changed schedule: start from now, ask nothing about the past
        e.update(sig=sig, handled_until=iso(now), missed=None, error=None, retry_at=None)
    if entry.get("status") == "setup":  # not signed in on this device
        return None
    busy = session_busy_until(entry)
    running = busy is not None and now < busy
    if conf["mode"] == "interval":
        last, retry = parse_time(e.get("last_ping")), parse_time(e.get("retry_at"))
        if running or (retry and now < retry) or (last and now < last + dt.timedelta(seconds=AUTO_PING_GAP)):
            return None
        return now
    handled = parse_time(e.get("handled_until")) or now
    slots = local_slots(conf["times"], max(handled, now - dt.timedelta(days=7)), now)
    e["handled_until"] = iso(now)
    if slots and (now - slots[-1]).total_seconds() <= AUTO_PING_GRACE:
        e.update(missed=None, error=None)
        return slots[-1]
    if slots:  # slept through it (several missed times make one question)
        e.update(missed=iso(slots[-1]), error=None)
    if running:  # a session is already going: nothing to start, nothing to ask
        e.update(missed=None, error=None)
    return None


def auto_ping_info(acc, e, entry, now):
    """For the UIs: the schedule, when the next ping is, a pending question, the last error."""
    conf = auto_ping_conf(acc)
    if not conf:
        return None
    e = e or {}
    current = e.get("sig") == auto_ping_sig(conf)  # a record from an older schedule: only its pings count
    info = {"mode": conf["mode"], "times": conf.get("times", []), "last_ping": e.get("last_ping"),
            "last_auto": e.get("last_auto"), "missed": e.get("missed") if current else None,
            "error": e.get("error") if current else None, "next_at": None}
    if (entry or {}).get("status") == "setup":
        return info
    if conf["mode"] == "times":
        info["next_at"] = iso(next_slot(conf["times"], now))
    else:
        last = parse_time(e.get("last_ping"))
        when = [now, session_busy_until(entry), parse_time(e.get("retry_at")) if current else None,
                last + dt.timedelta(seconds=AUTO_PING_GAP) if last else None]
        info["next_at"] = iso(max(t for t in when if t))
    return info


def record_pings(results):
    """Successful pings restart the every-5h chain and answer a pending question."""
    now = iso(utcnow())
    if not any(r["ok"] for r in results):
        return
    with schedule_state() as s:
        for r in results:
            if r["ok"]:
                s.setdefault(r["id"], {}).update(last_ping=now, missed=None, error=None, retry_at=None)


def run_auto_pings(cfg):
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


# ───────────────────────────── login ────────────────────────────────────────

def emit(event, as_json, **kw):
    if as_json:
        print(json.dumps(dict(event=event, **kw)), flush=True)
    elif event == "code":
        print(f"\n  Open {kw['verification_uri']} and enter code:  {kw['user_code']}"
              + ("  (copied to clipboard)" if kw.get("copied") else "") + "\n", flush=True)
    elif event in ("done", "error", "log", "info"):
        print(kw.get("message") or kw.get("line") or "", flush=True)


def login_copilot(acc, as_json):
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
    """Run a vendor CLI login (it opens the browser itself) and stream its output."""
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


def cmd_login(args):
    acc = find_account(load_config(), args.account)
    t = acc["type"]
    try:
        if t == "copilot":
            return login_copilot(acc, args.json)
        if t == "codex":
            exe = which("codex", acc.get("codex_bin"))
            if not exe:
                raise ProviderError("codex CLI not found — install it first (brew install codex / npm i -g @openai/codex)")
            home = codex_home(acc)
            os.makedirs(home, exist_ok=True)
            return login_cli(acc, [exe, "login"], tool_env(CODEX_HOME=home, OPENAI_API_KEY=None), args.json)
        if t == "claude":
            exe = which("claude", acc.get("claude_bin"))
            if not exe:
                raise ProviderError("claude CLI not found")
            default = claude_is_default_dir(acc)
            if not default:
                os.makedirs(claude_dir(acc), exist_ok=True)
            before = claude_keychain_services()
            env = tool_env(CLAUDE_CONFIG_DIR=None if default else claude_dir(acc))
            rc = login_cli(acc, [exe, "auth", "login"], env, args.json)
            # Remember which keychain item this config dir got (naming is a Claude Code detail).
            new = sorted(claude_keychain_services() - before)
            if rc == 0 and not default and len(new) == 1:
                update_account(acc["id"], keychain_service=new[0])
            return rc
        if t == "command":
            emit("error", args.json, message=acc.get("login_hint") or "This account has no sign-in")
            return 1
    except (ProviderError, ApiError) as e:
        emit("error", args.json, message=str(e))
        return 1
    return 1


# ───────────────────────────── config / state ───────────────────────────────

def autodetect_accounts():
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
    cfg = read_json(CONFIG_PATH)
    if cfg is None:
        if os.path.exists(CONFIG_PATH):
            raise SystemExit(f"ctrack: {CONFIG_PATH} is not valid JSON")
        cfg = {"refresh_minutes": DEFAULT_REFRESH_MIN, "menubar": "max", "accounts": autodetect_accounts()}
        write_json(CONFIG_PATH, cfg)
    cfg.setdefault("accounts", [])
    for a in cfg["accounts"]:
        if a.get("type") not in PROVIDERS:
            raise SystemExit(f"ctrack: account {a.get('id')!r} has unknown type {a.get('type')!r} "
                             f"(expected one of {', '.join(PROVIDERS)})")
    return cfg


def update_account(account_id, **fields):
    cfg = read_json(CONFIG_PATH, {}) or {}
    for a in cfg.get("accounts", []):
        if a.get("id") == account_id:
            a.update(fields)
    write_json(CONFIG_PATH, cfg)


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:32] or "account"


ACCOUNT_TYPES = {
    "claude": {"name": "Claude (Pro / Max)", "dir_key": "config_dir", "default_dir": "~/.claude"},
    "codex": {"name": "Codex (ChatGPT plan)", "dir_key": "codex_home", "default_dir": "~/.codex"},
    "copilot": {"name": "GitHub Copilot"},
    "command": {"name": "Command (JSON)"},
}


def cmd_add(args):
    cfg = load_config()
    t = args.type
    label = args.label or ACCOUNT_TYPES[t]["name"]
    taken = {a["id"] for a in cfg["accounts"]}
    base = slugify(args.id or label)
    acc_id, n = base, 2
    while acc_id in taken:
        acc_id, n = f"{base}-{n}", n + 1
    acc = {"id": acc_id, "label": label, "type": t}
    info = ACCOUNT_TYPES[t]
    if "dir_key" in info:
        # Each Claude / Codex subscription needs its own config dir to keep its own login.
        same = [a for a in cfg["accounts"] if a["type"] == t]
        used = {os.path.realpath(expand(a.get(info["dir_key"]) or info["default_dir"])) for a in same}
        suffix = re.sub(rf"^{t}-", "", acc_id) or acc_id
        directory = args.dir or (info["default_dir"] if os.path.realpath(expand(info["default_dir"])) not in used
                                 else f"{info['default_dir']}-{suffix}")
        acc[info["dir_key"]] = directory
    if t == "copilot" and normalize_host(args.host) not in ("", "github.com"):
        acc["host"] = normalize_host(args.host)
    if t == "command":
        if not (args.command or "").strip():
            raise SystemExit("ctrack add command: --command is required")
        acc["command"] = args.command.strip()
    if args.group and args.group.strip():
        acc["group"] = canonical_group(cfg, args.group)
    if args.this_device_only:
        acc["platforms"] = ["macos" if IS_MAC else "linux"]
    cfg["accounts"].append(acc)
    write_json(CONFIG_PATH, cfg)
    if args.json:
        print(json.dumps(acc))
    else:
        print(f"added {acc_id}" + (f" — sign in with: ctrack login {acc_id}" if t != "command" else ""))
    return 0


def cmd_edit(args):
    cfg = load_config()
    acc = find_account(cfg, args.account)
    if args.label is not None and args.label.strip():
        acc["label"] = args.label.strip()
    if args.group is not None:
        if args.group.strip():
            acc.pop("group", None)
            acc["group"] = canonical_group(cfg, args.group)
        else:
            acc.pop("group", None)
    if args.host is not None and acc["type"] == "copilot":
        host = normalize_host(args.host)
        if host != copilot_host(acc):
            if host in ("", "github.com"):
                acc.pop("host", None)
            else:
                acc["host"] = host
            # A token from one GitHub host is useless on another: sign in again.
            secrets = secrets_load()
            if secrets.pop(acc["id"], None) is not None:
                write_json(SECRETS_PATH, secrets, mode=0o600)
            state = read_json(STATE_PATH, {}) or {}
            if state.pop(acc["id"], None) is not None:
                write_json(STATE_PATH, state)
    if args.auto_ping is not None:
        try:
            conf = parse_auto_ping(args.auto_ping)
        except ValueError as e:
            raise SystemExit(f"ctrack: {e}") from None
        if conf and (not PROVIDERS[acc["type"]]["ping"] or acc.get("ping") is False):
            raise SystemExit(f"ctrack: {acc['id']} has no ping, so no auto ping")
        if conf:
            acc["auto_ping"] = conf
        else:
            acc.pop("auto_ping", None)
    write_json(CONFIG_PATH, cfg)
    print(json.dumps(acc) if args.json else f"updated {acc['id']}")
    return 0


def cmd_rename_group(args):
    cfg = load_config()
    old, new = args.old.strip().casefold(), args.new.strip()
    n = 0
    for a in cfg["accounts"]:
        if (a.get("group") or "").casefold() == old:
            if new:
                a["group"] = new
            else:
                a.pop("group", None)
            n += 1
    cfg["groups"] = [new if g.casefold() == old else g for g in cfg.get("groups") or [] if new or g.casefold() != old]
    colors = cfg.get("group_colors") or {}
    for k in [k for k in colors if k.casefold() == old]:
        color = colors.pop(k)
        if new:
            colors[new] = color
    if colors:
        cfg["group_colors"] = colors
    else:
        cfg.pop("group_colors", None)
    if not cfg["groups"]:
        cfg.pop("groups")
    write_json(CONFIG_PATH, cfg)
    print(json.dumps({"renamed": n}) if args.json else f"renamed group on {n} account(s)")
    return 0


def cmd_group_color(args):
    cfg = load_config()
    colors = {k: v for k, v in (cfg.get("group_colors") or {}).items() if k.casefold() != args.group.casefold()}
    colors[canonical_group(cfg, args.group)] = args.color
    cfg["group_colors"] = colors
    write_json(CONFIG_PATH, cfg)
    print(json.dumps({"group": args.group, "color": args.color}) if args.json else f"{args.group}: {args.color}")
    return 0


def cmd_remove(args):
    cfg = load_config()
    find_account(cfg, args.account)
    cfg["accounts"] = [a for a in cfg["accounts"] if a.get("id") != args.account]
    write_json(CONFIG_PATH, cfg)
    secrets = secrets_load()
    if secrets.pop(args.account, None) is not None:
        write_json(SECRETS_PATH, secrets, mode=0o600)
    state = read_json(STATE_PATH, {}) or {}
    if state.pop(args.account, None) is not None:
        write_json(STATE_PATH, state)
    if args.account in (read_json(SCHEDULE_PATH, {}) or {}):
        with schedule_state() as s:
            s.pop(args.account, None)
    print(json.dumps({"removed": args.account}) if args.json else f"removed {args.account}")
    return 0


def enabled_here(acc):
    if acc.get("enabled") is False:
        return False
    plats = acc.get("platforms")
    here = "macos" if IS_MAC else "linux"
    return not plats or here in plats or sys.platform in plats


def find_account(cfg, account_id):
    for a in cfg["accounts"]:
        if a.get("id") == account_id:
            return a
    raise SystemExit(f"ctrack: no account {account_id!r} in {CONFIG_PATH}")


def refresh_seconds(acc, cfg):
    m = acc.get("refresh_minutes") or PROVIDERS[acc["type"]].get("refresh_minutes") \
        or cfg.get("refresh_minutes") or DEFAULT_REFRESH_MIN
    return max(1, float(m)) * 60


def fetch_into_state(acc, state):
    entry = state.get(acc["id"]) or {}
    entry["fetched_at"] = iso(utcnow())
    try:
        data = PROVIDERS[acc["type"]]["fetch"](acc)
        partial = data.pop("_partial_error", None)
        entry.update(data=data, error=partial, hint=None, status="stale" if partial else "ok")
        if not partial:
            entry["ok_at"] = entry["fetched_at"]
    except ProviderError as e:
        entry.update(error=str(e), hint=e.hint, status="setup" if e.setup else "error")
    except Exception as e:  # never let one provider break the others
        entry.update(error=f"{type(e).__name__}: {e}", hint=None, status="error")
    state[acc["id"]] = entry


def decay(m, now):
    """Windows whose reset time passed are back to zero, even if our data is old."""
    reset = parse_time(m.get("resets_at"))
    if reset and reset <= now:
        m = dict(m, percent=0.0 if m.get("percent") is not None else None, resets_at=None,
                 idle=m.get("key") == "session")
        m["text"] = "idle" if m["idle"] else ("0% (reset)" if m["percent"] is not None else m["text"])
    return m


def cli_command(acc):
    """How to use this account from a terminal."""
    if acc["type"] == "claude":
        return "claude" if claude_is_default_dir(acc) else f"CLAUDE_CONFIG_DIR={acc['config_dir']} claude"
    if acc["type"] == "codex":
        home = acc.get("codex_home") or "~/.codex"
        return "codex" if os.path.realpath(expand(home)) == os.path.realpath(expand("~/.codex")) else f"CODEX_HOME={home} codex"
    return None


def build_account(acc, entry, now, sched=None):
    prov = PROVIDERS[acc["type"]]
    data = entry.get("data") or {}
    status = entry.get("status") or "pending"
    if status in ("error", "setup") and data:
        status = "stale" if status == "error" else status
    return {
        "id": acc["id"], "label": acc.get("label") or acc["id"], "type": acc["type"], "vendor": prov["vendor"],
        "plan": acc.get("plan") or data.get("plan"), "identity": data.get("identity"),
        "status": status, "error": entry.get("error"), "hint": entry.get("hint"),
        "fetched_at": entry.get("fetched_at"), "ok_at": entry.get("ok_at"),
        "can_ping": bool(prov["ping"]) and acc.get("ping") is not False,
        "can_login": acc["type"] != "command",
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


def canonical_group(cfg, name):
    """Reuse the existing spelling of a group ("work" → "Work")."""
    name = (name or "").strip()
    for g in list(cfg.get("groups") or []) + [a.get("group") for a in cfg["accounts"]]:
        if g and g.casefold() == name.casefold():
            return g
    return name


GROUP_PALETTE = ["blue", "green", "purple", "orange", "pink", "teal", "red", "gray"]


def group_colors(cfg, groups):
    """Configured "group_colors" or a palette colour by position."""
    conf = {k.casefold(): v for k, v in (cfg.get("group_colors") or {}).items()}
    return {g: conf.get(g.casefold()) or GROUP_PALETTE[i % len(GROUP_PALETTE)] for i, g in enumerate(groups)}


def group_rank(cfg, group):
    """Optional config "groups": ["Work", "Personal"] fixes the order; others alphabetical; ungrouped last."""
    order = [g.casefold() for g in cfg.get("groups") or []]
    if not group:
        return (2, "")
    g = group.casefold()
    return (0, f"{order.index(g):04d}") if g in order else (1, g)


def collect(cfg, ids=None, force=False, cached=False):
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


# ───────────────────────────── CLI ──────────────────────────────────────────

def human_delta(t):
    secs = int((t - utcnow()).total_seconds())
    if secs <= 0:
        return "now"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d {h}h" if d else (f"{h}h {m:02d}m" if h else f"{m}m")


def print_human(result):
    use_color = sys.stdout.isatty()

    def c(code, s):
        return f"\033[{code}m{s}\033[0m" if use_color else s

    current = object()
    for a in result["accounts"]:
        if result.get("groups") and a.get("group") != current:
            current = a.get("group")
            print(c("1;4", (current or "Other").upper()))
        head = c("1", a["label"])
        extra = " · ".join(x for x in (a.get("plan"), a.get("identity")) if x)
        print(f"{head}  {c('2', extra)}")
        for m in a["metrics"]:
            pct = m.get("percent")
            bar = ""
            if pct is not None:
                n = min(10, int(round(pct / 10)))
                col = "32" if pct < 60 else ("33" if pct < 85 else "31")
                bar = c(col, "█" * n) + c("2", "░" * (10 - n))
            reset = parse_time(m.get("resets_at"))
            when = f"resets in {human_delta(reset)}" if reset else ""
            print(f"  {m['label']:<26} {bar:<10} {m['text']:>16}  {c('2', when)}")
        for n in a["notes"]:
            print(f"  {c('2', '• ' + n)}")
        ap = a.get("auto_ping")
        if ap:
            def hm(v):
                return parse_time(v).astimezone().strftime("%H:%M")
            line = "Auto ping " + ("every 5h" if ap["mode"] == "interval" else "at " + ", ".join(ap["times"]))
            if ap.get("next_at"):
                line += f" · next {hm(ap['next_at'])}"
            print(f"  {c('2', '⏱ ' + line)}")
            if ap.get("missed"):
                print(f"  {c('33', '? ' + hm(ap['missed']) + ' ping ' + ('failed' if ap.get('error') else 'missed'))}"
                      + f"  {c('2', '→ ctrack ping ' + a['id'] + '  |  ctrack auto-ping --dismiss ' + a['id'])}")
            elif ap.get("error"):
                print(f"  {c('31', '! Auto ping failed: ' + ap['error'])}")
        if a.get("error"):
            print(f"  {c('31', '! ' + a['error'])}" + (f"  {c('2', '→ ' + a['hint'])}" if a.get("hint") else ""))
        print()


def cmd_status(args):
    cfg = load_config()
    res = collect(cfg, ids=args.accounts or None, force=args.force, cached=args.cached)
    if args.json:
        print(json.dumps(res, indent=None if args.compact else 2))
    else:
        print_human(res)
    return 0


def cmd_ping(args):
    cfg = load_config()
    accs = [a for a in cfg["accounts"] if enabled_here(a) and PROVIDERS[a["type"]]["ping"] and a.get("ping") is not False]
    if args.accounts:
        accs = [find_account(cfg, i) for i in args.accounts]
    elif args.idle:
        status = collect(cfg, cached=True)
        idle_ids = {a["id"] for a in status["accounts"]
                    if a["status"] in ("ok", "stale") and any(m["key"] == "session" and m["idle"] for m in a["metrics"])}
        accs = [a for a in accs if a["id"] in idle_ids]
    elif not args.all:
        raise SystemExit("ctrack ping: give account ids, --all or --idle")
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(ping_account, accs))
    record_pings(results)
    if any(r["ok"] for r in results):
        time.sleep(2)
        collect(cfg, ids=[r["id"] for r in results if r["ok"]], force=True)
    if args.json:
        print(json.dumps({"results": results}))
    else:
        for r in results:
            print(f"{r['id']}: " + (f"ok ({r['seconds']}s) {r.get('output', '')}" if r["ok"] else f"FAILED — {r.get('error')}"))
        if not results:
            print("nothing to ping")
    return 0 if all(r["ok"] for r in results) else 1


def cmd_auto_ping(args):
    cfg = load_config()
    if args.dismiss:  # "wait for the next scheduled time"
        with schedule_state() as s:
            for i in args.dismiss:
                if i in s:
                    s[i].update(missed=None, error=None)
        print(json.dumps({"dismissed": args.dismiss}) if args.json else "ok")
        return 0
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(SCHEDULE_PATH + ".run", "a") as run_lock:
        try:  # one run at a time: a ping can outlast the UIs' one-minute timer
            fcntl.flock(run_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(json.dumps({"busy": True, "pinged": [], "missed": []}) if args.json else "already running")
            return 0
        res = run_auto_pings(cfg)
    if args.json:
        print(json.dumps(dict(res, busy=False)))
        return 0
    for r in res["pinged"]:
        print(f"{r['id']}: " + (f"pinged ({r['seconds']}s)" if r["ok"] else f"FAILED — {r.get('error')}"))
    for m in res["missed"]:
        when = parse_time(m["slot"]).astimezone().strftime("%H:%M")
        nxt = f", next at {parse_time(m['next']).astimezone().strftime('%H:%M')}" if m.get("next") else ""
        print(f"{m['id']}: {when} ping " + (f"failed ({m['error']})" if m.get("error") else "missed")
              + f"{nxt} — `ctrack ping {m['id']}` now, or `ctrack auto-ping --dismiss {m['id']}` to wait")
    if not res["pinged"] and not res["missed"]:
        print("nothing due")
    return 0


def cmd_init(args):
    if os.path.exists(CONFIG_PATH) and not args.force:
        print(f"{CONFIG_PATH} already exists (use --force to overwrite)")
        return 1
    cfg = {"refresh_minutes": DEFAULT_REFRESH_MIN, "menubar": "max", "accounts": autodetect_accounts()}
    write_json(CONFIG_PATH, cfg)
    print(f"wrote {CONFIG_PATH} with {len(cfg['accounts'])} detected account(s)")
    return 0


def cmd_paths(_args):
    print(json.dumps({"config": CONFIG_PATH, "secrets": SECRETS_PATH, "state": STATE_PATH,
                      "python": sys.executable, "version": VERSION}, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ctrack", description="Coding-assistant credits & limits tracker")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("status", help="show usage for all accounts")
    s.add_argument("accounts", nargs="*")
    s.add_argument("--json", action="store_true")
    s.add_argument("--compact", action="store_true", help="single-line JSON")
    s.add_argument("--force", action="store_true", help="ignore cache freshness")
    s.add_argument("--cached", action="store_true", help="no network, cache only")
    p = sub.add_parser("ping", help="send a tiny prompt (smallest model/effort) to start the session timer")
    p.add_argument("accounts", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--idle", action="store_true", help="only accounts whose 5h session is not running")
    p.add_argument("--json", action="store_true")
    au = sub.add_parser("auto-ping", help="run the scheduled pings that are due (the UIs call it every minute)")
    au.add_argument("--dismiss", action="append", metavar="ACCOUNT_ID",
                     help="drop the 'missed scheduled ping' question: wait for the next scheduled time")
    au.add_argument("--json", action="store_true")
    lg = sub.add_parser("login", help="sign an account in on this device")
    lg.add_argument("account")
    lg.add_argument("--json", action="store_true", help="stream JSON events (for the UIs)")
    ad = sub.add_parser("add", help="add an account (any number per type)")
    ad.add_argument("type", choices=list(ACCOUNT_TYPES))
    ad.add_argument("--label")
    ad.add_argument("--id")
    ad.add_argument("--dir", help="CLAUDE_CONFIG_DIR / CODEX_HOME for this account (default: ~/.claude-<id>, ~/.codex-<id>)")
    ad.add_argument("--host", help="Copilot: GitHub host, e.g. github.com or mycorp.ghe.com")
    ad.add_argument("--command", help="command: prints the account's usage as JSON (see README)")
    ad.add_argument("--group", help="e.g. Work / Personal")
    ad.add_argument("--this-device-only", action="store_true")
    ad.add_argument("--json", action="store_true")
    ed = sub.add_parser("edit", help="rename an account / change its GitHub host")
    ed.add_argument("account")
    ed.add_argument("--label")
    ed.add_argument("--group", help='group name, "" to remove from its group')
    ed.add_argument("--host", help="Copilot: github.com or e.g. mycorp.ghe.com (clears the stored token)")
    ed.add_argument("--auto-ping", metavar="off|5h|HH:MM,...",
                    help="ping on its own: 5h = 5 h 1 min after each ping; or at these local times; off")
    ed.add_argument("--json", action="store_true")
    rg = sub.add_parser("rename-group", help='rename a group ("" to ungroup its accounts)')
    rg.add_argument("old")
    rg.add_argument("new")
    rg.add_argument("--json", action="store_true")
    gc = sub.add_parser("group-color", help="set a group's colour (" + ", ".join(GROUP_PALETTE) + ")")
    gc.add_argument("group")
    gc.add_argument("color", choices=GROUP_PALETTE)
    gc.add_argument("--json", action="store_true")
    rm = sub.add_parser("remove", help="remove an account from the config (and its stored token)")
    rm.add_argument("account")
    rm.add_argument("--json", action="store_true")
    i = sub.add_parser("init", help="write a starter config")
    i.add_argument("--force", action="store_true")
    sub.add_parser("paths")
    args = ap.parse_args(argv)
    handlers = {"status": cmd_status, "ping": cmd_ping, "auto-ping": cmd_auto_ping, "login": cmd_login, "add": cmd_add, "edit": cmd_edit, "rename-group": cmd_rename_group, "group-color": cmd_group_color, "remove": cmd_remove,
                "init": cmd_init, "paths": cmd_paths}
    if not args.cmd:
        args = ap.parse_args(["status"] + (argv or sys.argv[1:]))
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
