from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from .config import AppConfig, ConfigError, config_for_job, load_app_config
from .jobs import PRESUMED_COMPLETE_MESSAGE, Job, JobStore, default_job_name, new_id
from .log import log
from .orchestrate import fetch_log_tail, format_progress, inject_and_start, poll_remote_state
from .runpod import RunpodClient, RunpodError, pod_is_running, ssh_endpoint
from .sshutil import Ssh, ensure_ed25519, ftp_check_due, write_ssh_config
from .status import HEARTBEAT_STALE_SECONDS, heartbeat_ok, job_indicator, pod_indicator
from .storage import (
    curl_put,
    download_result_dir,
    list_remote_files,
    remote_size,
    results_look_complete,
    tar_directory,
    write_netrc,
)


ACTIVE_PHASES = {"created", "uploading_dataset", "waiting_for_pod", "starting", "running"}
POLL_SECONDS = 8


class JobManager:
    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.store = JobStore(workdir / ".run" / "jobs")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ssh: dict[str, Ssh] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._api_pods: list[dict[str, Any]] = []
        self._api_pods_at = 0.0
        self._loop_thread: threading.Thread | None = None

    def start(self) -> None:
        self._reconcile_from_ftp()
        for job in self.store.all():
            if job.phase in ACTIVE_PHASES:
                self._spawn(job.id)
        self._loop_thread = threading.Thread(target=self._loop, name="job-manager", daemon=True)
        self._loop_thread.start()

    def stop(self) -> None:
        self._stop.set()

    def submit(self, spec: dict[str, Any]) -> Job:
        now = time.time()
        job_id = new_id()
        name = str(spec.get("name") or "").strip() or default_job_name()
        result_dir = str(spec.get("result_dir") or f"lichtfeld-results/{name}-{job_id}")
        job = Job(
            id=job_id,
            name=name,
            phase="created",
            gpu=str(spec.get("gpu") or "NVIDIA L40S"),
            cloud=str(spec.get("cloud") or "SECURE").upper(),
            build_archive=str(spec.get("build_archive") or ""),
            dataset_archive=str(spec.get("dataset_archive") or ""),
            dataset_source=str(spec.get("dataset_source") or "ftp"),
            dataset_local=str(spec.get("dataset_local") or ""),
            result_dir=result_dir.strip("/"),
            config_rel=str(spec.get("config") or "").strip(),
            auto_download=bool(spec.get("auto_download", False)),
            terminate_when_done=bool(spec.get("terminate_when_done", True)),
            max_cap=spec.get("max_cap"),
            enable_sparsity=bool(spec.get("enable_sparsity", True)),
            gut=bool(spec.get("gut", True)),
            message="created",
            created_at=now,
            updated_at=now,
        )
        if job.dataset_source == "ftp" and not job.dataset_archive:
            raise ValueError("select a dataset archive on the FTP server")
        if job.dataset_source == "local" and not job.dataset_local:
            raise ValueError("select a local dataset directory")
        if not job.build_archive:
            raise ValueError("select a LichtFeld build from the FTP server")
        self._save(job)
        self._spawn(job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        return self.store.get(job_id)

    def jobs(self) -> list[Job]:
        return self.store.all()

    def snapshot(self) -> dict[str, Any]:
        jobs = self.store.all()
        pods_raw = self._api_pods
        controlled = {j.pod_id: j for j in jobs if j.pod_id}
        now = time.time()
        pod_rows = []
        seen: set[str] = set()
        for pod in pods_raw:
            pid = str(pod.get("id") or "")
            if not pid:
                continue
            seen.add(pid)
            job = controlled.get(pid)
            running = pod_is_running(pod)
            fresh = heartbeat_ok(job.last_ssh_ok, now) if job else False
            color = pod_indicator(
                controlled=job is not None,
                api_running=running,
                heartbeat_fresh=fresh,
                has_error=bool(job and (job.phase == "error" or job.error)),
                job_complete=bool(job and job.phase == "complete"),
            )
            endpoint = ssh_endpoint(pod)
            pod_rows.append(
                {
                    "id": pid,
                    "name": pod.get("name") or pid,
                    "status": pod.get("status") or pod.get("desiredStatus"),
                    "gpu": _pod_gpu(pod),
                    "controlled": job is not None,
                    "job_id": job.id if job else None,
                    "color": color,
                    "ssh": f"{endpoint[0]}:{endpoint[1]}" if endpoint else None,
                }
            )
        for job in jobs:
            if job.pod_id and job.pod_id not in seen:
                color = pod_indicator(
                    controlled=True,
                    api_running=False,
                    heartbeat_fresh=False,
                    has_error=job.phase == "error" or bool(job.error),
                    job_complete=job.phase == "complete",
                )
                pod_rows.append(
                    {
                        "id": job.pod_id,
                        "name": job.name,
                        "status": "GONE",
                        "gpu": job.gpu,
                        "controlled": True,
                        "job_id": job.id,
                        "color": color,
                        "ssh": None,
                    }
                )
        job_rows = []
        pod_color_by_job = {p["job_id"]: p["color"] for p in pod_rows if p.get("job_id")}
        for job in jobs:
            job_rows.append(
                {
                    **job.to_dict(),
                    "color": job_indicator(job.phase, pod_color_by_job.get(job.id) if job.pod_id else None),
                }
            )
        return {"jobs": job_rows, "pods": pod_rows}

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_pods()
            except Exception as e:
                log("manager", f"list pods: {e}")
            try:
                self._reconcile_from_ftp()
            except Exception as e:
                log("manager", f"ftp reconcile: {e}")
            self._stop.wait(POLL_SECONDS)

    def _refresh_pods(self) -> None:
        try:
            cfg = load_app_config(self.workdir)
        except (ConfigError, Exception):
            return
        if not cfg.runpod.api_key:
            return
        try:
            pods = RunpodClient(cfg.runpod.api_key).list_pods()
        except RunpodError as e:
            log("manager", f"list pods failed: {e}")
            return
        with self._lock:
            self._api_pods = pods
            self._api_pods_at = time.time()

    def _ftp_stop(self, cfg: AppConfig, job: Job, attempt: int) -> bool:
        if not ftp_check_due(attempt):
            return False
        return results_look_complete(cfg.storage, job.result_dir)

    def _maybe_presumed_complete(self, job: Job, cfg: AppConfig) -> bool:
        if job.phase == "complete":
            return True
        if not results_look_complete(cfg.storage, job.result_dir):
            return False
        log("job", f"{job.id} {PRESUMED_COMPLETE_MESSAGE}")
        self._finish_ok(job, cfg, presumed=True)
        return True

    def _reconcile_from_ftp(self) -> None:
        try:
            load_app_config(self.workdir)
        except (ConfigError, Exception):
            return
        for job in self.store.all():
            if job.phase == "complete":
                continue
            if not job.result_dir:
                continue
            if not (job.injected or job.pod_id or job.phase == "error"):
                continue
            try:
                cfg = self._cfg_for(job)
            except Exception:
                continue
            self._maybe_presumed_complete(job, cfg)

    def _spawn(self, job_id: str) -> None:
        with self._lock:
            t = self._threads.get(job_id)
            if t and t.is_alive():
                return
            thread = threading.Thread(target=self._run_job, args=(job_id,), name=f"job-{job_id}", daemon=True)
            self._threads[job_id] = thread
            thread.start()

    def _save(self, job: Job) -> None:
        self.store.save(job)

    def _update(self, job: Job, **fields: Any) -> Job:
        for k, v in fields.items():
            setattr(job, k, v)
        self._save(job)
        return job

    def _cfg_for(self, job: Job) -> AppConfig:
        base = load_app_config(self.workdir)
        return config_for_job(
            base,
            job_name=job.name,
            gpu=job.gpu,
            cloud=job.cloud,
            dataset_archive=job.dataset_archive,
            build_archive=job.build_archive,
            result_dir=job.result_dir,
            config=job.config_rel,
            max_cap=job.max_cap,
            enable_sparsity=job.enable_sparsity,
            gut=job.gut,
            terminate_when_done=job.terminate_when_done,
        )

    def _run_job(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        try:
            if job.phase in {"complete", "error"}:
                return
            cfg = self._cfg_for(job)
            run_dir = self.store.workdir(job.id)
            netrc = run_dir / "netrc"
            write_netrc(netrc, cfg.storage)

            if job.dataset_source == "local" and job.phase in {"created", "uploading_dataset"}:
                self._upload_local_dataset(job, cfg, run_dir, netrc)
                job = self.store.get(job_id) or job

            if job.phase in {"created", "uploading_dataset"}:
                self._update(job, phase="waiting_for_pod", message="waiting for GPU")

            if not job.pod_id:
                self._create_pod(job, cfg)
                job = self.store.get(job_id) or job

            if job.pod_id and not job.injected:
                self._inject(job, cfg, run_dir)
                job = self.store.get(job_id) or job

            if job.injected and job.phase not in {"complete", "error"}:
                self._watch(job, cfg)
        except Exception as e:
            log("job", f"{job_id} failed: {e}")
            job = self.store.get(job_id)
            if job and job.phase not in {"complete"}:
                self._update(job, phase="error", error=str(e), message=str(e))

    def _upload_local_dataset(self, job: Job, cfg: AppConfig, run_dir: Path, netrc: Path) -> None:
        self._update(job, phase="uploading_dataset", message="packing dataset")
        src = Path(job.dataset_local).expanduser()
        tar_path = run_dir / "dataset.tar"
        tar_directory(src, tar_path)
        remote = job.dataset_archive or f"lichtfeld-datasets/{job.id}.tar"
        self._update(
            job,
            message=f"uploading {tar_path.stat().st_size:,} bytes",
            dataset_archive=remote,
            dataset_bytes=tar_path.stat().st_size,
        )
        curl_put(cfg.storage, tar_path, remote, netrc)
        self._update(job, dataset_archive=remote, message="dataset uploaded")

    def _create_pod(self, job: Job, cfg: AppConfig) -> None:
        self._update(job, phase="waiting_for_pod", message="waiting for GPU")
        pubkey = ensure_ed25519(cfg.ssh.identity_file, cfg.ssh.public_key_file)
        client = RunpodClient(cfg.runpod.api_key)
        client.ensure_ssh_key(pubkey)

        def on_attempt(msg: str) -> None:
            current = self.store.get(job.id)
            if current:
                self._update(current, message=msg)

        pod = client.create_pod_retry(
            should_stop=self._stop.is_set,
            on_attempt=on_attempt,
            name=job.name,
            image=cfg.runpod.image,
            gpu=job.gpu,
            gpu_count=cfg.runpod.gpu_count,
            cloud=job.cloud,
            disk_gb=cfg.runpod.container_disk_gb,
            volume_gb=cfg.runpod.volume_gb,
            volume_mount=cfg.runpod.volume_mount,
            pubkey=pubkey,
            allowed_cuda=cfg.runpod.allowed_cuda_versions,
        )
        pod_id = str(pod["id"])
        self._update(job, pod_id=pod_id, phase="starting", message=f"pod {pod_id} created")

    def _inject(self, job: Job, cfg: AppConfig, run_dir: Path) -> None:
        if not job.pod_id:
            raise RuntimeError("no pod_id")
        self._update(job, phase="starting", message="waiting for SSH")
        client = RunpodClient(cfg.runpod.api_key)

        def stop(n: int) -> bool:
            return self._ftp_stop(cfg, job, n)

        endpoint = client.wait_ssh(job.pod_id, should_stop=stop)
        if endpoint is None:
            if self._maybe_presumed_complete(job, cfg):
                return
            raise RuntimeError("pod SSH never appeared")
        host, port = endpoint
        self._update(job, ssh_host=host, ssh_port=port)
        ssh_config = run_dir / "ssh_config"
        write_ssh_config(ssh_config, host, port, cfg.ssh.identity_file)
        ssh = Ssh(ssh_config)
        if not ssh.wait_ready(should_stop=stop):
            if self._maybe_presumed_complete(job, cfg):
                return
            raise RuntimeError("SSH never became ready")
        self._ssh[job.id] = ssh

        build_bytes = job.build_bytes or remote_size(cfg.storage, cfg.storage.build_archive)
        dataset_bytes = job.dataset_bytes or remote_size(cfg.storage, cfg.storage.dataset_archive)
        self._update(job, build_bytes=build_bytes, dataset_bytes=dataset_bytes)
        inject_and_start(ssh, cfg, run_dir, job.pod_id, build_bytes, dataset_bytes)
        self._update(job, injected=True, phase="running", last_ssh_ok=time.time(), message="pipeline started")

    def _watch(self, job: Job, cfg: AppConfig) -> None:
        ssh = self._ssh.get(job.id)
        if ssh is None:
            client = RunpodClient(cfg.runpod.api_key)

            def stop(n: int) -> bool:
                return self._ftp_stop(cfg, job, n)

            if job.ssh_host and job.ssh_port:
                host, port = job.ssh_host, job.ssh_port
            elif job.pod_id:
                endpoint = client.wait_ssh(job.pod_id, should_stop=stop)
                if endpoint is None:
                    if self._maybe_presumed_complete(job, cfg):
                        return
                    raise RuntimeError("pod SSH never appeared")
                host, port = endpoint
                self._update(job, ssh_host=host, ssh_port=port)
            else:
                raise RuntimeError("no SSH session")
            ssh_config = self.store.workdir(job.id) / "ssh_config"
            write_ssh_config(ssh_config, host, port, cfg.ssh.identity_file)
            ssh = Ssh(ssh_config)
            if not ssh.wait_ready(should_stop=stop):
                if self._maybe_presumed_complete(job, cfg):
                    return
                raise RuntimeError("SSH never became ready")
            self._ssh[job.id] = ssh
        client = RunpodClient(cfg.runpod.api_key)
        fails = 0
        while not self._stop.is_set():
            job = self.store.get(job.id) or job
            if job.phase == "complete":
                return
            try:
                fields = poll_remote_state(ssh)
                fails = 0
                job.last_ssh_ok = time.time()
                job.stage = fields.get("STAGE", job.stage)
                train = fields.get("TRAIN", "")
                job.message = format_progress(job.stage, fields, train, job.build_bytes, job.dataset_bytes)
                try:
                    job.log_tail = fetch_log_tail(ssh)
                except Exception:
                    pass
                if fields.get("DONE") == "1" or job.stage == "done":
                    self._finish_ok(job, cfg)
                    return
                if fields.get("ERR") == "1":
                    job.phase = "error"
                    job.error = "remote pipeline failed"
                    job.message = job.error
                    try:
                        job.log_tail = fetch_log_tail(ssh)
                    except Exception:
                        pass
                    self._save(job)
                    return
                if job.phase != "running":
                    job.phase = "running"
                self._save(job)
            except Exception as e:
                fails += 1
                job.message = f"connection lost ({e})"
                self._save(job)
                if ftp_check_due(fails) and self._maybe_presumed_complete(job, cfg):
                    return
                if job.pod_id and not client.pod_running(job.pod_id):
                    if self._maybe_presumed_complete(job, cfg):
                        return
                    if job.stage in {"done", "upload"}:
                        self._finish_ok(job, cfg)
                        return
                    self._update(job, phase="error", error="pod disappeared before the job finished")
                    return
                if fails > 1:
                    time.sleep(cfg.progress_interval_seconds)
                    continue
            time.sleep(cfg.progress_interval_seconds)

    def _finish_ok(self, job: Job, cfg: AppConfig, *, presumed: bool = False) -> None:
        job.phase = "complete"
        job.error = None
        done = PRESUMED_COMPLETE_MESSAGE if presumed else "complete"
        job.message = done
        if job.auto_download:
            dest = self.workdir / "results" / job.id
            netrc = self.store.workdir(job.id) / "netrc"
            write_netrc(netrc, cfg.storage)
            try:
                download_result_dir(cfg.storage, job.result_dir, dest, netrc)
                job.local_results = str(dest)
                job.message = f"{done} · downloaded to {dest}"
            except Exception as e:
                job.message = f"{done} · download failed: {e}"
        self._save(job)
        self._ssh.pop(job.id, None)


def _pod_gpu(pod: dict[str, Any]) -> str | None:
    gpu = pod.get("gpu") or pod.get("gpuTypeId") or pod.get("machine")
    if isinstance(gpu, dict):
        return str(gpu.get("id") or gpu.get("displayName") or gpu.get("name") or "")
    if gpu:
        return str(gpu)
    return None
