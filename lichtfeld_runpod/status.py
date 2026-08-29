"""Status-circle colors for pods and jobs (used by the UI and tests)."""

from __future__ import annotations

HEARTBEAT_STALE_SECONDS = 30.0

# Pod circles
WHITE = "white"
GREEN = "green"
TEAL = "teal"
YELLOW = "yellow"
RED_BLINK = "red_blink"
RED = "red"

# Extra job-only states
GREEN_BLINK = "green_blink"
BLUE_BLINK = "blue_blink"

PRE_POD_PHASES = frozenset({"created", "uploading_dataset", "waiting_for_pod", "starting"})


def heartbeat_ok(last_ssh_ok: float | None, now: float, stale: float = HEARTBEAT_STALE_SECONDS) -> bool:
    if last_ssh_ok is None:
        return False
    return (now - last_ssh_ok) <= stale


def pod_indicator(
    *,
    controlled: bool,
    api_running: bool,
    heartbeat_fresh: bool,
    has_error: bool,
    job_complete: bool,
) -> str:
    """Return a pod circle token: white, green, teal, yellow, red_blink, red."""
    if not controlled:
        return WHITE
    if has_error:
        return RED_BLINK
    if job_complete:
        return TEAL
    if not api_running:
        return RED
    if heartbeat_fresh:
        return GREEN
    return YELLOW


def job_indicator(phase: str, pod_color: str | None = None) -> str:
    """Return a job row token. Pre-pod work blinks; afterwards follow the pod."""
    if phase == "created":
        return GREEN_BLINK
    if phase in {"uploading_dataset", "waiting_for_pod", "starting"}:
        return BLUE_BLINK
    if phase == "error":
        return RED_BLINK
    if phase == "complete":
        return TEAL
    if pod_color:
        return pod_color
    return BLUE_BLINK
