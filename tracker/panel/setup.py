"""`tracker panel setup|status`: create the owner's TOTP secret and check the panel.

The secret is shown once (QR code plus text) in *your* terminal and saved to `.env` only
after you prove your authenticator app produces matching codes, so you can't lock yourself
out. It is never logged and never sent anywhere.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

from tracker.panel.auth import TotpVerifier, new_totp_secret, provisioning_uri
from tracker.settings import ENV_FILE, Settings

ENV_KEY = "PANEL_TOTP_SECRET"


def set_env_value(path: Path, key: str, value: str) -> None:
    """Replace `key=...` in a .env file (or append it), keeping everything else as is."""
    raw = path.read_bytes().decode("utf-8") if path.exists() else ""
    newline = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.splitlines()
    entry = f"{key}={value}"
    for index, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[index] = entry
            break
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [
            "# Owner panel login (authenticator app). Rotate: tracker panel setup --rotate",
            entry,
        ]
    path.write_bytes((newline.join(lines) + newline).encode("utf-8"))
    if os.name == "posix":
        path.chmod(0o600)


def _show_qr(uri: str) -> None:
    try:
        import segno

        segno.make(uri, error="m").terminal(compact=True)
    except Exception:  # A terminal that can't draw it still has the text key below.
        print("(Couldn't draw a QR code here; enter the key manually.)")


def _confirm(secret: str) -> bool:
    verifier = TotpVerifier(secret)
    for attempt in range(3):
        try:
            code = input("Enter the 6-digit code your app shows now: ")
        except EOFError:
            return False
        if verifier.verify(code):
            return True
        print("That code didn't match." + (" Try the next one." if attempt < 2 else ""))
    return False


async def cmd_panel_setup(settings: Settings, args: argparse.Namespace) -> int:
    env_path: Path = args.env_file or ENV_FILE
    if settings.panel.totp_secret and not args.rotate:
        print(
            "The owner panel is already set up. To replace the authenticator secret "
            "(e.g. a lost phone), run: tracker panel setup --rotate"
        )
        return 0
    if not sys.stdin.isatty() and not args.yes:
        print("Run this in an interactive terminal so you can scan the code and confirm it.")
        return 2

    secret = new_totp_secret()
    uri = provisioning_uri(secret)
    print("\nScan this with your authenticator app (Google Authenticator, 1Password, etc.):\n")
    _show_qr(uri)
    grouped = " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))
    print(f"\nOr add it manually. Account: FlipScout (owner)   Key: {grouped}\n")
    print("Keep this key private: anyone with it can log in to your panel.\n")

    if not args.yes and not _confirm(secret):
        print("Not saved: the codes didn't match. Nothing changed; run setup again.")
        return 1
    set_env_value(env_path, ENV_KEY, secret)
    print(f"\nSaved to {env_path} ({ENV_KEY}).")
    print("Restart `tracker run`, then open the panel:")
    print(f"  http://{settings.panel.host}:{settings.panel.port}")
    if args.rotate:
        print("The old key stops working when the tracker restarts.")
    return 0


async def cmd_panel_status(settings: Settings, _args: argparse.Namespace) -> int:
    panel = settings.panel
    url = f"http://{panel.host}:{panel.port}"
    print(f"Owner panel: {'enabled' if panel.enabled else 'disabled (PANEL_ENABLED=false)'}")
    print(f"  Login secret: {'set' if panel.totp_secret else 'missing: run `tracker panel setup`'}")
    print(f"  Address: {url} (localhost only)")
    print(f"  Extra hosts: {', '.join(panel.allowed_hosts) or 'none'}")
    print(f"  Tailscale logins allowed: {len(panel.tailscale_logins) or 'any on your tailnet'}")
    family = socket.AF_INET6 if ":" in panel.host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        up = probe.connect_ex((panel.host, panel.port)) == 0
    print(f"  Listening now: {'yes' if up else 'no (is `tracker run` running?)'}")
    return 0


def add_panel_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    panel = commands.add_parser("panel", help="owner control panel").add_subparsers(
        dest="panel_command", required=True
    )
    setup = panel.add_parser("setup", help="create the owner login (authenticator app)")
    setup.add_argument("--rotate", action="store_true", help="replace an existing secret")
    setup.add_argument("--yes", action="store_true", help="save without confirming a code")
    setup.add_argument("--env-file", type=Path, help=argparse.SUPPRESS)
    setup.set_defaults(handler=cmd_panel_setup)
    status = panel.add_parser("status", help="show panel configuration (never the secret)")
    status.set_defaults(handler=cmd_panel_status)
