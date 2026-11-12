"""The ``mana`` command line: subcommands and terminal output."""
from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import json
import os
import re
import sys
import time

from .config import (
    ACCOUNT_TYPES, autodetect_accounts, canonical_group, enabled_here, find_account, load_config,
    secrets_load, slugify, update_account,
)
from .constants import (
    CACHE_DIR, CONFIG_PATH, DEFAULT_REFRESH_MIN, IS_MAC, SCHEDULE_PATH, SECRETS_PATH, STATE_PATH, VERSION,
)
from .errors import ApiError, ProviderError
from .login import emit, login_cli, login_copilot
from .ping import ping_account, run_auto_pings
from .providers import PROVIDERS, provider
from .providers.claude import claude_dir, claude_is_default_dir, claude_keychain_services
from .providers.codex import codex_home
from .providers.copilot import copilot_host, normalize_host
from .schedule import parse_auto_ping, record_pings, schedule_state
from .status import GROUP_PALETTE, collect
from .util import expand, parse_time, read_json, tool_env, utcnow, which, write_json


def human_delta(t):
    """Format the time left until `t`, e.g. ``"2d 3h"``, ``"1h 05m"``, ``"12m"``.

    Parameters
    ----------
    t : datetime.datetime

    Returns
    -------
    str
        ``"now"`` when `t` is past.
    """
    secs = int((t - utcnow()).total_seconds())
    if secs <= 0:
        return "now"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d {h}h" if d else (f"{h}h {m:02d}m" if h else f"{m}m")


def print_human(result):
    """Print the status for a terminal, in colour when it is a TTY.

    Parameters
    ----------
    result : dict
        Output of `collect`.
    """
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
                      + f"  {c('2', '→ mana ping ' + a['id'] + '  |  mana auto-ping --dismiss ' + a['id'])}")
            elif ap.get("error"):
                print(f"  {c('31', '! Auto ping failed: ' + ap['error'])}")
        if a.get("error"):
            print(f"  {c('31', '! ' + a['error'])}" + (f"  {c('2', '→ ' + a['hint'])}" if a.get("hint") else ""))
        print()


def cmd_status(args):
    """``mana status``: print the usage of the accounts.

    Parameters
    ----------
    args : argparse.Namespace
        ``accounts``, ``json``, ``compact``, ``force`` and ``cached``.

    Returns
    -------
    int
        Exit code.
    """
    cfg = load_config()
    res = collect(cfg, ids=args.accounts or None, force=args.force, cached=args.cached)
    if args.json:
        print(json.dumps(res, indent=None if args.compact else 2))
    else:
        print_human(res)
    return 0


def cmd_ping(args):
    """``mana ping``: start the session timer of accounts with a tiny prompt.

    Accounts pinged successfully are refreshed afterwards.

    Parameters
    ----------
    args : argparse.Namespace
        ``accounts``, ``all``, ``idle`` (only accounts with no running
        session) and ``json``.

    Returns
    -------
    int
        0 when every ping succeeded, else 1.

    Raises
    ------
    SystemExit
        When no selection is given, or an account is unknown.
    """
    cfg = load_config()
    accs = [a for a in cfg["accounts"] if enabled_here(a) and provider(a)["ping"] and a.get("ping") is not False]
    if args.accounts:
        accs = [find_account(cfg, i) for i in args.accounts]
    elif args.idle:
        status = collect(cfg, cached=True)
        idle_ids = {a["id"] for a in status["accounts"]
                    if a["status"] in ("ok", "stale") and any(m["key"] == "session" and m["idle"] for m in a["metrics"])}
        accs = [a for a in accs if a["id"] in idle_ids]
    elif not args.all:
        raise SystemExit("mana ping: give account ids, --all or --idle")
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
    """``mana auto-ping``: run the scheduled pings that are due.

    One run at a time: a run that finds another one going returns at once.
    With ``--dismiss``, drops the missed-ping questions of the given
    accounts instead.

    Parameters
    ----------
    args : argparse.Namespace
        ``dismiss`` and ``json``.

    Returns
    -------
    int
        Exit code.
    """
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
              + f"{nxt} — `mana ping {m['id']}` now, or `mana auto-ping --dismiss {m['id']}` to wait")
    if not res["pinged"] and not res["missed"]:
        print("nothing due")
    return 0


def cmd_login(args):
    """``mana login ACCOUNT_ID``: sign an account in on this device.

    Copilot uses the GitHub device flow; Claude and Codex run their CLI's
    login in the account's config dir. For Claude, the keychain item the
    login creates is saved as the account's ``keychain_service``.

    Parameters
    ----------
    args : argparse.Namespace
        ``account`` and ``json``.

    Returns
    -------
    int
        Exit code.
    """
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
        if not provider(acc)["login"]:
            emit("error", args.json, message=acc.get("login_hint") or "This account has no sign-in")
            return 1
    except (ProviderError, ApiError) as e:
        emit("error", args.json, message=str(e))
        return 1
    return 1


