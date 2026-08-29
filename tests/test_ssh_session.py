from __future__ import annotations

import queue
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lichtfeld_runpod.orchestrate import POLL_CMD
from lichtfeld_runpod.sshutil import Ssh, session_wrap, try_parse_framed


class FramedOutputTests(unittest.TestCase):
    def test_parse_strips_markers_and_rc(self) -> None:
        begin, end = "__LF_B_ab__", "__LF_E_ab__"
        parsed = try_parse_framed(
            ["motd", begin, "hello", "world", f"{end} 0"],
            begin,
            end,
        )
        self.assertEqual(parsed, ("hello\nworld\n", 0))

    def test_parse_incomplete_without_end(self) -> None:
        begin, end = "__LF_B_ab__", "__LF_E_ab__"
        self.assertIsNone(try_parse_framed([begin, "hello"], begin, end))

    def test_wrap_has_unique_markers(self) -> None:
        script, begin, end = session_wrap("echo hi", "deadbeef")
        self.assertIn(begin, script)
        self.assertIn(end, script)
        self.assertIn("set +e", script)
        self.assertIn("echo hi", script)

    def test_poll_cmd_is_one_shot_python(self) -> None:
        self.assertIn("python3 -c", POLL_CMD)
        self.assertNotIn("<<", POLL_CMD)
        script, begin, end = session_wrap(POLL_CMD, "cafef00d")
        self.assertIn(begin, script)
        self.assertIn(end, script)


class FakeSshProc:
    instances: list[FakeSshProc] = []

    def __init__(self, cmd: list[str], **kwargs: object) -> None:
        FakeSshProc.instances.append(self)
        self.cmd = cmd
        self.stdin = self
        self.stdout = self
        self._pending = b""
        self._lines: queue.Queue[bytes] = queue.Queue()
        self.returncode: int | None = None

    def write(self, data: bytes) -> int:
        self._pending += data
        self._reply_if_complete()
        return len(data)

    def flush(self) -> None:
        self._reply_if_complete()

    def _reply_if_complete(self) -> None:
        text = self._pending.decode("utf-8", errors="replace")
        m = re.search(r"__LF_B_([0-9a-f]+)__", text)
        if not m or '"$__rc"' not in text:
            return
        token = m.group(1)
        begin = f"__LF_B_{token}__"
        end = f"__LF_E_{token}__"
        body = text.split("set +e; ", 1)[-1]
        remote = body.split("\n__rc=$?", 1)[0]
        stdout, rc = self._handle(remote)
        self._lines.put(f"{begin}\n".encode())
        for line in stdout.splitlines():
            self._lines.put((line + "\n").encode())
        self._lines.put(f"{end} {rc}\n".encode())
        self._pending = b""

    def _handle(self, remote: str) -> tuple[str, int]:
        if "SSH_OK" in remote:
            return "SSH_OK\nfakehost\n", 0
        if "failplease" in remote:
            return "nope\n", 7
        return "ok\n", 0

    def readline(self) -> bytes:
        if self.returncode is not None:
            return b""
        try:
            return self._lines.get(timeout=2)
        except queue.Empty:
            return b""

    def close(self) -> None:
        return None

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9
        self._lines.put(b"")

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class PersistentSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeSshProc.instances = []

    def test_wait_ready_and_commands_share_one_process(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cfg = Path(raw) / "ssh_config"
            cfg.write_text("Host runpod\n", encoding="utf-8")
            with patch("lichtfeld_runpod.sshutil.subprocess.Popen", FakeSshProc):
                ssh = Ssh(cfg, ssh_bin="ssh")
                self.assertTrue(ssh.wait_ready())
                ssh.run("mkdir -p /workspace")
                ssh.run("echo still-here")
                ssh.close()
        self.assertEqual(len(FakeSshProc.instances), 1)
        self.assertIn("exec bash --noprofile --norc -s", FakeSshProc.instances[0].cmd)

    def test_close_then_run_opens_a_new_process(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cfg = Path(raw) / "ssh_config"
            cfg.write_text("Host runpod\n", encoding="utf-8")
            with patch("lichtfeld_runpod.sshutil.subprocess.Popen", FakeSshProc):
                ssh = Ssh(cfg, ssh_bin="ssh")
                self.assertTrue(ssh.wait_ready())
                ssh.close()
                ssh.run("echo after-reconnect")
                ssh.close()
        self.assertEqual(len(FakeSshProc.instances), 2)


if __name__ == "__main__":
    unittest.main()
