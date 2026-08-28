import tarfile
import tempfile
import unittest
from pathlib import Path

from lichtfeld_runpod.sshutil import ssh_config_text
from lichtfeld_runpod.storage import tar_directory


class SshConfigTests(unittest.TestCase):
    def test_unix_mux_uses_tmp_socket(self) -> None:
        text = ssh_config_text(
            "1.2.3.4",
            22,
            Path("/home/me/.ssh/runpod_ed25519"),
            Path("/tmp/known_hosts"),
            multiplex=True,
        )
        self.assertIn("ControlPath /tmp/lf-ssh-%C", text)
        self.assertNotIn("/dev/null", text)
        self.assertIn("UserKnownHostsFile", text)
        self.assertIn("IdentityFile", text)
        self.assertIn("BatchMode yes", text)
        self.assertIn("PasswordAuthentication no", text)

    def test_windows_skips_mux_and_dev_null(self) -> None:
        text = ssh_config_text(
            "1.2.3.4",
            22,
            Path("C:/Users/me/.ssh/runpod_ed25519"),
            Path("C:/Users/me/.run/known_hosts"),
            multiplex=False,
        )
        self.assertNotIn("ControlMaster", text)
        self.assertNotIn("ControlPath", text)
        self.assertNotIn("/dev/null", text)
        self.assertIn("UserKnownHostsFile", text)
        self.assertIn("BatchMode yes", text)


class TarDirectoryTests(unittest.TestCase):
    def test_packs_directory_without_system_tar(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            src = root / "scene"
            src.mkdir()
            (src / "hello.txt").write_text("hi", encoding="utf-8")
            dest = root / "scene.tar"
            tar_directory(src, dest)
            self.assertTrue(dest.is_file())
            with tarfile.open(dest) as tf:
                names = tf.getnames()
            self.assertTrue(any(n.endswith("hello.txt") for n in names))
            self.assertTrue(any(n == "scene" or n.startswith("scene/") for n in names))


if __name__ == "__main__":
    unittest.main()
