from __future__ import annotations

import re
import time
from pathlib import Path

from .config import AppConfig
from .host import restrict_secret_file, write_text_lf
from .jobs import PRESUMED_COMPLETE_MESSAGE
from .log import log
from .remote_job import render_job_script
from .runpod import RunpodClient, RunpodError, ssh_endpoint
from .sshutil import Ssh, ensure_ed25519, ftp_check_due, write_ssh_config
from .storage import remote_size, results_look_complete, verify_uploaded, write_netrc

_TRAIN_RE = re.compile(
    r"(\d+)/(\d+)\s+\|\s+Loss:\s+([0-9.]+)\s+\|\s+Splats:\s+(\d+)"
)

POLL_CMD = (
    "echo STAGE:$(cat /workspace/state/STAGE 2>/dev/null || echo starting); "
    "echo BUILD:$(stat -c%s /workspace/lichtfeld-build.tar.gz 2>/dev/null || echo 0); "
    "echo DATA:$(stat -c%s /workspace/dataset/scene.tar 2>/dev/null || echo 0); "
    "echo DONE:$(test -f /workspace/state/upload.done && echo 1 || echo 0); "
    "echo EXIT:$(cat /workspace/state/train.exit 2>/dev/null || echo); "
    "echo HB:$(cat /workspace/state/HEARTBEAT 2>/dev/null || echo); "
    "echo ERR:$(test -f /workspace/state/ERROR && echo 1 || echo 0); "
    "echo PIDOK:$(if [ -f /workspace/state/job.pid ] && kill -0 $(cat /workspace/state/job.pid) 2>/dev/null; then echo 1; else echo 0; fi); "
    "python3 - <<'PY'\n"
    "from pathlib import Path\n"
    "p=Path('/workspace/logs/nohup.out')\n"
    "t=p.read_text(errors='replace') if p.exists() else ''\n"
    "i=t.rfind('Training [')\n"
    "print('TRAIN:'+(t[i:].splitlines()[0][:240] if i>=0 else ''))\n"
    "PY"
)

LOG_TAIL_CMD = (
    "echo '--- pipeline.log ---'; "
    "tail -c 24000 /workspace/logs/pipeline.log 2>/dev/null || true; "
    "echo; echo '--- nohup.out ---'; "
    "tail -c 8000 /workspace/logs/nohup.out 2>/dev/null || true; "
    "echo; echo '--- train.log (tail) ---'; "
    "tail -c 8000 /workspace/logs/train.log 2>/dev/null || true"
)


