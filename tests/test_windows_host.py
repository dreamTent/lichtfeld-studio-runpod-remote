import tarfile
import tempfile
import unittest
from pathlib import Path

from lichtfeld_runpod.config import StorageConfig
from lichtfeld_runpod.sshutil import ssh_config_text
from lichtfeld_runpod.storage import tar_directory, uploaded_dataset_path, write_netrc


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


class UploadedDatasetPathTests(unittest.TestCase):
    def test_tar_keeps_original_name_and_appends_job_id(self) -> None:
        src = Path(
            r"C:\data\1c591935-873f-4ff5-b0a0-42f231665bbe prepared no down no up with points 260829_1247.tar"
        )
        self.assertEqual(
            uploaded_dataset_path(src, "80c5f1f6c174"),
            "lichtfeld-datasets/1c591935-873f-4ff5-b0a0-42f231665bbe prepared no down no up with points 260829_1247-80c5f1f6c174.tar",
        )

    def test_folder_gets_tar_suffix(self) -> None:
        self.assertEqual(
            uploaded_dataset_path(Path("/home/you/my-scene"), "abc123def456"),
            "lichtfeld-datasets/my-scene-abc123def456.tar",
        )

    def test_tar_gz_keeps_compound_suffix(self) -> None:
        self.assertEqual(
            uploaded_dataset_path(Path("scene.tar.gz"), "id1"),
            "lichtfeld-datasets/scene-id1.tar.gz",
        )


class NetrcTests(unittest.TestCase):
    def test_quotes_non_ascii_password(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "netrc"
            cfg = StorageConfig(
                host="uxxxxxx.your-storagebox.de",
                user="uxxxxxx-sub6",
                password="abc§ßdef",
                protocol="ftp",
                ftp_port=21,
                sftp_port=22,
                dataset_archive="d.tar",
                build_archive="b.tar.gz",
                result_dir="r",
            )
            write_netrc(path, cfg)
            text = path.read_text(encoding="utf-8")
            self.assertIn('login "uxxxxxx-sub6"', text)
            self.assertIn('password "abc§ßdef"', text)
            self.assertNotIn(" password abc", text)

    def test_escapes_quotes_in_password(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "netrc"
            cfg = StorageConfig(
                host="example.test",
                user="u",
                password=r'a"b\c',
                protocol="ftp",
                ftp_port=21,
                sftp_port=22,
                dataset_archive="d.tar",
                build_archive="b.tar.gz",
                result_dir="r",
            )
            write_netrc(path, cfg)
            self.assertIn(r'password "a\"b\\c"', path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
