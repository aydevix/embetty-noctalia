#!/usr/bin/env python3
"""Persistent PTY session daemon for the Embetty Noctalia plugin.

The daemon owns one interactive shell per named session, keeps a clean text
scrollback, and exposes a small JSON-over-Unix-socket API.  It is started on
demand by ``termctl.py`` and normally outlives the Noctalia panel, so closing
and reopening the terminal (or restarting the shell) keeps sessions alive.

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import atexit
import codecs
import collections
import errno
import fcntl
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import termios
import threading
import time
import traceback
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safety  # noqa: E402  (local module, same directory)

VERSION = "1.0.1"
MAX_REQUEST = 1 << 20
MAX_RESPONSE = 1 << 23
MAX_BUFFER_CHARS = 400_000
SAVE_INTERVAL_SECONDS = 4.0
UI_PUBLISH_INTERVAL = 0.05
UI_META_INTERVAL = 0.25
UI_MAX_CHARS_DEFAULT = 16_000
NAME_MAX_LEN = 40
SHELL_NAMES = set(safety.SHELLS) | {"csh", "tcsh", "nu", "elvish", "xonsh", "busybox"}

_log_lock = threading.Lock()


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with _log_lock:
        try:
            sys.stderr.write(f"[{stamp}] {message}\n")
            sys.stderr.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Terminal output cleaning
# ---------------------------------------------------------------------------


class AnsiFilter:
    """Remove ANSI/VT escape sequences while tolerating chunk boundaries."""

    def __init__(self) -> None:
        self.state = "normal"

    def feed(self, text: str) -> str:
        out: list[str] = []
        for ch in text:
            state = self.state
            if state == "normal":
                if ch == "\x1b":
                    self.state = "esc"
                elif ch == "\x9b":
                    self.state = "csi"
                elif ch == "\x9d":
                    self.state = "osc"
                else:
                    out.append(ch)
            elif state == "esc":
                if ch == "[":
                    self.state = "csi"
                elif ch == "]":
                    self.state = "osc"
                elif ch in "P_^":
                    self.state = "dcs"
                elif ch in "()#%":
                    self.state = "charset"
                else:
                    self.state = "normal"
            elif state == "charset":
                self.state = "normal"
            elif state == "csi":
                if "\x40" <= ch <= "\x7e":
                    self.state = "normal"
            elif state in {"osc", "dcs"}:
                if ch == "\x07":
                    self.state = "normal"
                elif ch == "\x1b":
                    self.state = "osc_esc"
            elif state == "osc_esc":
                if ch == "\\":
                    self.state = "normal"
                else:
                    self.state = "osc"
        return "".join(out)


class ScreenBuffer:
    """A tiny line-oriented text buffer with CR/backspace handling."""

    def __init__(self, max_chars: int = MAX_BUFFER_CHARS) -> None:
        self.max_chars = max_chars
        self.lines: collections.deque[str] = collections.deque()
        self.partial = ""
        self.chars = 0
        self.dirty = False
        self._pending_cr = False
        self._lock = threading.Lock()

    def feed(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            for ch in text:
                if self._pending_cr:
                    self._pending_cr = False
                    if ch == "\n":
                        self._commit_locked()
                        continue
                    self.partial = ""

                if ch == "\r":
                    self._pending_cr = True
                elif ch == "\n":
                    self._commit_locked()
                elif ch == "\b":
                    self.partial = self.partial[:-1]
                elif ch == "\t":
                    self.partial += "    "
                elif ch < " ":
                    continue
                else:
                    self.partial += ch

            self._trim_locked()
            self.dirty = True

    def _commit_locked(self) -> None:
        self.lines.append(self.partial)
        self.chars += len(self.partial) + 1
        self.partial = ""

    def _trim_locked(self) -> None:
        while self.lines and self.chars + len(self.partial) > self.max_chars:
            removed = self.lines.popleft()
            self.chars = max(0, self.chars - len(removed) - 1)
        if self.chars + len(self.partial) > self.max_chars:
            self.partial = self.partial[-self.max_chars :]

    def snapshot(self, max_chars: int | None = None) -> str:
        with self._lock:
            parts = list(self.lines)
            if self.partial:
                parts.append(self.partial)
            text = "\n".join(parts)
        if max_chars is not None and max_chars > 0 and len(text) > max_chars:
            cut = text[-max_chars:]
            newline = cut.find("\n")
            if newline >= 0:
                cut = cut[newline + 1 :]
            text = "…\n" + cut
        return text

    def clear(self) -> None:
        with self._lock:
            self.lines.clear()
            self.partial = ""
            self.chars = 0
            self._pending_cr = False
            self.dirty = True

    def load(self, text: str) -> None:
        self.feed(text)
        with self._lock:
            self.dirty = False


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def _valid_name(name: str) -> bool:
    if not isinstance(name, str):
        return False
    name = name.strip()
    if not name or len(name) > NAME_MAX_LEN:
        return False
    return not any(ch in name for ch in "/\\") and not any(
        ord(ch) < 32 for ch in name
    )


class Session:
    def __init__(
        self,
        daemon: "Daemon",
        name: str,
        sid: str,
        created: float,
        cwd: str,
        shell: str,
    ) -> None:
        self.daemon = daemon
        self.name = name
        self.id = sid
        self.created = created
        if not isinstance(cwd, str) or not cwd or not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
        self.cwd = cwd
        self.shell = shell if isinstance(shell, str) and shell else "bash"
        self.buffer = ScreenBuffer(MAX_BUFFER_CHARS)
        self.lock = threading.RLock()
        self.master: int | None = None
        self.proc: subprocess.Popen | None = None
        self.pid: int | None = None
        self.exit_code: int | None = None
        self._reader: threading.Thread | None = None
        self._loaded_log = False
        self._closing = False
        self.log_path = os.path.join(daemon.logs_dir, sid + ".log")
        self.ui_path = os.path.join(daemon.ui_dir, sid + ".json")
        self.ui_removed = False
        self.ui_dirty = True
        self._ui_revision = 0
        self._ui_text: str | None = None
        self._ui_signature: tuple | None = None
        self._ui_last_payload: str | None = None
        self._ui_last_check = 0.0
        self._ui_max_chars = 0

    # -- persistence -------------------------------------------------------

    def _load_log_locked(self) -> None:
        if self._loaded_log:
            return
        self._loaded_log = True
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.read(MAX_BUFFER_CHARS * 2)
            if data:
                self.buffer.load(data)
                self.buffer.dirty = False
        except FileNotFoundError:
            pass
        except Exception as exc:
            log(f"could not restore {self.name!r}: {exc}")

    def save(self, force: bool = False) -> None:
        # A daemon restart restores metadata first and only reads the output log
        # lazily.  Do not overwrite that log with an empty buffer when the
        # daemon shuts down again before the session was ever touched.
        with self.lock:
            if not self._loaded_log:
                self._load_log_locked()
        if not force and not self.buffer.dirty:
            return
        text = self.buffer.snapshot(MAX_BUFFER_CHARS)
        tmp = self.log_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.log_path)
            self.buffer.dirty = False
        except Exception as exc:
            log(f"could not save {self.name!r}: {exc}")

    def ensure_loaded(self) -> None:
        with self.lock:
            self._load_log_locked()

    def delete_log(self) -> None:
        for path in (self.log_path, self.log_path + ".tmp"):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except Exception as exc:
                log(f"could not remove {path}: {exc}")

    # -- UI snapshots ------------------------------------------------------

    def mark_ui_dirty(self) -> None:
        """Ask the UI writer to publish this session on its next tick."""
        with self.lock:
            self.ui_dirty = True
            self._ui_revision += 1

    def publish_ui(self, max_chars: int) -> None:
        """Atomically publish the current screen for the Luau panel.

        The panel polls one small JSON file per selected session.  Doing the
        formatting here, in the daemon, avoids starting a Python process for
        every visible frame.  The file is replaced atomically so a concurrent
        reader only ever sees a complete snapshot.
        """
        with self.lock:
            if self.ui_removed:
                return
            revision = self._ui_revision
            self._load_log_locked()
            info = self.info()
            signature = (
                info["name"],
                info["cwd"],
                info["state"],
                info["alive"],
                info["running"],
                info["running_command"],
                info["exit_code"],
            )
            if (
                not self.ui_dirty
                and signature == self._ui_signature
                and max_chars == self._ui_max_chars
            ):
                return

            if self.ui_dirty or self._ui_text is None or max_chars != self._ui_max_chars:
                self._ui_text = self.buffer.snapshot(max_chars)
                self._ui_max_chars = max_chars
            self._ui_signature = signature

            payload = {"ok": True}
            payload.update(info)
            payload["text"] = self._ui_text
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if encoded == self._ui_last_payload:
                if self._ui_revision == revision:
                    self.ui_dirty = False
                return

            tmp = self.ui_path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(encoded)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.ui_path)
            except Exception as exc:
                log(f"could not publish UI snapshot for {self.name!r}: {exc}")
                return
            self._ui_last_payload = encoded
            if self._ui_revision == revision:
                self.ui_dirty = False

    def delete_ui(self) -> None:
        for path in (self.ui_path, self.ui_path + ".tmp"):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except Exception as exc:
                log(f"could not remove {path}: {exc}")

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            self._load_log_locked()
            self._spawn_locked()

    def _spawn_locked(self) -> None:
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None

        shell = self.shell
        resolved = shutil.which(shell)
        if not resolved:
            resolved = shutil.which("bash") or "/bin/bash"
        shell_name = os.path.basename(resolved)
        if shell_name == "bash" and self.daemon.rc_path:
            args = [resolved, "--noprofile", "--rcfile", self.daemon.rc_path, "-i"]
        else:
            args = [resolved, "-i"]

        cwd = self.cwd if os.path.isdir(self.cwd) else os.path.expanduser("~")
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["SHELL"] = resolved
        env["NOCTALIA_TERMINAL"] = "1"
        env.pop("PROMPT_COMMAND", None)

        master, slave = pty.openpty()
        try:
            fcntl.ioctl(
                slave,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", 48, 120, 0, 0),
            )
            proc = subprocess.Popen(
                args,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=cwd,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        except Exception:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)

        self.master = master
        self.proc = proc
        self.pid = proc.pid
        self.exit_code = None
        self._closing = False

        self._reader = threading.Thread(
            target=self._read_loop,
            args=(master, proc),
            name=f"term-read-{self.id}",
            daemon=True,
        )
        self._reader.start()
        self.mark_ui_dirty()
        log(f"started session {self.name!r} (pid {proc.pid}, shell {resolved})")

    def _read_loop(self, fd: int, proc: subprocess.Popen) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        filt = AnsiFilter()
        try:
            while True:
                try:
                    ready, _, _ = select.select([fd], [], [], 0.5)
                except (OSError, ValueError):
                    break
                if not ready:
                    if proc.poll() is not None:
                        # One last non-blocking read may still have data.
                        try:
                            data = os.read(fd, 65536)
                        except OSError:
                            data = b""
                        if not data:
                            break
                    else:
                        continue
                try:
                    data = os.read(fd, 65536)
                except OSError as exc:
                    if exc.errno in (errno.EIO, errno.EBADF, errno.EAGAIN):
                        break
                    log(f"read error on {self.name!r}: {exc}")
                    break
                if not data:
                    break
                text = decoder.decode(data)
                if text:
                    cleaned = filt.feed(text)
                    if cleaned:
                        self.buffer.feed(cleaned)
                        self.mark_ui_dirty()
        except Exception:
            log(f"reader crashed for {self.name!r}:\n{traceback.format_exc()}")
        finally:
            try:
                remaining = decoder.decode(b"", final=True)
                if remaining:
                    cleaned = filt.feed(remaining)
                    if cleaned:
                        self.buffer.feed(cleaned)
                        self.mark_ui_dirty()
            except Exception:
                pass
            try:
                code = proc.wait(timeout=2)
            except Exception:
                code = None
            with self.lock:
                if self.proc is proc:
                    self.exit_code = code
                    if not self._closing:
                        if code is None:
                            self.buffer.feed("\n[shell exited]\n")
                        else:
                            self.buffer.feed(f"\n[shell exited with code {code}]\n")
                        self.mark_ui_dirty()
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    if self.master == fd:
                        self.master = None
                    self.proc = None if not self._closing else self.proc
            log(f"session {self.name!r} shell exited (code {code})")

    def close(self) -> None:
        with self.lock:
            self._closing = True
            proc = self.proc
            master = self.master
            self.proc = None
            self.master = None
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            except Exception as exc:
                log(f"could not terminate {self.name!r}: {exc}")
            try:
                proc.wait(timeout=0.8)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
        self.save(force=True)

    # -- interaction -------------------------------------------------------

    def _refresh_cwd(self) -> str:
        if self.pid is not None:
            try:
                path = os.readlink(f"/proc/{self.pid}/cwd")
                if os.path.isdir(path):
                    self.cwd = path
            except OSError:
                pass
        return self.cwd

    def _foreground(self) -> tuple[bool, str | None]:
        if self.master is None or self.pid is None:
            return False, None
        try:
            fg = os.tcgetpgrp(self.master)
        except OSError:
            return False, None
        if fg <= 0 or fg == self.pid:
            return False, None
        command = None
        try:
            with open(f"/proc/{fg}/comm", "r", encoding="utf-8") as fh:
                command = fh.read().strip() or None
        except OSError:
            pass
        return True, command

    def state(self) -> str:
        if self.proc is None:
            return "exited" if self.exit_code is not None else "sleeping"
        if self.proc.poll() is not None:
            return "exited"
        running, _ = self._foreground()
        return "running" if running else "idle"

    def info(self) -> dict:
        running, command = self._foreground()
        return {
            "name": self.name,
            "id": self.id,
            "created": self.created,
            "cwd": self._refresh_cwd(),
            "shell": self.shell,
            "state": self.state(),
            "alive": self.proc is not None and self.proc.poll() is None,
            "running": running,
            "running_command": command,
            "exit_code": self.exit_code,
        }

    def write(self, text: str, enter: bool = True) -> None:
        if "\x00" in text:
            raise ValueError("NUL byte in command")
        text = text.replace("\r\n", "\n").replace("\n", "\r")
        if enter and not text.endswith("\r"):
            text += "\r"
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._load_log_locked()
                self._spawn_locked()
            if self.master is None:
                raise RuntimeError("session has no terminal")
            os.write(self.master, text.encode("utf-8", errors="replace"))
        self.mark_ui_dirty()

    def send_key(self, key: str) -> None:
        mapping = {
            "ctrl-c": b"\x03",
            "ctrl-d": b"\x04",
            "ctrl-l": b"\x0c",
            "ctrl-z": b"\x1a",
            "tab": b"\t",
            "escape": b"\x1b",
            "enter": b"\r",
            "up": b"\x1b[A",
            "down": b"\x1b[B",
            "right": b"\x1b[C",
            "left": b"\x1b[D",
            "home": b"\x1b[H",
            "end": b"\x1b[F",
            "page-up": b"\x1b[5~",
            "page-down": b"\x1b[6~",
        }
        data = mapping.get(key)
        if data is None:
            raise ValueError(f"unknown key {key!r}")
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._load_log_locked()
                self._spawn_locked()
            if self.master is None:
                raise RuntimeError("session has no terminal")
            os.write(self.master, data)
        self.mark_ui_dirty()

    def restart(self) -> None:
        with self.lock:
            self.close_locked()
            self._load_log_locked()
            self._spawn_locked()

    def close_locked(self) -> None:
        proc = self.proc
        master = self.master
        self.proc = None
        self.master = None
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGHUP)
            except Exception:
                pass
            try:
                proc.wait(timeout=0.8)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class Daemon:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = os.path.abspath(data_dir)
        self.logs_dir = os.path.join(self.data_dir, "logs")
        self.ui_dir = os.path.join(self.data_dir, "ui")
        self.meta_path = os.path.join(self.data_dir, "sessions.json")
        self.rc_path = os.path.join(self.data_dir, "bashrc")
        self.socket_path = os.path.join(self.data_dir, "termd.sock")
        self.pid_path = os.path.join(self.data_dir, "termd.pid")
        self.sessions: dict[str, Session] = {}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self._meta_dirty = False
        self._server: socketserver.BaseServer | None = None
        self._cleaned = False
        self.ui_max_chars = UI_MAX_CHARS_DEFAULT

        os.makedirs(self.data_dir, mode=0o700, exist_ok=True)
        os.makedirs(self.logs_dir, mode=0o700, exist_ok=True)
        os.makedirs(self.ui_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
            os.chmod(self.logs_dir, 0o700)
            os.chmod(self.ui_dir, 0o700)
        except OSError:
            pass
        self._write_rc()
        self._load_meta()
        self._clean_ui_dir()
        self._maintenance = threading.Thread(
            target=self._maintenance_loop, name="term-maintenance", daemon=True
        )
        self._maintenance.start()
        self._ui_writer = threading.Thread(
            target=self._ui_loop, name="term-ui", daemon=True
        )
        self._ui_writer.start()

    def _write_rc(self) -> None:
        content = (
            "# Generated by the Embetty Noctalia plugin. Do not edit.\n"
            "if [ -f \"$HOME/.bashrc\" ]; then\n"
            "  . \"$HOME/.bashrc\"\n"
            "fi\n"
        )
        try:
            with open(self.rc_path, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.chmod(self.rc_path, 0o600)
        except Exception as exc:
            log(f"could not write shell rc: {exc}")

    # -- metadata ----------------------------------------------------------

    def _load_meta(self) -> None:
        try:
            with open(self.meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as exc:
            log(f"could not read sessions.json: {exc}")
            return
        if not isinstance(meta, dict):
            log("sessions.json is not a JSON object; ignoring it")
            return
        items = meta.get("sessions", [])
        if not isinstance(items, list):
            log("sessions.json has no session list; ignoring it")
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not _valid_name(name) or name in self.sessions:
                continue
            sid = item.get("id")
            if not isinstance(sid, str) or not re.fullmatch(r"[a-f0-9]{8,32}", sid):
                sid = uuid.uuid4().hex[:16]
            try:
                created = float(item.get("created") or time.time())
            except (TypeError, ValueError):
                created = time.time()
            cwd = item.get("cwd")
            if not isinstance(cwd, str):
                cwd = os.path.expanduser("~")
            shell = item.get("shell")
            if not isinstance(shell, str):
                shell = "bash"
            self.sessions[name] = Session(
                self,
                name.strip(),
                sid,
                created,
                cwd,
                shell,
            )
        log(f"loaded {len(self.sessions)} saved session(s)")

    def _save_meta(self) -> None:
        with self.lock:
            payload = {
                "version": 1,
                "sessions": [
                    {
                        "name": session.name,
                        "id": session.id,
                        "created": session.created,
                        "cwd": session.cwd,
                        "shell": session.shell,
                    }
                    for session in sorted(
                        self.sessions.values(), key=lambda s: s.created
                    )
                ],
            }
            tmp = self.meta_path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                    fh.write("\n")
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.meta_path)
                self._meta_dirty = False
            except Exception as exc:
                log(f"could not save sessions.json: {exc}")

    def _clean_ui_dir(self) -> None:
        keep = {session.id + ".json" for session in self.sessions.values()}
        try:
            names = os.listdir(self.ui_dir)
        except OSError:
            return
        for name in names:
            if name.endswith(".json") and name in keep:
                continue
            if name.endswith(".json") or name.endswith(".json.tmp"):
                try:
                    os.unlink(os.path.join(self.ui_dir, name))
                except OSError:
                    pass

    def _ui_loop(self) -> None:
        while not self.stop_event.wait(UI_PUBLISH_INTERVAL):
            try:
                with self.lock:
                    sessions = list(self.sessions.values())
                now = time.monotonic()
                for session in sessions:
                    if session.ui_removed:
                        continue
                    if session.ui_dirty or now - session._ui_last_check >= UI_META_INTERVAL:
                        session._ui_last_check = now
                        session.publish_ui(self.ui_max_chars)
            except Exception:
                log(f"UI writer error:\n{traceback.format_exc()}")

    def set_ui_max_chars(self, value) -> dict:
        try:
            max_chars = max(1000, min(int(value), 120000))
        except (TypeError, ValueError):
            return {"ok": False, "error": "max_chars must be an integer"}
        with self.lock:
            self.ui_max_chars = max_chars
            sessions = list(self.sessions.values())
        for session in sessions:
            session.mark_ui_dirty()
        return {"ok": True, "max_chars": max_chars}

    def _maintenance_loop(self) -> None:
        while not self.stop_event.wait(SAVE_INTERVAL_SECONDS):
            try:
                with self.lock:
                    sessions = list(self.sessions.values())
                for session in sessions:
                    old_cwd = session.cwd
                    session._refresh_cwd()
                    if session.cwd != old_cwd:
                        self._meta_dirty = True
                    if session.buffer.dirty:
                        session.save()
                if self._meta_dirty:
                    self._save_meta()
            except Exception:
                log(f"maintenance error:\n{traceback.format_exc()}")

    # -- operations --------------------------------------------------------

    def _find(self, name: str) -> Session:
        session = self.sessions.get(name)
        if session is None:
            raise LookupError(f"session {name!r} does not exist")
        return session

    def _get_session(self, request: dict) -> Session:
        name = request.get("name")
        if not isinstance(name, str):
            raise ValueError("name is required")
        return self._find(name.strip())

    def _session_list(self) -> list[dict]:
        with self.lock:
            return [
                session.info()
                for session in sorted(self.sessions.values(), key=lambda s: s.created)
            ]

    def handle(self, request: dict) -> dict:
        command = request.get("cmd")
        if not isinstance(command, str):
            return {"ok": False, "error": "cmd is required"}

        if command == "ping":
            return {
                "ok": True,
                "version": VERSION,
                "pid": os.getpid(),
                "socket": self.socket_path,
            }

        if command == "list":
            if "max_chars" in request:
                configured = self.set_ui_max_chars(request.get("max_chars"))
                if not configured.get("ok"):
                    return configured
            return {"ok": True, "sessions": self._session_list()}

        if command == "ui_config":
            return self.set_ui_max_chars(request.get("max_chars"))

        if command == "create":
            return self.create(request)

        if command == "send":
            return self.send(request)

        if command == "send_key":
            session = self._get_session(request)
            key = request.get("key")
            if not isinstance(key, str):
                return {"ok": False, "error": "key is required"}
            session.send_key(key)
            return {"ok": True, "session": session.info()}

        if command == "capture":
            session = self._get_session(request)
            max_chars = request.get("max_chars", 20000)
            try:
                max_chars = max(1000, min(int(max_chars), 120000))
            except (TypeError, ValueError):
                max_chars = 20000
            session.ensure_loaded()
            info = session.info()
            info["text"] = session.buffer.snapshot(max_chars)
            return {"ok": True, **info}

        if command == "clear":
            session = self._get_session(request)
            session.buffer.clear()
            session.mark_ui_dirty()
            session.save(force=True)
            return {"ok": True, "session": session.info()}

        if command == "rename":
            return self.rename(request)

        if command == "remove":
            return self.remove(request)

        if command == "restart":
            session = self._get_session(request)
            session.restart()
            return {"ok": True, "session": session.info()}

        if command == "shutdown":
            threading.Thread(target=self._shutdown_async, daemon=True).start()
            return {"ok": True}

        return {"ok": False, "error": f"unknown command {command!r}"}

    def create(self, request: dict) -> dict:
        name = request.get("name")
        if not isinstance(name, str) or not _valid_name(name):
            return {
                "ok": False,
                "code": "invalid_name",
                "error": "session name must be 1-40 characters and contain no / or \\",
            }
        name = name.strip()
        shell = request.get("shell") or "bash"
        if not isinstance(shell, str):
            shell = "bash"
        startup = request.get("startup") or ""
        if not isinstance(startup, str):
            startup = ""
        guard = request.get("guard", True) is not False
        max_sessions = request.get("max_sessions", 16)
        try:
            max_sessions = max(1, min(int(max_sessions), 32))
        except (TypeError, ValueError):
            max_sessions = 16

        with self.lock:
            if name in self.sessions:
                return {
                    "ok": False,
                    "code": "exists",
                    "error": f"session {name!r} already exists",
                    "session": self.sessions[name].info(),
                }
            if len(self.sessions) >= max_sessions:
                return {
                    "ok": False,
                    "code": "too_many",
                    "error": f"at most {max_sessions} sessions are allowed",
                }
            if startup.strip() and guard:
                verdict = safety.check(startup, os.path.expanduser("~"))
                if not verdict["ok"]:
                    return {
                        "ok": False,
                        "blocked": True,
                        "code": verdict["code"],
                        "detail": verdict["detail"],
                        "reason": verdict["reason"],
                        "error": verdict["reason"],
                    }
            session = Session(
                self,
                name,
                uuid.uuid4().hex[:16],
                time.time(),
                os.path.expanduser("~"),
                shell,
            )
            self.sessions[name] = session
            self._meta_dirty = True

        try:
            session.start()
        except Exception as exc:
            with self.lock:
                self.sessions.pop(name, None)
            return {"ok": False, "error": f"could not start shell: {exc}"}

        if startup.strip():
            timer = threading.Timer(
                0.45, lambda: self._safe_write(session, startup)
            )
            timer.daemon = True
            timer.start()

        self._save_meta()
        return {"ok": True, "session": session.info()}

    def _safe_write(self, session: Session, text: str) -> None:
        try:
            session.write(text, enter=True)
        except Exception as exc:
            log(f"startup command failed for {session.name!r}: {exc}")

    def send(self, request: dict) -> dict:
        session = self._get_session(request)
        text = request.get("text")
        if not isinstance(text, str):
            return {"ok": False, "error": "text is required"}
        if len(text) > 100_000:
            return {"ok": False, "error": "command is too long"}
        guard = request.get("guard", True) is not False
        password = request.get("password") is True
        cwd = session._refresh_cwd()
        if password:
            running, command = session._foreground()
            if not running or not command:
                return {
                    "ok": False,
                    "code": "password_not_waiting",
                    "error": "session is not waiting for password input",
                }
            if command.lower() in SHELL_NAMES:
                return {
                    "ok": False,
                    "code": "password_shell",
                    "error": "refusing raw password input to a shell",
                }
        if guard and text.strip():
            verdict = safety.check(text, cwd)
            if not verdict["ok"]:
                return {
                    "ok": False,
                    "blocked": True,
                    "code": verdict["code"],
                    "detail": verdict["detail"],
                    "reason": verdict["reason"],
                    "error": verdict["reason"],
                }
        was_sleeping = session.state() in {"sleeping", "exited"}
        try:
            session.write(text, enter=True)
        except Exception as exc:
            return {"ok": False, "error": f"could not send command: {exc}"}
        return {
            "ok": True,
            "restarted": was_sleeping,
            "session": session.info(),
        }

    def rename(self, request: dict) -> dict:
        old = request.get("from")
        new = request.get("to")
        if not isinstance(old, str) or not isinstance(new, str):
            return {"ok": False, "error": "from and to are required"}
        old = old.strip()
        new = new.strip()
        if not _valid_name(new):
            return {
                "ok": False,
                "code": "invalid_name",
                "error": "session name must be 1-40 characters and contain no / or \\",
            }
        with self.lock:
            session = self._find(old)
            if new == old:
                return {"ok": True, "session": session.info()}
            if new in self.sessions:
                return {
                    "ok": False,
                    "code": "exists",
                    "error": f"session {new!r} already exists",
                }
            del self.sessions[old]
            session.name = new
            self.sessions[new] = session
            self._meta_dirty = True
        session.mark_ui_dirty()
        self._save_meta()
        return {"ok": True, "session": session.info()}

    def remove(self, request: dict) -> dict:
        session = self._get_session(request)
        with self.lock:
            self.sessions.pop(session.name, None)
            self._meta_dirty = True
        with session.lock:
            session.ui_removed = True
        session.close()
        session.delete_log()
        session.delete_ui()
        self._save_meta()
        return {"ok": True}

    def _shutdown_async(self) -> None:
        time.sleep(0.15)
        log("shutdown requested")
        self.stop_event.set()
        if self._server is not None:
            threading.Thread(target=self._server.shutdown, daemon=True).start()

    def cleanup(self) -> None:
        with self.lock:
            if self._cleaned:
                return
            self._cleaned = True
        self.stop_event.set()
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            try:
                session.close()
            except Exception:
                log(f"error closing {session.name!r}:\n{traceback.format_exc()}")
        self._save_meta()
        for path in (self.socket_path, self.pid_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except Exception:
                pass
        log("daemon stopped")


# ---------------------------------------------------------------------------
# Socket server
# ---------------------------------------------------------------------------


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            line = self.rfile.readline(MAX_REQUEST + 1)
            if len(line) > MAX_REQUEST:
                response = {"ok": False, "error": "request too large"}
            else:
                try:
                    request = json.loads(line.decode("utf-8"))
                except Exception as exc:
                    response = {"ok": False, "error": f"invalid JSON: {exc}"}
                else:
                    if not isinstance(request, dict):
                        response = {"ok": False, "error": "request must be an object"}
                    else:
                        daemon: Daemon = self.server.daemon_instance  # type: ignore[attr-defined]
                        try:
                            response = daemon.handle(request)
                        except LookupError as exc:
                            response = {"ok": False, "error": str(exc)}
                        except ValueError as exc:
                            response = {"ok": False, "error": str(exc)}
                        except Exception as exc:
                            log(f"request failed: {request!r}\n{traceback.format_exc()}")
                            response = {"ok": False, "error": f"internal error: {exc}"}
        except Exception as exc:
            log(f"socket handler error: {exc}")
            response = {"ok": False, "error": "socket error"}
        try:
            payload = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
            self.wfile.write(payload.encode("utf-8") + b"\n")
        except Exception:
            pass


class UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, path: str, daemon: Daemon) -> None:
        self.daemon_instance = daemon
        super().__init__(path, Handler)


def _acquire_lock(data_dir: str):
    path = os.path.join(data_dir, "termd.lock")
    fd = open(path, "a+")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return None
    os.chmod(path, 0o600)
    return fd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Embetty Noctalia session daemon")
    parser.add_argument("--data", required=True, help="plugin data directory")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)

    data_dir = os.path.abspath(os.path.expanduser(args.data))
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)
    except OSError:
        pass

    log_path = os.path.join(data_dir, "termd.log")
    try:
        if os.path.getsize(log_path) > 2 * 1024 * 1024:
            os.replace(log_path, log_path + ".1")
    except FileNotFoundError:
        pass
    except Exception:
        pass

    lock_fd = _acquire_lock(data_dir)
    if lock_fd is None:
        return 0

    daemon = Daemon(data_dir)
    socket_path = daemon.socket_path
    if len(socket_path.encode("utf-8")) > 100:
        log(f"socket path is too long: {socket_path}")
        return 1
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass
    except Exception as exc:
        log(f"could not remove stale socket: {exc}")

    server = UnixServer(socket_path, daemon)
    daemon._server = server
    try:
        os.chmod(socket_path, 0o600)
    except OSError:
        pass
    try:
        with open(daemon.pid_path, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()) + "\n")
        os.chmod(daemon.pid_path, 0o600)
    except Exception as exc:
        log(f"could not write pid file: {exc}")

    def stop(_signum, _frame):
        daemon.stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    atexit.register(daemon.cleanup)
    log(f"listening on {socket_path} (pid {os.getpid()}, version {VERSION})")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        daemon.cleanup()
        try:
            server.server_close()
        except Exception:
            pass
        try:
            lock_fd.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
