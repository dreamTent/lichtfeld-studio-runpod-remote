import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
                mgr._next_pod_list_at = time.time() + 8
            later = mgr.snapshot()
            self.assertGreater(later["next_pod_list_at"], time.time())
            self.assertLessEqual(later["next_pod_list_at"], time.time() + 9)

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
