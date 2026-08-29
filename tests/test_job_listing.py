import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from lichtfeld_runpod.config import (
    AppConfig,
    LichtfeldConfig,
    RunpodConfig,
    SshConfig,
    StorageConfig,
)
from lichtfeld_runpod.jobs import Job, JobStore
from lichtfeld_runpod.manager import JobManager
from lichtfeld_runpod.runpod import RunpodClient, RunpodError


def _job(**kw) -> Job:
    base = dict(
        id="abc123def456",
        name="demo",
        phase="complete",
        gpu="NVIDIA L40S",
        cloud="SECURE",
        build_archive="build.tar.gz",
        dataset_archive="data.tar",
        dataset_source="ftp",
        dataset_local="",
        result_dir="lichtfeld-results/demo",
        config_rel="",
        auto_download=False,
        terminate_when_done=True,
        max_cap=None,
        enable_sparsity=True,
        gut=True,
    )
    base.update(kw)
    return Job(**base)


class JobListingTests(unittest.TestCase):
    def test_archived_defaults_false_on_old_records(self) -> None:
        job = Job.from_dict(_job().to_dict())
        data = job.to_dict()
        data.pop("archived", None)
        restored = Job.from_dict(data)
        self.assertFalse(restored.archived)

    def test_connection_errors_defaults_zero_on_old_records(self) -> None:
        data = _job().to_dict()
        data.pop("connection_errors", None)
        restored = Job.from_dict(data)
        self.assertEqual(restored.connection_errors, 0)

    def test_snapshot_includes_next_check_countdown(self) -> None:
        from lichtfeld_runpod.manager import JobManager

        with tempfile.TemporaryDirectory() as raw:
            mgr = JobManager(Path(raw))
            job = _job(phase="running")
            mgr.store.save(job)
            mgr._set_next_check(job.id, 15, "poll")
            snap = mgr.snapshot()
            row = snap["jobs"][0]
            self.assertEqual(row["next_check_kind"], "poll")
            self.assertGreater(row["next_check_at"], time.time())
            self.assertLessEqual(row["next_check_at"], time.time() + 16)
            self.assertIsNone(snap["next_pod_list_at"])
            mgr._set_next_check(job.id, 8, "retry")
            row = mgr.snapshot()["jobs"][0]
            self.assertEqual(row["next_check_kind"], "retry")
            mgr._clear_next_check(job.id)
            row = mgr.snapshot()["jobs"][0]
            self.assertIsNone(row["next_check_at"])
            self.assertIsNone(row["next_check_kind"])
            with mgr._lock:
                mgr._next_pod_list_at = time.time() + 30
            later = mgr.snapshot()
            self.assertGreater(later["next_pod_list_at"], time.time())
            self.assertLessEqual(later["next_pod_list_at"], time.time() + 31)

    def test_poll_seconds_is_thirty(self) -> None:
        from lichtfeld_runpod.manager import POLL_SECONDS

        self.assertEqual(POLL_SECONDS, 30)

    def test_delete_removes_listing_not_results_dir(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = JobStore(root / "jobs")
            job = _job()
            store.save(job)
            store.workdir(job.id).joinpath("ssh_config").write_text("x", encoding="utf-8")
            results = root / "results" / job.id
            results.mkdir(parents=True)
            (results / "REPORT.md").write_text("ok", encoding="utf-8")

            self.assertTrue(store.delete(job.id))
            self.assertIsNone(store.get(job.id))
            self.assertFalse((root / "jobs" / job.id).exists())
            self.assertTrue((results / "REPORT.md").is_file())
            self.assertFalse(store.delete(job.id))


class OpenLocalResultsTests(unittest.TestCase):
    def test_snapshot_flags_ready_when_folder_exists(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            mgr = JobManager(root)
            dest = root / "results" / "abc123def456"
            dest.mkdir(parents=True)
            job = _job(phase="complete", local_results=str(dest), auto_download=True)
            mgr.store.save(job)
            row = mgr.snapshot()["jobs"][0]
            self.assertTrue(row["local_results_ready"])
            self.assertEqual(mgr.local_results_dir(job), dest.resolve())

    def test_snapshot_not_ready_without_folder(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            mgr = JobManager(Path(raw))
            job = _job(phase="complete", auto_download=True, local_results="")
            mgr.store.save(job)
            row = mgr.snapshot()["jobs"][0]
            self.assertFalse(row["local_results_ready"])

    def test_snapshot_not_ready_while_running(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            mgr = JobManager(root)
            dest = root / "results" / "abc123def456"
            dest.mkdir(parents=True)
            job = _job(phase="running", local_results=str(dest), auto_download=True)
            mgr.store.save(job)
            self.assertFalse(mgr.snapshot()["jobs"][0]["local_results_ready"])
            self.assertIsNone(mgr.local_results_dir(job))

    def test_open_local_results_opens_downloaded_folder(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            mgr = JobManager(root)
            dest = root / "results" / "abc123def456"
            dest.mkdir(parents=True)
            job = _job(phase="complete", local_results=str(dest), auto_download=True)
            mgr.store.save(job)
            with patch("lichtfeld_runpod.manager.open_in_file_manager") as open_fn:
                out = mgr.open_local_results(job.id)
            open_fn.assert_called_once_with(dest.resolve())
            self.assertEqual(out, {"ok": True, "id": job.id, "path": str(dest.resolve())})

    def test_open_local_results_uses_default_results_dir(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            mgr = JobManager(root)
            dest = root / "results" / "abc123def456"
            dest.mkdir(parents=True)
            job = _job(phase="complete", local_results="", auto_download=True)
            mgr.store.save(job)
            with patch("lichtfeld_runpod.manager.open_in_file_manager") as open_fn:
                out = mgr.open_local_results(job.id)
            open_fn.assert_called_once_with(dest.resolve())
            self.assertEqual(out["path"], str(dest.resolve()))

    def test_open_local_results_rejects_running_job(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            mgr = JobManager(Path(raw))
            job = _job(phase="running")
            mgr.store.save(job)
            with self.assertRaises(ValueError):
                mgr.open_local_results(job.id)

    def test_open_local_results_rejects_missing_folder(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            mgr = JobManager(Path(raw))
            job = _job(phase="complete", local_results="", auto_download=True)
            mgr.store.save(job)
            with self.assertRaises(FileNotFoundError):
                mgr.open_local_results(job.id)

    def test_open_local_results_rejects_path_outside_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            mgr = JobManager(root / "workdir")
            outside = root / "elsewhere"
            outside.mkdir()
            job = _job(phase="complete", local_results=str(outside), auto_download=True)
            mgr.store.save(job)
            with self.assertRaises(FileNotFoundError):
                mgr.open_local_results(job.id)

    def test_open_local_results_unknown_job(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            mgr = JobManager(Path(raw))
            with self.assertRaises(KeyError):
                mgr.open_local_results("missing")


class FakeSsh:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class DiscardPodTests(unittest.TestCase):
    def _patch_runpod(self, client: MagicMock):
        cfg = SimpleNamespace(runpod=SimpleNamespace(api_key="k"))
        return (
            patch("lichtfeld_runpod.manager.load_app_config", return_value=cfg),
            patch("lichtfeld_runpod.manager.RunpodClient", return_value=client),
        )

    def test_discard_marks_running_job_error_and_closes_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            mgr = JobManager(Path(raw))
            job = _job(phase="running", pod_id="pod1")
            mgr.store.save(job)
            ssh = FakeSsh()
            mgr._ssh[job.id] = ssh
            mgr._set_next_check(job.id, 15, "poll")
            client = MagicMock()
            client.list_pods.return_value = []
            load, rp = self._patch_runpod(client)
            with load, rp:
                out = mgr.discard_pod("pod1")
            self.assertEqual(out, {"ok": True, "id": "pod1"})
            client.terminate.assert_called_once_with("pod1")
            stored = mgr.store.get(job.id)
            self.assertEqual(stored.phase, "error")
            self.assertEqual(stored.message, "pod discarded")
            self.assertEqual(stored.error, "pod discarded")
            self.assertTrue(ssh.closed)
            self.assertNotIn(job.id, mgr._ssh)
            self.assertTrue(mgr._job_should_stop(job.id))

    def test_discard_leaves_complete_job_complete(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            mgr = JobManager(Path(raw))
            job = _job(phase="complete", pod_id="pod1")
            mgr.store.save(job)
            client = MagicMock()
            client.list_pods.return_value = []
            load, rp = self._patch_runpod(client)
            with load, rp:
                mgr.discard_pod("pod1")
            stored = mgr.store.get(job.id)
            self.assertEqual(stored.phase, "complete")
            client.terminate.assert_called_once_with("pod1")

    def test_discard_foreign_pod(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            mgr = JobManager(Path(raw))
            mgr._api_pods = [{"id": "foreign", "status": "RUNNING"}]
            client = MagicMock()
            client.list_pods.return_value = []
            load, rp = self._patch_runpod(client)
            with load, rp:
                mgr.discard_pod("foreign")
            client.terminate.assert_called_once_with("foreign")
            self.assertEqual(mgr._api_pods, [])
            self.assertEqual(mgr.jobs(), [])

    def test_discard_failure_unhalts_running_job(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            mgr = JobManager(Path(raw))
            job = _job(phase="running", pod_id="pod1")
            mgr.store.save(job)
            client = MagicMock()
            client.terminate.side_effect = RunpodError("nope")
            load, rp = self._patch_runpod(client)
            with load, rp, patch.object(mgr, "_spawn") as spawn:
                with self.assertRaises(RunpodError):
                    mgr.discard_pod("pod1")
                spawn.assert_called_once_with(job.id)
            stored = mgr.store.get(job.id)
            self.assertEqual(stored.phase, "running")
            self.assertNotIn(job.id, mgr._halted)

    def test_empty_pod_id_raises(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            mgr = JobManager(Path(raw))
            with self.assertRaises(ValueError):
                mgr.discard_pod("  ")


def _app_cfg(*, dataset_archive: str = "yaml-default.zip") -> AppConfig:
    return AppConfig(
        job_name="t",
        progress_interval_seconds=15,
        terminate_when_done=True,
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
            dataset_archive=dataset_archive,
            build_archive="build.tar.gz",
            result_dir="lichtfeld-results/t",
        ),
        lichtfeld=LichtfeldConfig(
            config="",
            max_cap=None,
            enable_sparsity=True,
            gut=True,
            headless=True,
            extra_args=[],
            iterations=None,
            strategy=None,
        ),
    )


class RunJobConfigTests(unittest.TestCase):
    def test_inject_uses_uploaded_dataset_not_yaml_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            mgr = JobManager(Path(raw))
            job = _job(
                phase="created",
                dataset_source="local",
                dataset_archive="",
                dataset_local="/tmp/scene",
                pod_id=None,
                injected=False,
            )
            mgr.store.save(job)
            captured: dict[str, str] = {}

            def upload(job, cfg, run_dir, netrc):
                mgr._update(job, dataset_archive=f"lichtfeld-datasets/{job.id}.tar", dataset_bytes=99)

            def create(job, cfg):
                mgr._update(job, pod_id="pod1")

            def inject(job, cfg, run_dir):
                captured["dataset"] = cfg.storage.dataset_archive
                mgr._update(job, injected=True)

            with (
                patch("lichtfeld_runpod.manager.load_app_config", return_value=_app_cfg()),
                patch("lichtfeld_runpod.manager.write_netrc"),
                patch.object(mgr, "_upload_local_dataset", side_effect=upload),
                patch.object(mgr, "_create_pod", side_effect=create),
                patch.object(mgr, "_inject", side_effect=inject),
                patch.object(mgr, "_watch"),
            ):
                mgr._run_job(job.id)
            self.assertEqual(captured["dataset"], f"lichtfeld-datasets/{job.id}.tar")

    def test_upload_as_is_skips_packing_copy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "scene.tar"
            archive.write_bytes(b"abc")
            mgr = JobManager(root)
            job = _job(
                phase="created",
                dataset_source="local",
                dataset_local=str(archive),
                upload_as_is=True,
            )
            mgr.store.save(job)
            run_dir = mgr.store.workdir(job.id)
            netrc = run_dir / "netrc"
            netrc.write_text("x", encoding="utf-8")
            with (
                patch("lichtfeld_runpod.manager.tar_directory") as tar,
                patch("lichtfeld_runpod.manager.curl_put") as put,
            ):
                mgr._upload_local_dataset(job, _app_cfg(), run_dir, netrc)
            tar.assert_not_called()
            put.assert_called_once()
            self.assertEqual(Path(put.call_args[0][1]), archive)

    def test_abort_upload_halts_and_deletes_staging(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            mgr = JobManager(Path(raw))
            job = _job(phase="uploading_dataset", dataset_archive="lichtfeld-datasets/scene.tar")
            mgr.store.save(job)
            with (
                patch("lichtfeld_runpod.manager.load_app_config", return_value=_app_cfg()),
                patch("lichtfeld_runpod.manager.remote_delete") as delete,
            ):
                out = mgr.abort_upload(job.id)
            self.assertEqual(out.phase, "error")
            self.assertEqual(out.message, "upload aborted")
            self.assertTrue(mgr._job_should_stop(job.id))
            delete.assert_called_once()
            self.assertEqual(delete.call_args[0][1], "lichtfeld-datasets/scene.tar.upload")

    def test_abort_upload_rejects_running_job(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            mgr = JobManager(Path(raw))
            job = _job(phase="running")
            mgr.store.save(job)
            with self.assertRaises(ValueError):
                mgr.abort_upload(job.id)

    def test_halted_progress_does_not_overwrite_abort(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            mgr = JobManager(Path(raw))
            job = _job(phase="uploading_dataset")
            mgr.store.save(job)
            mgr._halt_job(job.id)
            mgr._update(job, phase="error", error="upload aborted", message="upload aborted")
            mgr._update(job, message="upload dataset 50%")
            stored = mgr.store.get(job.id)
            self.assertEqual(stored.phase, "error")
            self.assertEqual(stored.message, "upload aborted")

    def test_dead_pid_marks_job_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            mgr = JobManager(Path(raw))
            job = _job(phase="running", stage="download_dataset", injected=True)
            mgr.store.save(job)
            stop = mgr._on_poll_ok(
                job,
                {
                    "STAGE": "download_dataset",
                    "PIDOK": "0",
                    "ERR": "0",
                    "DONE": "0",
                    "EXIT": "",
                    "DATA": "930264494",
                },
                _app_cfg(),
                None,
            )
            self.assertTrue(stop)
            stored = mgr.store.get(job.id)
            self.assertEqual(stored.phase, "error")
            self.assertIn("remote process exited", stored.error or "")


class TerminateApiTests(unittest.TestCase):
    def test_404_is_success(self) -> None:
        client = RunpodClient("k")
        with patch.object(client, "request", return_value=(404, {"error": "gone"})):
            client.terminate("x")

    def test_500_raises(self) -> None:
        client = RunpodClient("k")
        with patch.object(client, "request", return_value=(500, "fail")):
            with self.assertRaises(RunpodError):
                client.terminate("x")


if __name__ == "__main__":
    unittest.main()
