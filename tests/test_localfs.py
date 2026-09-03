import tempfile
import unittest
from pathlib import Path

from lichtfeld_runpod.config import (
    AppConfig,
    ConfigError,
    LichtfeldConfig,
    RunpodConfig,
    SshConfig,
    StorageConfig,
    config_for_job,
    parse_extra_args,
    peek_runpod_image,
)
from lichtfeld_runpod.localfs import ensure_datasets_dir, is_allowed_path, list_local_dir
from lichtfeld_runpod.orchestrate import inject_and_start


class StaticFormTests(unittest.TestCase):
    def test_new_job_form_has_release_fields(self) -> None:
        html = (
            Path(__file__).resolve().parents[1]
            / "lichtfeld_runpod"
            / "static"
            / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn('id="job-start"', html)
        self.assertIn('id="job-image"', html)
        self.assertIn('id="override-lichtfeld"', html)
        self.assertIn('id="extra-args"', html)
        self.assertGreater(html.index('id="extra-args"'), html.index('id="override-lichtfeld"'))
        self.assertIn('id="pick-folder"', html)
        self.assertIn('id="pick-archive"', html)
        self.assertIn('id="pick-config"', html)
        self.assertIn('id="config-local"', html)
        self.assertIn('id="upload-as-is"', html)
        self.assertIn('id="build-start"', html)
        self.assertIn('id="build-form"', html)
        self.assertNotIn('data-view="build" disabled', html)
        js = (
            Path(__file__).resolve().parents[1]
            / "lichtfeld_runpod"
            / "static"
            / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn("abort-upload", js)
        self.assertIn('e.target.id === "job-start"', js)
        self.assertIn("open-results", js)
        self.assertIn("local_results_ready", js)
        self.assertIn('kind: "build"', js)
        self.assertIn("submitBuild", js)
        self.assertIn("form.extra_args.value", js)


class DatasetsDirTests(unittest.TestCase):
    def test_creates_datasets_folder(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = ensure_datasets_dir(root)
            self.assertTrue(path.is_dir())
            self.assertEqual(path.name, "datasets")
            again = ensure_datasets_dir(root)
            self.assertEqual(again, path)

    def test_list_allows_workdir_outside_home_sandbox_via_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            datasets = ensure_datasets_dir(root)
            (datasets / "scene.tar").write_bytes(b"x")
            home = root / "home"
            home.mkdir()
            listing = list_local_dir(datasets, home, root)
            names = {e["name"] for e in listing["entries"]}
            self.assertIn("scene.tar", names)
            archive = next(e for e in listing["entries"] if e["name"] == "scene.tar")
            self.assertTrue(archive["is_archive"])
            self.assertTrue(is_allowed_path(datasets, home, root))


class ConfigOverlayTests(unittest.TestCase):
    def _base(self) -> AppConfig:
        return AppConfig(
            job_name="t",
            progress_interval_seconds=15,
            terminate_when_done=True,
            runpod=RunpodConfig(
                api_key="rpa_test",
                gpu="NVIDIA L40S",
                gpu_count=1,
                cloud="SECURE",
                image="yaml-image",
                container_disk_gb=50,
                volume_gb=150,
                volume_mount="/workspace",
                allowed_cuda_versions=[],
            ),
            ssh=SshConfig(identity_file=Path("/tmp/id"), public_key_file=Path("/tmp/id.pub")),
            storage=StorageConfig(
                host="example.test",
                user="u",
                password="secret",
                protocol="ftp",
                ftp_port=21,
                sftp_port=22,
                dataset_archive="data.tar",
                build_archive="build.tar.gz",
                result_dir="lichtfeld-results/t",
            ),
            lichtfeld=LichtfeldConfig(
                config="",
                max_cap=300000,
                enable_sparsity=True,
                gut=True,
                headless=True,
                extra_args=[],
                iterations=None,
                strategy=None,
            ),
        )

    def test_none_overrides_clear_yaml_lichtfeld_flags(self) -> None:
        cfg = config_for_job(self._base(), max_cap=None, enable_sparsity=None, gut=None)
        self.assertIsNone(cfg.lichtfeld.max_cap)
        self.assertIsNone(cfg.lichtfeld.enable_sparsity)
        self.assertIsNone(cfg.lichtfeld.gut)

    def test_extra_args_string_overlay(self) -> None:
        cfg = config_for_job(self._base(), extra_args="--export ply")
        self.assertEqual(cfg.lichtfeld.extra_args, ["--export", "ply"])

    def test_empty_extra_args_clears_yaml(self) -> None:
        base = self._base()
        from dataclasses import replace

        base = replace(base, lichtfeld=replace(base.lichtfeld, extra_args=["--from-yaml"]))
        cfg = config_for_job(base, extra_args="")
        self.assertEqual(cfg.lichtfeld.extra_args, [])

    def test_parse_extra_args(self) -> None:
        self.assertEqual(parse_extra_args("--export ply"), ["--export", "ply"])
        self.assertEqual(parse_extra_args(["--export", "ply"]), ["--export", "ply"])
        self.assertEqual(parse_extra_args(""), [])
        self.assertEqual(parse_extra_args(None), [])
        with self.assertRaises(ConfigError):
            parse_extra_args('--export "unterminated')

    def test_image_override(self) -> None:
        cfg = config_for_job(self._base(), image="custom/image:tag")
        self.assertEqual(cfg.runpod.image, "custom/image:tag")

    def test_peek_runpod_image_reads_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "config.yaml").write_text(
                "runpod:\n  image: peeked/image:1\n",
                encoding="utf-8",
            )
            self.assertEqual(peek_runpod_image(root), "peeked/image:1")


class InjectSidecarTests(unittest.TestCase):
    def test_puts_local_config_on_pod(self) -> None:
        from lichtfeld_runpod.config import (
            AppConfig,
            LichtfeldConfig,
            RunpodConfig,
            SshConfig,
            StorageConfig,
        )

        class RecSsh:
            def __init__(self) -> None:
                self.puts: list[tuple[str, str]] = []

            def put(self, src: Path, dest: str) -> None:
                self.puts.append((str(src), dest))

            def run(self, *_a, **_k) -> None:
                return None

        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            sidecar = run_dir / "lichtfeld-config.json"
            sidecar.write_text("{}", encoding="utf-8")
            (run_dir / "netrc").write_text("x", encoding="utf-8")
            cfg = AppConfig(
                job_name="t",
                progress_interval_seconds=15,
                terminate_when_done=True,
                runpod=RunpodConfig(
                    api_key="k",
                    gpu="NVIDIA L40S",
                    gpu_count=1,
                    cloud="SECURE",
                    image="x",
                    container_disk_gb=50,
                    volume_gb=150,
                    volume_mount="/workspace",
                    allowed_cuda_versions=[],
                ),
                ssh=SshConfig(identity_file=Path("/tmp/id"), public_key_file=Path("/tmp/id.pub")),
                storage=StorageConfig(
                    host="example.test",
                    user="u",
                    password="secret",
                    protocol="ftp",
                    ftp_port=21,
                    sftp_port=22,
                    dataset_archive="data.tar",
                    build_archive="build.tar.gz",
                    result_dir="lichtfeld-results/t",
                ),
                lichtfeld=LichtfeldConfig(
                    config="",
                    max_cap=None,
                    enable_sparsity=None,
                    gut=None,
                    headless=True,
                    extra_args=[],
                    iterations=None,
                    strategy=None,
                ),
            )
            ssh = RecSsh()
            inject_and_start(ssh, cfg, run_dir, "pod1", 1, 1)
            dests = [d for _, d in ssh.puts]
            self.assertIn("/workspace/lichtfeld-config.json", dests)


if __name__ == "__main__":
    unittest.main()
