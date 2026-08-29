import tempfile
import unittest
from pathlib import Path

from lichtfeld_runpod.jobs import Job, JobStore


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


if __name__ == "__main__":
    unittest.main()