def cmd_add(args):
    """``mana add TYPE``: add an account to the config.

    The id comes from ``--id`` or the label and is made unique. Each extra
    Claude / Codex account gets its own config dir (``~/.claude-<id>``,
    ``~/.codex-<id>``) so that it keeps its own login.

    Parameters
    ----------
    args : argparse.Namespace
        ``type``, ``label``, ``id``, ``dir``, ``host``, ``command``, ``group``,
        ``this_device_only`` and ``json``.

    Returns
    -------
    int
        Exit code.

    Raises
    ------
    SystemExit
        When a command account has no ``--command``.
    """
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
            raise SystemExit("mana add command: --command is required")
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
        print(f"added {acc_id}" + (f" — sign in with: mana login {acc_id}" if PROVIDERS[t]["login"] else ""))
    return 0


def cmd_edit(args):
    """``mana edit ACCOUNT_ID``: change an account's label, group, host or auto ping.

    Changing a Copilot host drops the stored token and the cached data.

    Parameters
    ----------
    args : argparse.Namespace
        ``account``, ``label``, ``group`` (empty to ungroup), ``host``,
        ``auto_ping`` and ``json``.

    Returns
    -------
    int
        Exit code.

    Raises
    ------
    SystemExit
        When the account is unknown, or the auto-ping value is invalid or not
        supported by the account.
    """
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
            raise SystemExit(f"mana: {e}") from None
        if conf and (not provider(acc)["ping"] or acc.get("ping") is False):
            raise SystemExit(f"mana: {acc['id']} has no ping, so no auto ping")
        if conf:
            acc["auto_ping"] = conf
        else:
            acc.pop("auto_ping", None)
    write_json(CONFIG_PATH, cfg)
    print(json.dumps(acc) if args.json else f"updated {acc['id']}")
    return 0


def cmd_rename_group(args):
    """``mana rename-group OLD NEW``: rename a group, or ungroup its accounts.

    Matching is case-insensitive; the group's place in ``groups`` and its
    colour follow the new name.

    Parameters
    ----------
    args : argparse.Namespace
        ``old``, ``new`` (empty to ungroup) and ``json``.

    Returns
    -------
    int
        Exit code.
    """
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
    """``mana group-color GROUP COLOR``: set a group's colour.

    Parameters
    ----------
    args : argparse.Namespace
        ``group``, ``color`` (one of `GROUP_PALETTE`) and ``json``.

    Returns
    -------
    int
        Exit code.
    """
    cfg = load_config()
    colors = {k: v for k, v in (cfg.get("group_colors") or {}).items() if k.casefold() != args.group.casefold()}
    colors[canonical_group(cfg, args.group)] = args.color
    cfg["group_colors"] = colors
    write_json(CONFIG_PATH, cfg)
    print(json.dumps({"group": args.group, "color": args.color}) if args.json else f"{args.group}: {args.color}")
    return 0


def cmd_remove(args):
    """``mana remove ACCOUNT_ID``: remove an account and everything stored for it.

    Drops it from the config, along with its token, cached data and
    auto-ping record.

    Parameters
    ----------
    args : argparse.Namespace
        ``account`` and ``json``.

    Returns
    -------
    int
        Exit code.

    Raises
    ------
    SystemExit
        When the account is unknown.
    """
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


def cmd_init(args):
    """``mana init``: write a starter config with the detected accounts.

    Parameters
    ----------
    args : argparse.Namespace
        ``force``: overwrite an existing config.

    Returns
    -------
    int
        1 when the config exists and ``--force`` is not given, else 0.
    """
    if os.path.exists(CONFIG_PATH) and not args.force:
        print(f"{CONFIG_PATH} already exists (use --force to overwrite)")
        return 1
    cfg = {"refresh_minutes": DEFAULT_REFRESH_MIN, "menubar": "max", "accounts": autodetect_accounts()}
    write_json(CONFIG_PATH, cfg)
    print(f"wrote {CONFIG_PATH} with {len(cfg['accounts'])} detected account(s)")
    return 0


def cmd_paths(_args):
    """``mana paths``: print the config, secrets and cache locations as JSON.

    Parameters
    ----------
    _args : argparse.Namespace
        Unused.

    Returns
    -------
    int
        Exit code.
    """
    print(json.dumps({"config": CONFIG_PATH, "secrets": SECRETS_PATH, "state": STATE_PATH,
                      "python": sys.executable, "version": VERSION}, indent=2))
    return 0


def main(argv=None):
    """Parse the command line and run a ``mana`` subcommand.

    Without a subcommand, runs ``status`` with the given arguments.

    Parameters
    ----------
    argv : list of str, optional
        Arguments; defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        Exit code.
    """
    ap = argparse.ArgumentParser(prog="mana", description="Coding-assistant credits & limits tracker")
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
