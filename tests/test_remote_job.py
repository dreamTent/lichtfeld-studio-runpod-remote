import unittest
from pathlib import Path

from lichtfeld_runpod.config import (
    AppConfig,
    LichtfeldConfig,
    RunpodConfig,
    SshConfig,
    StorageConfig,
)
from lichtfeld_runpod.remote_job import render_job_script


def _cfg(*, terminate: bool = True) -> AppConfig:
    return AppConfig(
        job_name="t",
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
            dataset_archive="data.tar",
            build_archive="build.tar.gz",
            result_dir="lichtfeld-results/t",
        ),
        lichtfeld=LichtfeldConfig(
            config="",
            max_cap=10000000,
            enable_sparsity=True,
            gut=True,
            headless=True,
            extra_args=[],
            iterations=None,
            strategy=None,
        ),
    )


class RemoteJobScriptTests(unittest.TestCase):
    def test_self_delete_and_heartbeat(self) -> None:
        text = render_job_script(_cfg(), 1, 1, pod_id="podabc")
        self.assertIn("HEARTBEAT", text)
        self.assertIn("/root/.runpod_api", text)
        self.assertIn("https://rest.runpod.io/v1/pods/${POD_ID}", text)
        self.assertIn("X DELETE", text)
        self.assertIn("TERMINATE=1", text)
        self.assertIn("STATEDIR/ERROR", text)

    def test_no_terminate_flag(self) -> None:
        text = render_job_script(_cfg(terminate=False), 1, 1, pod_id="podabc")
        self.assertIn("TERMINATE=0", text)

    def test_client_does_not_terminate_on_success(self) -> None:
        import inspect
        from lichtfeld_runpod import orchestrate

        src = inspect.getsource(orchestrate.run_job)
        self.assertNotIn("client.terminate", src)
        self.assertIn("inject_and_start", src)


if __name__ == "__main__":
    unittest.main()
