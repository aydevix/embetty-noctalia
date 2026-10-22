#!/usr/bin/env python3
"""Tiny change notifier for the Embetty Noctalia panel.

The PTY daemon publishes an atomic JSON snapshot for each session in
``ui/<id>.json``.  Noctalia panels only get an ``update()`` tick once per
second, which is far too slow for a terminal.  This helper is started through
``noctalia.runStream`` and prints one short line whenever the selected
session's snapshot changes.  The Luau callback then reads the snapshot and
renders it.  Output is only a session id (or ``hb`` heartbeat), so a large
scrollback never has to pass through a command-output line limit.
"""

from __future__ import annotations

import argparse
import os
import re
import time

SESSION_RE = re.compile(r"^[0-9a-f]{8,64}$")
POLL_SECONDS = 0.02
RENDER_SECONDS = 0.10
HEARTBEAT_SECONDS = 1.0


def read_control(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read(128).strip()
    except OSError:
        return ""
    if value == "-" or not value:
        return ""
    return value if SESSION_RE.match(value) else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--control", required=True)
    args = parser.parse_args(argv)

    ui_dir = os.path.join(args.data, "ui")
    session = ""
    snapshot_path = ""
    last_signature: tuple[int, int] | None = None
    last_emit = 0.0
    last_heartbeat = time.monotonic()
    pending = False

    while True:
        now = time.monotonic()
        selected = read_control(args.control)
        if selected != session:
            session = selected
            snapshot_path = os.path.join(ui_dir, session + ".json") if session else ""
            last_signature = None
            pending = False
            if session:
                # Tell the panel immediately; it also reads the file itself.
                print(session, flush=True)
                last_emit = now
                last_heartbeat = now

        if not session:
            time.sleep(0.1)
            continue

        try:
            stat = os.stat(snapshot_path)
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None

        if signature is not None and signature != last_signature:
            last_signature = signature
            pending = True

        # Coalesce bursts to at most 10 notifications per second, but always
        # emit the final change so the prompt does not wait for the heartbeat.
        if pending and now - last_emit >= RENDER_SECONDS:
            print(session, flush=True)
            last_emit = now
            last_heartbeat = now
            pending = False
        elif not pending and now - last_heartbeat >= HEARTBEAT_SECONDS:
            print("hb", flush=True)
            last_heartbeat = now

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BrokenPipeError, KeyboardInterrupt):
        raise SystemExit(0)
