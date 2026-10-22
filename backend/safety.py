"""Guardrails for the Embetty Noctalia plugin.

This module rejects commands that would very likely destroy the machine or the
user's home directory.  It is intentionally conservative but *not* a sandbox:
shell variables, generated scripts, interpreters, and binaries invoked through
paths the parser does not understand can still evade it.  The goal is to stop
the classic "oops" commands typed into a terminal, not to contain a hostile
user.

The public API is a single function::

    check(command, cwd) -> {"ok": True}
                        or {"ok": False, "code": ..., "detail": ...,
                            "reason": ...}
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex

HOME = os.path.expanduser("~") or "/home"

# Directories whose contents are part of the operating system.  Deleting one
# of these (or one of their immediate children) is treated as destructive.
PROTECTED_EXACT = {
    "/",
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/home",
    "/lib",
    "/lib64",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
    "/usr",
    "/var",
    "/opt",
    "/srv",
    "/mnt",
    "/media",
    "/nix",
    "/snap",
    "/var/lib",
    "/var/db",
    "/var/spool",
    "/var/cache/pacman",
    "/usr/local",
}

SYSTEM_DIRS = (
    "bin",
    "boot",
    "dev",
    "etc",
    "lib",
    "lib64",
    "proc",
    "root",
    "run",
    "sbin",
    "sys",
    "usr",
)

WRAPPERS = {
    "command",
    "doas",
    "env",
    "exec",
    "ionice",
    "nice",
    "nohup",
    "setsid",
    "stdbuf",
    "sudo",
    "time",
    "xargs",
}

SHELLS = {"bash", "dash", "fish", "ksh", "sh", "zsh", "busybox"}

# Options that take a separate value, used when skipping a wrapper's flags.
WRAPPER_VALUE_OPTS = {
    "-u",
    "-g",
    "-p",
    "-C",
    "-h",
    "-r",
    "-t",
    "-U",
    "-T",
    "-D",
    "-R",
    "-c",
    "-n",
    "-i",
    "-s",
    "--user",
    "--group",
    "--prompt",
    "--close-from",
    "--host",
    "--role",
    "--type",
    "--other-user",
    "--command-timeout",
    "--chdir",
    "--adjustment",
}

SHELL_PREFIX_WORDS = {"then", "do", "else", "elif", "!", "{", "}", "time"}

SAFE_DEVICES = {"/dev/null", "/dev/zero", "/dev/stdout", "/dev/stderr"}
DEVICE_RE = re.compile(r"^/dev/(?!null$|stdout$|stderr$|tty$|pts/|fd/)")
PROTECTED_SYSTEM_RE = re.compile(
    r"^/(?:" + "|".join(SYSTEM_DIRS) + r")(?:/[^/]+)?$"
)
HOME_CHILD_RE = re.compile(r"^/home/[^/]+$")
REDIRECT_RE = re.compile(r"(?<![<>])>{1,2}\s*([^\s;&|<>]+)")
FORKBOMB_RE = re.compile(
    r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:[^}]*&[^}]*\}", re.S
)


def _ok() -> dict:
    return {"ok": True}


def _block(code: str, detail: str, reason: str) -> dict:
    return {"ok": False, "code": code, "detail": detail, "reason": reason}


def _resolve(target: str, cwd: str | None = None) -> tuple[str, bool]:
    """Return (normalised path or glob prefix, is_glob)."""
    cwd = cwd or HOME
    text = target.strip()
    # Strip a single layer of shell quotes that survived tokenisation.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    text = text.replace("${HOME}", HOME).replace("$HOME", HOME)
    text = text.replace("${PWD}", cwd).replace("$PWD", cwd)
    text = os.path.expanduser(text)
    is_glob = any(ch in text for ch in "*?[{")
    prefix = text
    if is_glob:
        cut = min(text.index(ch) for ch in "*?[{" if ch in text)
        prefix = text[:cut] or "/"
    if not prefix.startswith("/"):
        prefix = posixpath.join(cwd, prefix)
    normalised = posixpath.normpath(prefix or "/")
    if normalised.startswith("//"):
        normalised = "/" + normalised.lstrip("/")
    return normalised, is_glob


def _is_protected(path: str, is_glob: bool = False) -> bool:
    if path in SAFE_DEVICES:
        return False
    if path in PROTECTED_EXACT:
        return True
    if path == HOME:
        return True
    if path == "/home":
        return True
    if HOME_CHILD_RE.match(path):
        return True
    if PROTECTED_SYSTEM_RE.match(path):
        return True
    return False


def _segments(command: str) -> list[str]:
    """Split on unquoted shell command separators, keeping quotes intact."""
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote is not None:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                i += 1
                buf.append(command[i])
            elif ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch == "\\" and i + 1 < len(command):
            buf.append(ch)
            i += 1
            buf.append(command[i])
        elif ch in ";|\n":
            parts.append("".join(buf))
            buf = []
            while i + 1 < len(command) and command[i + 1] in "|&\n":
                i += 1
        elif ch == "&" and (
            (buf and buf[-1] == ">")
            or (i + 1 < len(command) and command[i + 1] == ">")
        ):
            buf.append(ch)
        elif ch == "&":
            parts.append("".join(buf))
            buf = []
            while i + 1 < len(command) and command[i + 1] in "&|":
                i += 1
        elif ch in "()":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [part for part in parts if part.strip()]


def _substitutions(command: str) -> list[str]:
    """Extract the contents of $(...) and `...` so nested commands are checked."""
    found: list[str] = []
    i = 0
    quote: str | None = None
    while i < len(command):
        ch = command[i]
        if quote is not None:
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < len(command):
            i += 2
            continue
        if ch == "$" and i + 1 < len(command) and command[i + 1] == "(":
            depth = 1
            j = i + 2
            content: list[str] = []
            while j < len(command) and depth > 0:
                if command[j] == "(":
                    depth += 1
                elif command[j] == ")":
                    depth -= 1
                if depth > 0:
                    content.append(command[j])
                j += 1
            if depth == 0:
                found.append("".join(content))
                i = j
                continue
        if ch == "`":
            j = command.find("`", i + 1)
            if j != -1:
                found.append(command[i + 1 : j])
                i = j + 1
                continue
        i += 1
    return found


def _tokens(segment: str) -> list[str] | None:
    try:
        return shlex.split(segment, comments=False, posix=True)
    except ValueError:
        return None


def _strip_assignments(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens) and re.match(
        r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index]
    ):
        index += 1
    return tokens[index:]


def _strip_prefix_words(tokens: list[str]) -> list[str]:
    while tokens and tokens[0] in SHELL_PREFIX_WORDS:
        tokens = tokens[1:]
    return tokens


def _wrapper_rest(tokens: list[str]) -> list[str]:
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if token.startswith("-"):
            if token in WRAPPER_VALUE_OPTS:
                index += 2
            else:
                index += 1
            continue
        break
    return tokens[index:]


def _check_rm(args: list[str], cwd: str) -> dict | None:
    recursive = False
    force = False
    no_preserve = False
    targets: list[str] = []
    options_done = False
    for token in args:
        if not options_done and token == "--":
            options_done = True
            continue
        if not options_done and token.startswith("--"):
            if token == "--recursive":
                recursive = True
            elif token == "--force":
                force = True
            elif token == "--no-preserve-root":
                no_preserve = True
            continue
        if not options_done and token.startswith("-") and token != "-":
            flags = token[1:]
            if "r" in flags or "R" in flags:
                recursive = True
            if "f" in flags:
                force = True
            continue
        targets.append(token)

    if no_preserve:
        return _block(
            "rm_no_preserve",
            " ".join(args),
            "rm --no-preserve-root is never allowed",
        )

    for target in targets:
        resolved, is_glob = _resolve(target, cwd)
        if _is_protected(resolved, is_glob):
            return _block(
                "rm_protected",
                target,
                "refusing to delete a protected system or home path",
            )
    return None


def _check_mv(args: list[str], cwd: str) -> dict | None:
    options_done = False
    operands: list[str] = []
    for token in args:
        if not options_done and token == "--":
            options_done = True
            continue
        if not options_done and token.startswith("-") and token != "-":
            continue
        operands.append(token)
    if len(operands) < 2:
        return None
    for source in operands[:-1]:
        resolved, is_glob = _resolve(source, cwd)
        if _is_protected(resolved, is_glob):
            return _block(
                "mv_protected",
                source,
                "refusing to move a protected system or home path",
            )
    return None


def _check_chmod_like(args: list[str], cwd: str) -> dict | None:
    recursive = False
    operands: list[str] = []
    options_done = False
    for token in args:
        if not options_done and token == "--":
            options_done = True
            continue
        if not options_done and token.startswith("--recursive"):
            recursive = True
            continue
        if not options_done and token.startswith("-") and token != "-":
            if "R" in token or "r" in token:
                recursive = True
            continue
        operands.append(token)
    if not recursive:
        return None
    for target in operands[1:]:
        resolved, is_glob = _resolve(target, cwd)
        if _is_protected(resolved, is_glob):
            return _block(
                "chmod_protected",
                target,
                "refusing a recursive permission change on a protected path",
            )
    return None


def _check_dd(args: list[str], cwd: str) -> dict | None:
    for token in args:
        if not token.startswith("of="):
            continue
        target = token[3:]
        resolved, _ = _resolve(target, cwd)
        if DEVICE_RE.match(resolved) or _is_protected(resolved):
            return _block(
                "dd_device",
                target,
                "refusing to write a raw disk or protected file with dd",
            )
    return None


def _check_mkfs(args: list[str], cwd: str, command: str) -> dict | None:
    if command in {"wipefs", "blkdiscard"}:
        for token in args:
            if token.startswith("/dev/"):
                return _block(
                    "wipe_device",
                    token,
                    "refusing to wipe a block device",
                )
        return None

    if command in {"sgdisk", "parted"}:
        destructive = command == "sgdisk" and any(
            token in {"--zap", "--zap-all", "-Z", "-z"} for token in args
        )
        if command == "parted" and any(
            token in {"mklabel", "mkpart", "rm", "resizepart"} for token in args
        ):
            destructive = True
        if destructive:
            target = next((token for token in args if token.startswith("/dev/")), "")
            return _block(
                "wipe_device",
                target,
                "refusing to rewrite a partition table",
            )
        return None

    # mkfs / mkfs.ext4 / mke2fs / mkswap
    for token in args:
        if token.startswith("/dev/"):
            return _block(
                "format_device",
                token,
                "refusing to format a block device",
            )
    return None


def _check_shred_truncate(args: list[str], cwd: str) -> dict | None:
    for token in args:
        if token.startswith("-"):
            continue
        resolved, _ = _resolve(token, cwd)
        if DEVICE_RE.match(resolved) or _is_protected(resolved):
            return _block(
                "destroy_protected",
                token,
                "refusing to overwrite a raw disk or protected path",
            )
    return None


def _check_find(args: list[str], cwd: str) -> dict | None:
    if "-delete" not in args and "-exec" not in args:
        return None
    paths: list[str] = []
    for token in args:
        if token.startswith("-") or token in {"!", "(", ")"}:
            break
        paths.append(token)
    if not paths:
        paths = ["."]
    for path in paths:
        resolved, is_glob = _resolve(path, cwd)
        if _is_protected(resolved, is_glob):
            return _block(
                "find_protected",
                path,
                "refusing to delete or execute against a protected path",
            )
    return None


def _check_redirect(segment: str, cwd: str) -> dict | None:
    for match in REDIRECT_RE.finditer(segment):
        target = match.group(1)
        resolved, _ = _resolve(target, cwd)
        if DEVICE_RE.match(resolved):
            return _block(
                "write_device",
                target,
                "refusing to write directly to a raw disk device",
            )
        if _is_protected(resolved):
            return _block(
                "write_protected",
                target,
                "refusing to overwrite a protected system path",
            )
    return None


def _check_tokens(tokens: list[str], cwd: str) -> dict | None:
    tokens = _strip_assignments(tokens)
    tokens = _strip_prefix_words(tokens)
    if not tokens:
        return None

    first = os.path.basename(tokens[0])
    args = tokens[1:]

    if first == "busybox":
        if args and not args[0].startswith("-"):
            return _check_tokens(args, cwd)
        return None

    if first in SHELLS:
        for index, token in enumerate(args):
            if token.startswith("-") and "c" in token:
                if index + 1 < len(args):
                    return _check_command(" ".join(args[index + 1 :]), cwd)
                return None
        return None

    if first == "eval":
        return _check_command(" ".join(args), cwd)

    if first == "trap":
        for arg in args:
            if arg.startswith("-"):
                continue
            result = _check_command(arg, cwd)
            if result is not None:
                return result
        return None

    if first in WRAPPERS:
        if first == "command" and args and args[0] in {"-v", "-V"}:
            return None
        rest = _wrapper_rest(tokens)
        if not rest:
            return None
        return _check_tokens(rest, cwd)

    if first == "rm":
        return _check_rm(args, cwd)
    if first == "mv":
        return _check_mv(args, cwd)
    if first in {"chmod", "chown"}:
        return _check_chmod_like(args, cwd)
    if first == "dd":
        return _check_dd(args, cwd)
    if first.startswith("mkfs") or first in {
        "mke2fs",
        "mkswap",
        "wipefs",
        "blkdiscard",
        "sgdisk",
        "parted",
    }:
        return _check_mkfs(args, cwd, first)
    if first in {"shred", "truncate"}:
        return _check_shred_truncate(args, cwd)
    if first == "find":
        return _check_find(args, cwd)
    return None


def _check_command(command: str, cwd: str) -> dict | None:
    command = command.strip()
    if not command:
        return None

    if FORKBOMB_RE.search(command):
        return _block(
            "forkbomb",
            command,
            "refusing a fork bomb",
        )

    for substitution in _substitutions(command):
        result = _check_command(substitution, cwd)
        if result is not None:
            return result

    for segment in _segments(command):
        if segment.lstrip().startswith("#"):
            continue
        tokens = _tokens(segment)
        if tokens is not None:
            result = _check_tokens(tokens, cwd)
            if result is not None:
                return result
        result = _check_redirect(segment, cwd)
        if result is not None:
            return result
    return None


def check(command: str, cwd: str | None = None) -> dict:
    """Check one command line.  Returns a dict with ``ok`` and, on rejection,
    ``code``, ``detail`` and ``reason``."""
    if not isinstance(command, str):
        return _block("invalid", "", "command must be a string")
    if "\x00" in command:
        return _block("invalid", "", "NUL bytes are not allowed")
    if not command.strip():
        return _ok()
    result = _check_command(command, cwd or HOME)
    return result if result is not None else _ok()
