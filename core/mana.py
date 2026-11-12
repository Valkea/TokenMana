#!/usr/bin/env python3
"""mana — TokenMana core.

Fetches usage / limits / spend for several coding-assistant accounts and prints
them in one normalized JSON shape consumed by the macOS menu-bar app and the
GNOME Shell extension. Stdlib only, Python >= 3.9, macOS + Linux.

  mana status [--json] [--force] [--cached] [ACCOUNT_ID ...]
  mana ping   [--json] (--all | --idle | ACCOUNT_ID ...)
  mana auto-ping [--json] [--dismiss ACCOUNT_ID]   run the scheduled pings that are due
  mana login  [--json] ACCOUNT_ID
  mana add    TYPE [--label L] [--dir D] [--host H] [--command C]   (claude|codex|copilot|command)
  mana edit   ACCOUNT_ID [--label L] [--group G] [--host H] [--auto-ping off|5h|HH:MM,...]
  mana rename-group OLD NEW      rename a group ("" to ungroup its accounts)
  mana group-color GROUP COLOR   set a group's colour
  mana remove ACCOUNT_ID
  mana init   [--force]          write a starter config (auto-detected)
  mana paths                     print config / cache locations

Secrets (OAuth tokens) are read from where each tool already stores them and are
never printed or written to the cache.

This file is only the entry point: the code is in the `tokenmana` package next to it.
"""
import os
import sys

sys.dont_write_bytecode = True  # no __pycache__ inside the signed app bundle / extension dir
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))  # `python3 -I` leaves it out

from tokenmana.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
