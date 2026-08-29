import tarfile
import tempfile
import unittest
from pathlib import Path

from lichtfeld_runpod.config import StorageConfig
from lichtfeld_runpod.sshutil import ssh_config_text
from lichtfeld_runpod.storage import (
    UploadAborted,
    curl_put,
    format_transfer_progress,
    is_archive_path,
    parse_curl_progress_line,
    parse_curl_size,
    staging_remote_path,
    tar_directory,
    uploaded_dataset_path,
    write_netrc,
)


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


class StagingRemotePathTests(unittest.TestCase):
    def test_appends_upload_suffix(self) -> None:
        self.assertEqual(
            staging_remote_path("lichtfeld-datasets/scene.tar"),
            "lichtfeld-datasets/scene.tar.upload",
        )
        self.assertEqual(
            staging_remote_path("lichtfeld-results/job-name/"),
            "lichtfeld-results/job-name.upload",
        )
        self.assertEqual(staging_remote_path("already.upload"), "already.upload")

    def test_curl_put_uploads_staging_then_renames(self) -> None:
        from unittest.mock import patch

        from lichtfeld_runpod.storage import curl_put

        cfg = StorageConfig(
            host="example.test",
            user="u",
            password="p",
            protocol="ftp",
            ftp_port=21,
            sftp_port=22,
            dataset_archive="d.tar",
            build_archive="b.tar.gz",
            result_dir="r",
        )
        with tempfile.TemporaryDirectory() as raw:
            local = Path(raw) / "data.tar"
            local.write_bytes(b"abc")
            netrc = Path(raw) / "netrc"
            netrc.write_text("x", encoding="utf-8")
            with (
                patch("lichtfeld_runpod.storage.ensure_remote_dir"),
                patch("lichtfeld_runpod.storage.which_tool", return_value="curl"),
                patch("lichtfeld_runpod.storage.subprocess.run") as run,
                patch("lichtfeld_runpod.storage.remote_rename") as rename,
            ):
                curl_put(cfg, local, "lichtfeld-datasets/data.tar", netrc)
            cmd = run.call_args[0][0]
            self.assertIn("ftp://example.test:21/lichtfeld-datasets/data.tar.upload", cmd)
            rename.assert_called_once_with(
                cfg,
                "lichtfeld-datasets/data.tar.upload",
                "lichtfeld-datasets/data.tar",
                netrc,
            )

    def test_curl_put_aborts_before_transfer(self) -> None:
        from unittest.mock import patch

        cfg = StorageConfig(
            host="example.test",
            user="u",
            password="p",
            protocol="ftp",
            ftp_port=21,
            sftp_port=22,
            dataset_archive="d.tar",
            build_archive="b.tar.gz",
            result_dir="r",
        )
        with tempfile.TemporaryDirectory() as raw:
            local = Path(raw) / "data.tar"
            local.write_bytes(b"abc")
            netrc = Path(raw) / "netrc"
            netrc.write_text("x", encoding="utf-8")
            with (
                patch("lichtfeld_runpod.storage.ensure_remote_dir"),
                patch("lichtfeld_runpod.storage.subprocess.run") as run,
                patch("lichtfeld_runpod.storage.remote_rename") as rename,
            ):
                with self.assertRaises(UploadAborted):
                    curl_put(cfg, local, "lichtfeld-datasets/data.tar", netrc, should_stop=lambda: True)
            run.assert_not_called()
            rename.assert_not_called()

    def test_is_archive_path(self) -> None:
        self.assertTrue(is_archive_path("scene.tar"))
        self.assertTrue(is_archive_path(Path("scene.tar.gz")))
        self.assertTrue(is_archive_path("scene.zip"))
        self.assertFalse(is_archive_path("scene"))
        self.assertFalse(is_archive_path("notes.json"))


class CurlProgressTests(unittest.TestCase):
    def test_parse_put_meter_line(self) -> None:
        line = " 45  887M    0     0   45  400M      0  6458k  0:02:20  0:01:03  0:01:17 6832k"
        expected = 930_264_494
        prog = parse_curl_progress_line(line, expected)
        self.assertIsNotNone(prog)
        assert prog is not None
        self.assertEqual(prog.uploaded, 400 * 1024 * 1024)
        self.assertEqual(prog.total, expected)
        self.assertEqual(prog.speed, "6832k")
        self.assertEqual(prog.eta, "0:01:17")

    def test_parse_ignores_header(self) -> None:
        self.assertIsNone(parse_curl_progress_line("  % Total    % Received % Xferd  Average Speed"))
        self.assertIsNone(parse_curl_progress_line("                                 Dload  Upload   Total   Spent    Left  Speed"))

    def test_parse_progress_bar(self) -> None:
        prog = parse_curl_progress_line("######## 12.5%", expected=1000)
        self.assertIsNotNone(prog)
        assert prog is not None
        self.assertEqual(prog.uploaded, 125)
        self.assertEqual(prog.total, 1000)

    def test_parse_curl_size(self) -> None:
        self.assertEqual(parse_curl_size("400M"), 400 * 1024 * 1024)
        self.assertEqual(parse_curl_size("13.7G"), int(13.7 * 1024**3))
        self.assertEqual(parse_curl_size("6321k"), 6321 * 1024)

    def test_format_matches_download_style(self) -> None:
        text = format_transfer_progress("upload dataset", 125, 1000, speed="6832k", eta="0:01:17")
        self.assertEqual(text, "upload dataset 12.5%  (125/1,000 bytes)  6832k/s  remaining=0:01:17")
        done = format_transfer_progress("upload dataset", 1000, 1000, speed="5090k", eta="--:--:--")
        self.assertEqual(done, "upload dataset 100.0%  (1,000/1,000 bytes)  5090k/s")


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


class OpenInFileManagerTests(unittest.TestCase):
    def test_windows_uses_startfile(self) -> None:
        from unittest.mock import patch

        from lichtfeld_runpod.host import open_in_file_manager

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw)
            with (
                patch("lichtfeld_runpod.host.IS_WINDOWS", True),
                patch("lichtfeld_runpod.host.os.startfile") as startfile,
            ):
                open_in_file_manager(path)
            startfile.assert_called_once()
            self.assertEqual(Path(startfile.call_args[0][0]), path.resolve())

    def test_linux_uses_xdg_open(self) -> None:
        from unittest.mock import patch

        from lichtfeld_runpod.host import open_in_file_manager

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw)
            with (
                patch("lichtfeld_runpod.host.IS_WINDOWS", False),
                patch("lichtfeld_runpod.host.sys.platform", "linux"),
                patch("lichtfeld_runpod.host.subprocess.Popen") as popen,
            ):
                open_in_file_manager(path)
            popen.assert_called_once()
            args = popen.call_args[0][0]
            self.assertEqual(args[0], "xdg-open")
            self.assertEqual(Path(args[1]), path.resolve())
            self.assertTrue(popen.call_args.kwargs.get("start_new_session"))

    def test_rejects_missing_folder(self) -> None:
        from lichtfeld_runpod.host import open_in_file_manager

        with tempfile.TemporaryDirectory() as raw:
            missing = Path(raw) / "gone"
            with self.assertRaises(FileNotFoundError):
                open_in_file_manager(missing)


if __name__ == "__main__":
    unittest.main()
