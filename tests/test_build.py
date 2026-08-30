import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lichtfeld_runpod.buildspec import (
    DEFAULT_GIT_REF,
    cuda_arch_for_gpu,
    default_archive_name,
    default_build_folder,
    gpu_slug,
    normalize_cuda_arch,
    normalize_git_ref,
    normalize_repo_url,
    version_slug,
)
from lichtfeld_runpod.config import (
    AppConfig,
    LichtfeldConfig,
    RunpodConfig,
    SshConfig,
    StorageConfig,
)
from lichtfeld_runpod.jobs import Job
from lichtfeld_runpod.manager import JobManager
from lichtfeld_runpod.orchestrate import format_progress, inject_and_start
from lichtfeld_runpod.remote_job import (
    PACKAGED_BUILD_SCRIPT,
    render_build_env,
    render_build_script,
    resolve_build_script,
)


def _cfg(*, terminate: bool = True) -> AppConfig:
    return AppConfig(
        job_name="b",
        progress_interval_seconds=15,
        terminate_when_done=terminate,
        runpod=RunpodConfig(
            api_key="rpa_test",
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
            dataset_archive="",
            build_archive="",
            result_dir="lichtfeld-builds/lichtfeld-0.5.3-l40s-sm89-260829",
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


class BuildSpecTests(unittest.TestCase):
    def test_l40s_is_sm89(self) -> None:
        self.assertEqual(cuda_arch_for_gpu("NVIDIA L40S"), "89")
        self.assertEqual(gpu_slug("NVIDIA L40S"), "l40s")
        self.assertEqual(version_slug("v0.5.3"), "0.5.3")

    def test_default_paths_match_existing_layout(self) -> None:
        from datetime import datetime

        folder = default_build_folder("v0.5.3", "NVIDIA L40S", "89", when=datetime(2026, 8, 28))
        name = default_archive_name("v0.5.3", "NVIDIA L40S", "89")
        self.assertEqual(folder, "lichtfeld-builds/lichtfeld-0.5.3-l40s-sm89-260828")
        self.assertEqual(name, "lichtfeld-0.5.3-l40s-sm89.tar.gz")

    def test_rejects_bad_ref_and_repo(self) -> None:
        with self.assertRaises(ValueError):
            normalize_git_ref("v0.5.3; rm -rf /")
        with self.assertRaises(ValueError):
            normalize_repo_url("http://evil.example/x.git")
        self.assertEqual(normalize_git_ref(""), DEFAULT_GIT_REF)
        self.assertEqual(normalize_cuda_arch("", "NVIDIA A40"), "86")


class BuildScriptTests(unittest.TestCase):
    def test_compile_flags_and_self_delete(self) -> None:
        env = render_build_env(
            _cfg(),
            pod_id="podabc",
            git_ref="v0.5.3",
            cuda_arch="89",
            repo_url="https://github.com/MrNeRF/LichtFeld-Studio.git",
            archive_name="lichtfeld-0.5.3-l40s-sm89.tar.gz",
        )
        script = PACKAGED_BUILD_SCRIPT.read_text(encoding="utf-8")
        text = render_build_script(
            _cfg(),
            pod_id="podabc",
            git_ref="v0.5.3",
            cuda_arch="89",
            repo_url="https://github.com/MrNeRF/LichtFeld-Studio.git",
            archive_name="lichtfeld-0.5.3-l40s-sm89.tar.gz",
        )
        self.assertIn("CMAKE_CUDA_ARCHITECTURES", script)
        self.assertIn("CUDA_ARCH='89'", env)
        self.assertIn("GIT_REF='v0.5.3'", env)
        self.assertIn('git clone --branch "$GIT_REF"', script)
        self.assertIn("LichtFeld-Studio/build/LichtFeld-Studio", script)
        self.assertIn("tar --exclude='LichtFeld-Studio/.git'", script)
        self.assertIn("https://rest.runpod.io/v1/pods/${POD_ID}", script)
        self.assertIn("X DELETE", script)
        self.assertIn("TERMINATE=1", env)
        self.assertIn("lichtfeld-builds/lichtfeld-0.5.3-l40s-sm89-260829.upload/", env)
        self.assertIn("-RNFR ${RESULT_STAGING}", script)
        self.assertIn("git clone https://github.com/microsoft/vcpkg.git", script)
        self.assertNotIn("git clone --depth 1 https://github.com/microsoft/vcpkg.git", script)
        self.assertIn("self_terminate", script)
        self.assertIn("cmake-3.31.6-linux-x86_64.sh", env)
        self.assertIn("REPORT.md", script)
        self.assertNotIn("${{", text)
        self.assertIn(env.strip(), text)

    def test_override_script_next_to_config_wins(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workdir = Path(raw)
            override = workdir / "remote_build.sh"
            override.write_text("#!/usr/bin/env bash\necho override-pipeline\n", encoding="utf-8")
            self.assertEqual(resolve_build_script(workdir), override)
            self.assertEqual(resolve_build_script(None), PACKAGED_BUILD_SCRIPT)
            text = render_build_script(
                _cfg(),
                git_ref="v0.5.3",
                cuda_arch="89",
                repo_url="https://github.com/MrNeRF/LichtFeld-Studio.git",
                archive_name="x.tar.gz",
                app_workdir=workdir,
            )
            self.assertIn("override-pipeline", text)
            self.assertNotIn("CMAKE_CUDA_ARCHITECTURES", text)

    def test_inject_uploads_env_and_script(self) -> None:
        class RecSsh:
            def __init__(self) -> None:
                self.puts: list[tuple[str, str]] = []
                self.runs: list[str] = []

            def put(self, src: Path, dest: str) -> None:
                self.puts.append((str(src), dest))

            def run(self, cmd: str, **_k) -> None:
                self.runs.append(cmd)

        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            ssh = RecSsh()
            inject_and_start(
                ssh,
                _cfg(),
                run_dir,
                "pod1",
                None,
                None,
                kind="build",
                git_ref="v0.5.3",
                cuda_arch="89",
                repo_url="https://github.com/MrNeRF/LichtFeld-Studio.git",
                archive_name="lichtfeld-0.5.3-l40s-sm89.tar.gz",
            )
            dests = [d for _, d in ssh.puts]
            self.assertIn("/workspace/build.env", dests)
            self.assertIn("/workspace/remote_build.sh", dests)
            self.assertNotIn("/workspace/remote_job.sh", dests)
            self.assertTrue((run_dir / "build.env").is_file())
            self.assertTrue((run_dir / "remote_build.sh").is_file())
            joined = "\n".join(ssh.runs)
            self.assertIn("bash /workspace/remote_build.sh", joined)
            self.assertNotIn("remote_job.sh", joined)

    def test_compile_progress_label(self) -> None:
        msg = format_progress("compile", {"COMPILE": "[12/685]"}, "", None, None)
        self.assertEqual(msg, "compile [12/685]")
        self.assertEqual(format_progress("apt", {}, "", None, None), "installing compilers")


class BuildSubmitTests(unittest.TestCase):
    def test_submit_build_skips_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            mgr = JobManager(Path(raw))
            with patch.object(JobManager, "_spawn"):
                job = mgr.submit(
                    {
                        "kind": "build",
                        "name": "my-build",
                        "gpu": "NVIDIA L40S",
                        "cloud": "SECURE",
                        "git_ref": "v0.5.3",
                    }
                )
            self.assertEqual(job.kind, "build")
            self.assertEqual(job.git_ref, "v0.5.3")
            self.assertEqual(job.cuda_arch, "89")
            self.assertTrue(job.build_archive.endswith("lichtfeld-0.5.3-l40s-sm89.tar.gz"))
            self.assertTrue(job.result_dir.startswith("lichtfeld-builds/"))
            self.assertEqual(job.dataset_archive, "")

    def test_old_job_records_default_to_train(self) -> None:
        data = Job(
            id="x",
            name="n",
            phase="complete",
            gpu="NVIDIA L40S",
            cloud="SECURE",
            build_archive="a.tar.gz",
            dataset_archive="d.tar",
            dataset_source="ftp",
            dataset_local="",
            result_dir="r",
            config_rel="",
            auto_download=False,
            terminate_when_done=True,
            max_cap=None,
            enable_sparsity=None,
            gut=None,
        ).to_dict()
        data.pop("kind", None)
        restored = Job.from_dict(data)
        self.assertEqual(restored.kind, "train")

    def test_train_still_requires_build_and_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            mgr = JobManager(Path(raw))
            with patch.object(JobManager, "_spawn"):
                with self.assertRaises(ValueError):
                    mgr.submit({"kind": "train", "gpu": "NVIDIA L40S"})


if __name__ == "__main__":
    unittest.main()
