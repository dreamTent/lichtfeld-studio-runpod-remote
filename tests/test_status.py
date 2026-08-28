import unittest

from lichtfeld_runpod.status import (
    BLUE_BLINK,
    GREEN,
    GREEN_BLINK,
    RED,
    RED_BLINK,
    WHITE,
    YELLOW,
    heartbeat_ok,
    job_indicator,
    pod_indicator,
)


class PodIndicatorTests(unittest.TestCase):
    def test_foreign_pod_is_white(self) -> None:
        self.assertEqual(
            pod_indicator(
                controlled=False,
                api_running=True,
                heartbeat_fresh=True,
                has_error=False,
                job_complete=False,
            ),
            WHITE,
        )

    def test_controlled_heartbeat_is_green(self) -> None:
        self.assertEqual(
            pod_indicator(
                controlled=True,
                api_running=True,
                heartbeat_fresh=True,
                has_error=False,
                job_complete=False,
            ),
            GREEN,
        )

    def test_stale_ssh_while_running_is_yellow(self) -> None:
        self.assertEqual(
            pod_indicator(
                controlled=True,
                api_running=True,
                heartbeat_fresh=False,
                has_error=False,
                job_complete=False,
            ),
            YELLOW,
        )

    def test_error_is_blinking_red(self) -> None:
        self.assertEqual(
            pod_indicator(
                controlled=True,
                api_running=True,
                heartbeat_fresh=True,
                has_error=True,
                job_complete=False,
            ),
            RED_BLINK,
        )

    def test_vanished_before_complete_is_red(self) -> None:
        self.assertEqual(
            pod_indicator(
                controlled=True,
                api_running=False,
                heartbeat_fresh=False,
                has_error=False,
                job_complete=False,
            ),
            RED,
        )

    def test_complete_stays_green_even_if_pod_gone(self) -> None:
        self.assertEqual(
            pod_indicator(
                controlled=True,
                api_running=False,
                heartbeat_fresh=False,
                has_error=False,
                job_complete=True,
            ),
            GREEN,
        )


class JobIndicatorTests(unittest.TestCase):
    def test_created_blinks_green(self) -> None:
        self.assertEqual(job_indicator("created"), GREEN_BLINK)

    def test_upload_and_wait_blink_blue(self) -> None:
        for phase in ("uploading_dataset", "waiting_for_pod", "starting"):
            self.assertEqual(job_indicator(phase), BLUE_BLINK)

    def test_running_follows_pod(self) -> None:
        self.assertEqual(job_indicator("running", GREEN), GREEN)
        self.assertEqual(job_indicator("running", YELLOW), YELLOW)

    def test_error_blinks_red(self) -> None:
        self.assertEqual(job_indicator("error", GREEN), RED_BLINK)


class HeartbeatTests(unittest.TestCase):
    def test_fresh_and_stale(self) -> None:
        self.assertTrue(heartbeat_ok(100.0, 120.0))
        self.assertFalse(heartbeat_ok(100.0, 140.0))
        self.assertFalse(heartbeat_ok(None, 120.0))


if __name__ == "__main__":
    unittest.main()
