#!/usr/bin/env python3
"""Tiny client for the Embetty Noctalia session daemon.

Usage:
    termctl.py --data <plugin-data-dir> --request <request.json>

The request file contains a JSON object such as ``{"cmd": "list"}``.  The
client prints exactly one JSON line on stdout.  It starts the daemon on demand
so the Luau side never has to manage processes itself.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

VERSION = "1.0.1"
MAX_RESPONSE = 8 * 1024 * 1024


def socket_path(data_dir: str) -> str:
    return os.path.join(data_dir, "termd.sock")


def _connect(data_dir: str, timeout: float) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(socket_path(data_dir))
    return sock


def request(
    data_dir: str, payload: dict, timeout: float = 20.0, start_missing: bool = False
) -> dict:
    try:
        sock = _connect(data_dir, timeout)
    except OSError:
        if not start_missing:
            raise
        # Only retry when the connection itself failed.  Retrying after a
        # timeout could send a non-idempotent command twice.
        ensure(data_dir)
        sock = _connect(data_dir, timeout)
    try:
        sock.sendall(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            + b"\n"
        )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_RESPONSE:
                raise RuntimeError("response too large")
            if chunk.endswith(b"\n"):
                break
    finally:
        sock.close()
    raw = b"".join(chunks).strip()
    if not raw:
        raise RuntimeError("daemon returned an empty response")
    result = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(result, dict):
        raise RuntimeError("daemon returned a non-object response")
    return result


def ping(data_dir: str) -> dict | None:
    try:
        return request(data_dir, {"cmd": "ping"}, timeout=0.6)
    except Exception:
        return None


def start_daemon(data_dir: str) -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    termd = os.path.join(here, "termd.py")
    log_path = os.path.join(data_dir, "termd.log")
    log_file = open(log_path, "ab", buffering=0)
    try:
        subprocess.Popen(
            [sys.executable, termd, "--data", data_dir],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            cwd=data_dir,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_file.close()


def ensure(data_dir: str) -> dict:
    current = ping(data_dir)
    if current is not None:
        return current

    try:
        start_daemon(data_dir)
    except Exception as exc:
        raise RuntimeError(f"could not start terminal daemon: {exc}") from exc

    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        current = ping(data_dir)
        if current is not None:
            return current
        time.sleep(0.1)

    log_tail = ""
    try:
        with open(os.path.join(data_dir, "termd.log"), "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 1200))
            log_tail = fh.read().strip()
    except Exception:
        pass
    raise RuntimeError(
        "terminal daemon did not start" + (f": {log_tail}" if log_tail else "")
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    data_dir = ""
    request_path = ""
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--data" and index + 1 < len(args):
            data_dir = args[index + 1]
            index += 2
        elif arg == "--request" and index + 1 < len(args):
            request_path = args[index + 1]
            index += 2
        elif arg in {"-h", "--help"}:
            print(__doc__)
            return 0
        elif arg == "--version":
            print(VERSION)
            return 0
        else:
            index += 1

    if not data_dir or not request_path:
        print(json.dumps({"ok": False, "error": "--data and --request are required"}))
        return 0

    data_dir = os.path.abspath(os.path.expanduser(data_dir))
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)
    except OSError:
        pass

    try:
        with open(request_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict):
            raise ValueError("request must be a JSON object")
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"invalid request: {exc}"}))
        return 0
    finally:
        try:
            os.unlink(request_path)
        except OSError:
            pass

    try:
        result = request(data_dir, payload, start_missing=True)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}

    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
