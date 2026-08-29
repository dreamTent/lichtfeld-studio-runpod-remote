import unittest

from lichtfeld_runpod.jobs import PRESUMED_COMPLETE_MESSAGE
from lichtfeld_runpod.status import TEAL, job_indicator
from lichtfeld_runpod.storage import result_names_look_complete
from lichtfeld_runpod.sshutil import ftp_check_due


class ResultCompleteTests(unittest.TestCase):
    def test_report_md_is_the_marker(self) -> None:
        self.assertTrue(result_names_look_complete(["REPORT.md", "train.log"]))
        self.assertTrue(result_names_look_complete(["output/foo.ply", "report.md"]))
        self.assertFalse(result_names_look_complete(["train.log", "scene.ply"]))
        self.assertFalse(result_names_look_complete([]))

    def test_ftp_check_once_after_five_ssh_fails(self) -> None:
        self.assertFalse(ftp_check_due(1))
        self.assertFalse(ftp_check_due(4))
        self.assertTrue(ftp_check_due(5))
        self.assertFalse(ftp_check_due(6))
        self.assertFalse(ftp_check_due(10))

    def test_presumed_complete_is_teal(self) -> None:
        self.assertEqual(job_indicator("complete"), TEAL)
        self.assertEqual(PRESUMED_COMPLETE_MESSAGE, "completed (presumably)")


if __name__ == "__main__":
    unittest.main()
