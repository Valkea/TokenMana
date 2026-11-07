# CreditsTracker

One glance at every coding-assistant subscription: session (5 h) and weekly usage with reset
countdowns, monthly premium-request quotas, and budget spend. One click **pings** an account
with its smallest model at low effort, which starts its 5 h session timer early.

| Platform | UI | Where |
|---|---|---|
| macOS 14+ | menu-bar app (native `NSMenu`) | `macos/` |
| Ubuntu 24.04 (GNOME 45–49) | top-bar extension | `gnome/` |
| both | `ctrack` CLI = the shared core both UIs call | `core/ctrack.py` |

The core is a single dependency-free Python file (3.9+). Every provider integration lives there, so
the two UIs stay thin and behave identically.

## Install

```sh
./install.sh          # macOS: builds + installs ~/Applications/CreditsTracker.app
                      # Ubuntu: installs the GNOME extension (log out/in once, then enable)
```

macOS needs only the Xcode Command Line Tools (no Xcode). Turn on *Settings → Launch at Login* in the menu.

## Accounts

Add as many accounts of each type as you like: **Add Account… (⌘N)** in the menu (macOS), the add
button (GNOME), or the CLI:

```sh
ctrack add claude  --label "Claude Max · work"     # → own config dir ~/.claude-max-work
ctrack add codex   --label "Codex Pro · client"    # → own CODEX_HOME ~/.codex-pro-client
ctrack add copilot --label "Copilot · company"     # GitHub device-code sign-in
ctrack add command --label "Team budget" --command "my-usage --json"   # your own provider
ctrack login <id>        # sign in (the UIs do this for you after "Add & sign in")
ctrack edit <id> --label "New name" [--group Work] [--host mycorp.ghe.com]
ctrack remove <id>
```

In the menus: an account's submenu → *Edit Account…* (macOS), or its ✎ button (GNOME), to rename it, change its
group or change its GitHub host.

**Groups** (e.g. *Work* / *Personal*): give accounts a group when adding or editing them, or on macOS use the
account's submenu → *Move to Group ▸*. The menu then shows one section per group, with accounts sorted by name and
ungrouped accounts under *Other*. Group names ignore case (`work` joins `Work`). To set the order of the sections,
add `"groups": ["Work", "Personal"]` to the config; otherwise they are alphabetical.

| Type | Shows | Data source |
|---|---|---|
| `claude` (Pro/Max) | session 5 h, weekly (+ per-model weekly, extra-usage spend when enabled) | Claude Code OAuth login → `api.anthropic.com/api/oauth/usage` (same as `/usage`) |
| `codex` (ChatGPT Plus/Pro/Business) | 5 h window, weekly, credits | Codex CLI login (`$CODEX_HOME/auth.json`) → `chatgpt.com/backend-api/wham/usage`; falls back to the last rate-limit snapshot in Codex session logs |
| `copilot` | premium requests used / entitlement, monthly reset, overage | GitHub device flow → `api.github.com/copilot_internal/user` |
| `command` | whatever your command reports (budgets, quotas, …) | any command printing CreditsTracker JSON (format: `fetch_command` in `core/ctrack.py`) |

**Several subscriptions of the same vendor on one machine.** Each Claude/Codex account gets its own
config dir, so logins don't overwrite each other. To use one from a terminal (account submenu →
*Copy Terminal Command* on macOS, terminal icon on GNOME):

```sh
CLAUDE_CONFIG_DIR=~/.claude-max-work claude
CODEX_HOME=~/.codex-pro-client codex
```

The first account of each type uses the default dir (`~/.claude`, `~/.codex`), the one plain
`claude`/`codex` already use.

**Copilot client vs company:** sign each one in with the GitHub account that holds that seat.
**GitHub Enterprise with data residency (GHE.com)**: set the account's GitHub host, e.g. `mycorp.ghe.com` (in the Add
Account window, *Edit Account…*, or `--host`). Sign-in then happens at `https://mycorp.ghe.com/login/device` and usage comes
from `https://api.mycorp.ghe.com`. Finish the tenant's onboarding in the browser first (device enrolment, SSO), so the
device-code page just asks you to authorize. Changing the host signs the account out. Instead of the device flow you can use
`"token_cmd": "gh auth token --user <login>"` or `"token_env": "VAR"`.

## Ping

`ctrack ping <id>` / `--idle` / `--all`, or *Ping Now* / *Ping Idle Sessions* in the menu (▶ on GNOME). It sends one tiny prompt with tools, MCP
servers, hooks and session persistence disabled, from an empty temp dir:

- Claude: `claude -p --model haiku --effort low …`
- Codex: `codex exec -m gpt-5.1-codex-mini -c model_reasoning_effort="low" …`

Override per account:

```json
{ "id": "codex-perso", "type": "codex", "ping": { "model": "gpt-5.1-codex-mini", "effort": "low" } }
```

Set `"ping": false` to hide the button. Copilot and command accounts have no session timer, so they
have no ping.

## Config reference

`~/.config/creditstracker/config.json` is the same file on both machines (sync it with chezmoi, for example):

```json
{
  "refresh_minutes": 5,
  "menubar": "max",
  "accounts": [
    { "id": "claude-perso", "label": "Claude Pro · perso", "type": "claude" },
    { "id": "team-budget", "label": "Team budget", "type": "command",
      "command": "my-usage --json", "platforms": ["macos"], "refresh_minutes": 15 }
  ]
}
```

| Key | Meaning |
|---|---|
| `menubar` | `"max"` (the highest % across accounts) or `"<account-id>:<metric>"`, e.g. `"claude-perso:session"` |
| `group` | section the account is listed in (`"groups": [...]` at the top level sets the section order) |
| `platforms` | `["macos"]` / `["linux"]`: only show the account on that OS |
| `enabled` | `false` hides the account |
| `refresh_minutes` | per account or global; the UIs poll every minute and the core only fetches what is due |
| `config_dir` / `codex_home` | Claude / Codex config dir of the account |
| `dashboard_url` | override (or `null` to hide) the "open usage page" button |

Files: secrets (Copilot tokens) live in `~/.config/creditstracker/secrets.json` (0600), the cache in
`~/.cache/creditstracker/state.json`. Claude/Codex tokens are read where those CLIs keep them (the
macOS Keychain or `~/.claude/.credentials.json`, and `auth.json`), only to call the usage endpoints.
They are never written to the cache or printed.

## Notes

- The Claude, Codex and Copilot usage endpoints are the ones their own clients use. They are not
  public APIs and may change; if a provider breaks, the fix goes in one function in `core/ctrack.py`.
- When a login token has expired, the card keeps the last data (marked ⚠). Windows whose reset time
  has passed drop to 0 / *idle*. Using the tool once, or pinging it, refreshes the token.
