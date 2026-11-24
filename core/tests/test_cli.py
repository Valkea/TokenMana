"""Config, secrets and the ``mana`` commands that change them."""
import os
import stat

import pytest

from tests.helpers import read, write
from tokenmana.cli import main
from tokenmana.config import load_config, secrets_save
from tokenmana.constants import CONFIG_PATH, HOME, SCHEDULE_PATH, SECRETS_PATH, STATE_PATH


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_invalid_config_is_never_overwritten():
    write(CONFIG_PATH, '{"accounts": [')
    with pytest.raises(SystemExit):
        load_config()
    with open(CONFIG_PATH) as f:
        assert f.read() == '{"accounts": ['


def test_missing_config_is_created_from_the_logins_on_this_device():
    write(os.path.join(HOME, ".claude", ".credentials.json"), {"claudeAiOauth": {}})
    write(os.path.join(HOME, ".codex-work", "auth.json"), {"tokens": {}})
    cfg = load_config()
    assert [(a["id"], a["type"], a.get("codex_home")) for a in cfg["accounts"]] == [
        ("claude", "claude", None), ("codex-work", "codex", "~/.codex-work")]
    assert read(CONFIG_PATH) == cfg


def test_secrets_are_private():
    secrets_save("gh", "gho_secret")
    assert mode(SECRETS_PATH) == 0o600
    assert read(SECRETS_PATH) == {"gh": "gho_secret"}


def test_each_claude_account_gets_its_own_id_and_login_dir():
    write(CONFIG_PATH, {"groups": ["Work"], "accounts": []})
    main(["add", "claude", "--label", "Claude"])
    main(["add", "claude", "--label", "Claude"])
    main(["add", "claude", "--label", "Work Max", "--group", "work"])
    main(["add", "copilot", "--label", "GH", "--host", "https://MyCorp.ghe.com/"])
    accounts = read(CONFIG_PATH)["accounts"]
    assert [(a["id"], a.get("config_dir")) for a in accounts[:3]] == [
        ("claude", "~/.claude"), ("claude-2", "~/.claude-2"), ("work-max", "~/.claude-work-max")]
    assert accounts[2]["group"] == "Work", "an existing group keeps its spelling"
    assert accounts[3]["host"] == "mycorp.ghe.com"


@pytest.fixture
def stored():
    """A config with a Copilot and a Claude account, plus a token, cache and schedule for them."""
    write(CONFIG_PATH, {"accounts": [{"id": "gh", "type": "copilot"}, {"id": "c", "type": "claude"}]})
    write(SECRETS_PATH, {"gh": "gho_old", "other": "x"})
    write(STATE_PATH, {"gh": {"status": "ok"}, "c": {"status": "ok"}})
    write(SCHEDULE_PATH, {"c": {"last_ping": "2026-01-15T10:00:00Z"}})


@pytest.mark.usefixtures("stored")
def test_changing_the_github_host_drops_the_token_and_cache():
    main(["edit", "gh", "--host", "mycorp.ghe.com"])
    assert read(CONFIG_PATH)["accounts"][0]["host"] == "mycorp.ghe.com"
    assert read(SECRETS_PATH) == {"other": "x"}
    assert mode(SECRETS_PATH) == 0o600
    assert list(read(STATE_PATH)) == ["c"]

    main(["edit", "gh", "--host", "https://github.com/"])
    assert "host" not in read(CONFIG_PATH)["accounts"][0]


@pytest.mark.usefixtures("stored")
def test_auto_ping_setting():
    main(["edit", "c", "--auto-ping", "13:00, 8"])
    assert read(CONFIG_PATH)["accounts"][1]["auto_ping"] == {"mode": "times", "times": ["08:00", "13:00"]}
    main(["edit", "c", "--auto-ping", "off"])
    assert "auto_ping" not in read(CONFIG_PATH)["accounts"][1]


@pytest.mark.usefixtures("stored")
@pytest.mark.parametrize("argv", [["c", "--auto-ping", "25:00"], ["gh", "--auto-ping", "5h"]],
                         ids=["invalid-time", "account-cannot-ping"])
def test_auto_ping_setting_is_refused(argv):
    with pytest.raises(SystemExit):
        main(["edit", *argv])


@pytest.mark.usefixtures("stored")
def test_remove_forgets_everything_stored_for_the_account():
    main(["remove", "c"])
    assert [a["id"] for a in read(CONFIG_PATH)["accounts"]] == ["gh"]
    assert "c" not in read(STATE_PATH)
    assert "c" not in read(SCHEDULE_PATH)
    main(["remove", "gh"])
    assert read(SECRETS_PATH) == {"other": "x"}
    with pytest.raises(SystemExit):
        main(["remove", "gh"])


def test_rename_group_is_case_insensitive_and_keeps_order_and_colour():
    write(CONFIG_PATH, {"groups": ["Work", "Perso"], "group_colors": {"work": "red", "Perso": "teal"},
                        "accounts": [{"id": "a", "type": "claude", "group": "work"},
                                     {"id": "b", "type": "claude", "group": "Work"},
                                     {"id": "c", "type": "claude", "group": "Perso"}]})
    main(["rename-group", "WORK", "Job"])
    cfg = read(CONFIG_PATH)
    assert [a["group"] for a in cfg["accounts"]] == ["Job", "Job", "Perso"]
    assert (cfg["groups"], cfg["group_colors"]) == (["Job", "Perso"], {"Perso": "teal", "Job": "red"})

    main(["rename-group", "perso", ""])  # ungroup
    cfg = read(CONFIG_PATH)
    assert "group" not in cfg["accounts"][2]
    assert (cfg["groups"], cfg["group_colors"]) == (["Job"], {"Job": "red"})
