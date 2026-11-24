"""Pings: the vendor CLI command, and what an auto-ping run records."""
import datetime as dt
import os
from unittest import mock

from tests.helpers import fake_cli, local, read, write
from tokenmana import ping
from tokenmana.constants import HOME, SCHEDULE_PATH
from tokenmana.schedule import AUTO_PING_RETRY, auto_ping_sig
from tokenmana.util import iso


def test_claude_ping_uses_the_account_login_never_an_api_key(monkeypatch):
    exe = fake_cli("claude")
    for var, value in {"ANTHROPIC_API_KEY": "sk-paid", "ANTHROPIC_AUTH_TOKEN": "t", "CLAUDE_CODE_USE_BEDROCK": "1",
                       "CLAUDE_CONFIG_DIR": "/some/other/account"}.items():
        monkeypatch.setenv(var, value)
    argv, env = ping.ping_command({"id": "w", "type": "claude", "claude_bin": exe, "config_dir": "~/.claude-w"})
    _, default_env = ping.ping_command({"id": "d", "type": "claude", "claude_bin": exe})

    assert not {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK"} & set(env)
    assert env["CLAUDE_CONFIG_DIR"] == os.path.join(HOME, ".claude-w")
    assert "CLAUDE_CONFIG_DIR" not in default_env, "the default account must not inherit another dir"
    assert argv[0] == exe
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[-1] == ping.PING_PROMPT


def test_codex_ping_uses_the_account_home_never_an_api_key(monkeypatch):
    exe = fake_cli("codex")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-paid")
    argv, env = ping.ping_command({"id": "c", "type": "codex", "codex_bin": exe, "codex_home": "~/.codex-w"})
    assert "OPENAI_API_KEY" not in env
    assert env["CODEX_HOME"] == os.path.join(HOME, ".codex-w")
    assert argv[-1] == ping.PING_PROMPT


def test_ping_account_reports_the_cli_outcome():
    res = ping.ping_account({"id": "a", "type": "claude", "claude_bin": fake_cli("claude", "echo starting; echo pong")})
    assert (res["ok"], res["output"]) == (True, "pong")

    failing = fake_cli("claude-bad", "echo 'usage limit reached' >&2; exit 1")
    res = ping.ping_account({"id": "a", "type": "claude", "claude_bin": failing})
    assert (res["ok"], res["error"]) == (False, "usage limit reached")

    assert ping.ping_account({"id": "g", "type": "copilot"})["ok"] is False


def test_auto_ping_run_records_each_outcome_for_its_mode(monkeypatch):
    now = local(2026, 1, 15, 8, 1)
    times = {"mode": "times", "times": ["08:00"]}
    cfg = {"accounts": [
        {"id": "ok", "type": "claude", "auto_ping": {"mode": "interval"}},
        {"id": "times", "type": "codex", "auto_ping": times},
        {"id": "interval", "type": "claude", "auto_ping": {"mode": "interval"}},
        {"id": "recent", "type": "claude", "auto_ping": {"mode": "interval"}},
        {"id": "manual", "type": "claude"},
        {"id": "disabled", "type": "claude", "enabled": False, "auto_ping": {"mode": "interval"}},
    ]}
    write(SCHEDULE_PATH, {
        "times": {"sig": auto_ping_sig(times), "handled_until": iso(local(2026, 1, 15, 7, 59))},
        "recent": {"last_ping": iso(now - dt.timedelta(hours=1))},
    })

    def fake_ping(acc):
        ok = acc["id"] == "ok"
        return {"id": acc["id"], "ok": ok, "seconds": 1.0, **({} if ok else {"error": "boom"})}

    collect = mock.Mock()
    monkeypatch.setattr(ping, "utcnow", lambda: now)
    monkeypatch.setattr(ping, "ping_account", fake_ping)
    monkeypatch.setattr(ping, "collect", collect)
    monkeypatch.setattr(ping.time, "sleep", lambda s: None)
    res = ping.run_auto_pings(cfg)

    assert sorted(r["id"] for r in res["pinged"]) == ["interval", "ok", "times"]
    s = read(SCHEDULE_PATH)
    # Success restarts the every-5h chain.
    assert (s["ok"]["last_ping"], s["ok"]["last_auto"]) == (iso(now), iso(now))
    # Failed scheduled time: becomes the "ping now, or wait for the next time?" question.
    assert (s["times"]["missed"], s["times"]["error"]) == (iso(local(2026, 1, 15, 8, 0)), "boom")
    # Failed every-5h ping: retried later, nothing to ask.
    assert s["interval"]["retry_at"] == iso(now + dt.timedelta(seconds=AUTO_PING_RETRY))
    assert s["interval"].get("missed") is None
    assert res["missed"] == [{"id": "times", "label": "times", "slot": iso(local(2026, 1, 15, 8, 0)),
                              "error": "boom", "next": iso(local(2026, 1, 16, 8, 0))}]
    collect.assert_called_once_with(cfg, ids=["ok"], force=True)  # refresh what was pinged
