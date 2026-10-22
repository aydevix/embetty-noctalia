"""Safety-guard unit tests.

These tests only call ``safety.check()``; they never execute the commands they
describe.  Run with ``python3 -m unittest discover -s tests -v``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import safety  # noqa: E402


class BlockedCommands(unittest.TestCase):
    def assert_blocked(self, command: str, cwd: str = "/home/tester") -> None:
        result = safety.check(command, cwd)
        self.assertFalse(result["ok"], f"expected block: {command!r}")
        self.assertTrue(result.get("code"), f"missing code: {command!r}")
        self.assertTrue(result.get("reason"), f"missing reason: {command!r}")

    def test_root_removal(self) -> None:
        for command in (
            "sudo rm -rf /",
            "rm -rf /",
            "rm -rf //",
            "rm -rf /.",
            "rm -rf /usr",
            "rm -rf /etc",
            "rm -rf /home/tester",
            "rm -rf ~",
            "rm -rf $HOME",
            "rm -rf ${HOME}",
            "rm -rf /{bin,etc}",
            "rm --no-preserve-root -rf /",
            "command rm -rf /",
            "env rm -rf /",
            "busybox rm -rf /",
        ):
            with self.subTest(command=command):
                self.assert_blocked(command)

    def test_shell_indirection(self) -> None:
        for command in (
            "bash -c 'rm -rf /'",
            "sh -c \"rm -rf /\"",
            "eval 'rm -rf /'",
            "echo ok; rm -rf /",
            "echo $(rm -rf /)",
            "echo `rm -rf /`",
            "printf x | xargs rm -rf /",
        ):
            with self.subTest(command=command):
                self.assert_blocked(command)

    def test_other_destructive_tools(self) -> None:
        for command in (
            "mv / /tmp",
            "mv /etc/passwd /tmp",
            "chmod -R 000 /",
            "chown -R nobody /usr",
            "dd if=/dev/zero of=/dev/sda",
            "dd if=/dev/zero of=/etc/passwd",
            "mkfs.ext4 /dev/sda",
            "mkswap /dev/sda",
            "wipefs -a /dev/sda",
            "blkdiscard /dev/sda",
            "sgdisk --zap-all /dev/sda",
            "parted /dev/sda mklabel gpt",
            "shred /dev/sda",
            "truncate -s 0 /etc/passwd",
            "find / -delete",
            "find /etc -exec rm -rf {} +",
            "echo x > /dev/sda",
            "echo x >> /etc/passwd",
            ":(){ :|:& };:",
        ):
            with self.subTest(command=command):
                self.assert_blocked(command)


class AllowedCommands(unittest.TestCase):
    def assert_allowed(self, command: str, cwd: str = "/tmp/opencode") -> None:
        result = safety.check(command, cwd)
        self.assertTrue(result["ok"], f"unexpected block: {command!r}: {result}")

    def test_normal_commands(self) -> None:
        for command in (
            "echo hello",
            "ls -la",
            "pwd",
            "git status",
            "sudo apt update",
            "echo hi | grep hi",
            "FOO=1 echo hi",
            "cd /tmp",
            "rm -rf ./build",
            "rm -rf /tmp/opencode/definitely-not-here",
            "mkdir -p build && cd build",
            "echo x > /dev/null",
            "echo x 2>/dev/null",
            "dd if=/dev/zero of=/dev/null bs=1 count=1",
            "find . -name '*.pyc' -delete",
        ):
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_empty_command(self) -> None:
        self.assertTrue(safety.check("   ", "/tmp/opencode")["ok"])


if __name__ == "__main__":
    unittest.main()