def run_job(cfg: AppConfig, workdir: Path) -> int:
    run_dir = workdir / ".run"
    run_dir.mkdir(parents=True, exist_ok=True)

    pubkey = ensure_ed25519(cfg.ssh.identity_file, cfg.ssh.public_key_file)
    client = RunpodClient(cfg.runpod.api_key)
    client.ensure_ssh_key(pubkey)

    log("storage", f"sizing {cfg.storage.build_archive}")
    build_bytes = remote_size(cfg.storage, cfg.storage.build_archive)
    log("storage", f"build size {build_bytes}")
    log("storage", f"sizing {cfg.storage.dataset_archive}")
    dataset_bytes = remote_size(cfg.storage, cfg.storage.dataset_archive)
    log("storage", f"dataset size {dataset_bytes}")

    try:
        pod = client.create_pod(
            name=cfg.job_name,
            image=cfg.runpod.image,
            gpu=cfg.runpod.gpu,
            gpu_count=cfg.runpod.gpu_count,
            cloud=cfg.runpod.cloud,
            disk_gb=cfg.runpod.container_disk_gb,
            volume_gb=cfg.runpod.volume_gb,
            volume_mount=cfg.runpod.volume_mount,
            pubkey=pubkey,
            allowed_cuda=cfg.runpod.allowed_cuda_versions,
        )
    except RunpodError as e:
        log("runpod", str(e))
        return 1
    pod_id = str(pod["id"])
    (run_dir / "pod_id").write_text(pod_id + "\n", encoding="utf-8")
    log("pod", f"{pod_id} gpu={cfg.runpod.gpu}")

    def stop_if_ftp_done(attempt: int) -> bool:
        if not ftp_check_due(attempt):
            return False
        if results_look_complete(cfg.storage, cfg.storage.result_dir):
            log("ftp", "found REPORT.md; treating job as completed (presumably)")
            return True
        return False

    endpoint = client.wait_ssh(pod_id, should_stop=stop_if_ftp_done)
    if endpoint is None:
        return _finish_from_ftp(cfg)
    host, port = endpoint
    ssh_config = run_dir / "ssh_config"
    write_ssh_config(ssh_config, host, port, cfg.ssh.identity_file)
    ssh = Ssh(ssh_config)
    if not ssh.wait_ready(should_stop=stop_if_ftp_done):
        return _finish_from_ftp(cfg)

    gpu_line = ssh.check_output("nvidia-smi -L && nvidia-smi --query-gpu=name,memory.total --format=csv,noheader")
    log("gpu", gpu_line.strip().replace("\n", " | "))

    inject_and_start(ssh, cfg, run_dir, pod_id, build_bytes, dataset_bytes)

    rc = _watch_progress(ssh, cfg, build_bytes, dataset_bytes, client=client, pod_id=pod_id)

    if rc == 0:
        names = verify_uploaded(cfg.storage, cfg.storage.result_dir)
        if names:
            log("result", f"{cfg.storage.result_dir}: " + ", ".join(names))
        else:
            log("result", f"uploaded to {cfg.storage.result_dir} (include REPORT.md + train.log)")
        if cfg.terminate_when_done:
            log("pod", f"{pod_id} will self-terminate after upload")
        else:
            log("pod", f"left running: {pod_id}  ssh root@{host} -p {port}")
    else:
        log("job", f"remote job failed rc={rc}")
        _dump_failure(ssh)
    return rc


def _finish_from_ftp(cfg: AppConfig) -> int:
    if results_look_complete(cfg.storage, cfg.storage.result_dir):
        names = verify_uploaded(cfg.storage, cfg.storage.result_dir)
        log("job", PRESUMED_COMPLETE_MESSAGE)
        if names:
            log("result", f"{cfg.storage.result_dir}: " + ", ".join(names))
        return 0
    log("job", "SSH unavailable and no REPORT.md on FTP")
    return 1


def inject_and_start(
    ssh: Ssh,
    cfg: AppConfig,
    run_dir: Path,
    pod_id: str,
    build_bytes: int | None,
    dataset_bytes: int | None,
) -> None:
    """Copy credentials + job script and start it under nohup. The pod owns the rest."""
    netrc = run_dir / "netrc"
    write_netrc(netrc, cfg.storage)
    api_file = run_dir / "runpod_api"
    write_text_lf(api_file, cfg.runpod.api_key)
    restrict_secret_file(api_file)

    ssh.run("mkdir -p /workspace/logs /workspace/state /root")
    ssh.put(netrc, "/root/.netrc")
    ssh.put(api_file, "/root/.runpod_api")

    script_text = render_job_script(cfg, build_bytes, dataset_bytes, pod_id=pod_id)
    local_script = run_dir / "remote_job.sh"
    write_text_lf(local_script, script_text)
    ssh.put(local_script, "/workspace/remote_job.sh")
    ssh.run("chmod 600 /root/.netrc /root/.runpod_api && chmod +x /workspace/remote_job.sh")

    log("job", "starting remote pipeline (autonomous)")
    ssh.run(
        "nohup bash /workspace/remote_job.sh >> /workspace/logs/nohup.out 2>&1 & echo $! > /workspace/state/job.pid",
        timeout=30,
    )


def poll_remote_state(ssh: Ssh, timeout: int = 45) -> dict[str, str]:
    blob = ssh.check_output(POLL_CMD, timeout=timeout, attempts=1)
    fields: dict[str, str] = {}
    for line in blob.splitlines():
        if line.startswith("TRAIN:"):
            fields["TRAIN"] = line[6:]
        elif ":" in line:
            k, _, v = line.partition(":")
            fields[k] = v.strip()
    return fields


def fetch_log_tail(ssh: Ssh, timeout: int = 30) -> str:
    try:
        return ssh.check_output(LOG_TAIL_CMD, timeout=timeout, attempts=1)
    except Exception as e:
        return f"(could not fetch logs: {e})"


def _watch_progress(
    ssh: Ssh,
    cfg: AppConfig,
    build_bytes: int | None,
    dataset_bytes: int | None,
    *,
    client: RunpodClient | None = None,
    pod_id: str = "",
) -> int:
    last_msg = ""
    last_stage = ""
    ssh_fails = 0
    while True:
        try:
            fields = poll_remote_state(ssh)
            ssh_fails = 0
        except Exception as e:
            log("watch", f"poll failed: {e}")
            ssh_fails += 1
            if last_stage in {"done", "upload"} and ssh_fails >= 2:
                log("job", "lost SSH after upload; assuming pod self-terminated")
                return 0
            if ftp_check_due(ssh_fails) and results_look_complete(cfg.storage, cfg.storage.result_dir):
                log("job", PRESUMED_COMPLETE_MESSAGE)
                return 0
            if cfg.terminate_when_done and client and pod_id and ssh_fails >= 4:
                if not _pod_still_running(client, pod_id) and last_stage in {"done", "upload", "report", "train"}:
                    log("job", "pod gone after last known progress; treating as complete")
                    return 0 if last_stage in {"done", "upload", "report"} else 1
            time.sleep(cfg.progress_interval_seconds)
            continue

        stage = fields.get("STAGE", "starting")
        last_stage = stage
        train_line = fields.get("TRAIN", "")
        msg = format_progress(stage, fields, train_line, build_bytes, dataset_bytes)
        if msg != last_msg:
            log(stage[:12], msg)
            last_msg = msg

        if fields.get("DONE") == "1" or stage == "done":
            log("job", "remote stages complete")
            return 0

        if fields.get("ERR") == "1":
            exit_code = fields.get("EXIT", "") or "1"
            try:
                return int(exit_code)
            except ValueError:
                return 1

        pid_ok = fields.get("PIDOK") == "1"
        exit_code = fields.get("EXIT", "")
        if not pid_ok and stage not in {"done", "upload", "report"}:
            if exit_code and exit_code != "0":
                return int(exit_code)
            try:
                tail = ssh.check_output(
                    "tail -20 /workspace/logs/pipeline.log /workspace/logs/nohup.out 2>/dev/null",
                    timeout=20,
                    attempts=1,
                )
                log("job", "remote process exited\n" + tail)
            except Exception:
                pass
            return 1

        time.sleep(cfg.progress_interval_seconds)


def format_progress(
    stage: str,
    fields: dict[str, str],
    train_line: str,
    build_bytes: int | None,
    dataset_bytes: int | None,
) -> str:
    if stage == "download_build":
        return _pct("download build", fields.get("BUILD"), build_bytes)
    if stage == "download_dataset":
        return _pct("download dataset", fields.get("DATA"), dataset_bytes)
    if stage == "train" and train_line:
        m = _TRAIN_RE.search(train_line)
        if m:
            cur, total, loss, splats = m.groups()
            pct = 100.0 * int(cur) / max(int(total), 1)
            eta = ""
            em = re.search(r"<([^]]+)]", train_line)
            if em:
                eta = f"  remaining={em.group(1)}"
            return f"{cur}/{total} ({pct:.0f}%)  loss={loss}  splats={int(splats):,}{eta}"
        return train_line.strip()[:200]
    return stage.replace("_", " ")


def _pct(label: str, got: str | None, expected: int | None) -> str:
    try:
        n = int(got or 0)
    except ValueError:
        n = 0
    if expected and expected > 0:
        return f"{label} {n / expected * 100:.1f}%  ({n:,}/{expected:,} bytes)"
    return f"{label} {n:,} bytes"


def _dump_failure(ssh: Ssh) -> None:
    try:
        log("log", fetch_log_tail(ssh))
    except Exception as e:
        log("log", f"could not fetch logs: {e}")


def _pod_still_running(client: RunpodClient, pod_id: str) -> bool:
    try:
        pod = client.get_pod(pod_id)
    except RunpodError:
        return False
    status = str(pod.get("status") or pod.get("desiredStatus") or "").upper()
    return status in {"RUNNING", "EXITED"} and ssh_endpoint(pod) is not None
