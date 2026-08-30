from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from .buildspec import (
    DEFAULT_GIT_REF,
    DEFAULT_REPO,
    default_archive_name,
    default_build_folder,
    normalize_cuda_arch,
    normalize_git_ref,
    normalize_repo_url,
)
from .config import AppConfig, ConfigError, config_for_job, load_app_config
from .host import open_in_file_manager
from .jobs import PRESUMED_COMPLETE_MESSAGE, Job, JobStore, default_job_name, new_id
from .log import log
from .orchestrate import fetch_log_tail, format_progress, inject_and_start, poll_remote_state
from .runpod import RunpodClient, RunpodError, pod_is_running, ssh_endpoint
from .sshutil import Ssh, SshError, ensure_ed25519, ftp_check_due, write_ssh_config
from .status import HEARTBEAT_STALE_SECONDS, heartbeat_ok, job_indicator, pod_indicator
from .localfs import ensure_datasets_dir
from .storage import (
    CurlProgress,
    UploadAborted,
    curl_put,
    download_result_dir,
    format_transfer_progress,
    is_archive_path,
    remote_delete,
    remote_size,
    results_look_complete,
    staging_remote_path,
    tar_directory,
    uploaded_dataset_path,
    write_netrc,
)


ACTIVE_PHASES = {"created", "uploading_dataset", "waiting_for_pod", "starting", "running"}
UPLOAD_ABORT_PHASES = {"created", "uploading_dataset"}
POLL_SECONDS = 30


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
        self._dropped: set[str] = set()
        self._halted: set[str] = set()
        self._next_check: dict[str, tuple[float, str]] = {}
        self._next_pod_list_at: float | None = None

    def start(self) -> None:
        ensure_datasets_dir(self.workdir)
        self._reconcile_from_ftp()
        for job in self.store.all():
            if job.phase in ACTIVE_PHASES:
                self._spawn(job.id)
        self._loop_thread = threading.Thread(target=self._loop, name="job-manager", daemon=True)
        self._loop_thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            sessions = list(self._ssh.values())
            self._ssh.clear()
        for ssh in sessions:
            ssh.close()

    def submit(self, spec: dict[str, Any]) -> Job:
        now = time.time()
        job_id = new_id()
        kind = str(spec.get("kind") or "train").strip().lower() or "train"
        if kind not in {"train", "build"}:
            raise ValueError("kind must be train or build")
        name = str(spec.get("name") or "").strip() or default_job_name()
        gpu = str(spec.get("gpu") or "NVIDIA L40S")
        git_ref = ""
        cuda_arch = ""
        repo_url = ""
        archive_name = ""
        if kind == "build":
            git_ref = normalize_git_ref(str(spec.get("git_ref") or DEFAULT_GIT_REF))
            cuda_arch = normalize_cuda_arch(str(spec.get("cuda_arch") or ""), gpu)
            repo_url = normalize_repo_url(str(spec.get("repo_url") or DEFAULT_REPO))
            archive_name = str(spec.get("archive_name") or "").strip() or default_archive_name(
                git_ref, gpu, cuda_arch
            )
            if "/" in archive_name or archive_name.startswith("."):
                raise ValueError("archive name must be a file name, not a path")
            result_dir = str(spec.get("result_dir") or "").strip() or default_build_folder(
                git_ref, gpu, cuda_arch
            )
            build_archive = f"{result_dir.strip('/')}/{archive_name}"
            dataset_source = "ftp"
            dataset_archive = ""
            dataset_local = ""
        else:
            result_dir = str(spec.get("result_dir") or f"lichtfeld-results/{name}-{job_id}")
            build_archive = str(spec.get("build_archive") or "")
            dataset_source = str(spec.get("dataset_source") or "ftp")
            dataset_archive = str(spec.get("dataset_archive") or "")
            dataset_local = str(spec.get("dataset_local") or "")
        job = Job(
            id=job_id,
            name=name,
            phase="created",
            gpu=gpu,
            cloud=str(spec.get("cloud") or "SECURE").upper(),
            build_archive=build_archive,
            dataset_archive=dataset_archive,
            dataset_source=dataset_source,
            dataset_local=dataset_local,
            result_dir=result_dir.strip("/"),
            config_rel=str(spec.get("config") or "").strip(),
            auto_download=bool(spec.get("auto_download", False)),
            terminate_when_done=bool(spec.get("terminate_when_done", True)),
            max_cap=spec.get("max_cap"),
            enable_sparsity=spec.get("enable_sparsity"),
            gut=spec.get("gut"),
            image=str(spec.get("image") or "").strip(),
            config_local=str(spec.get("config_local") or "").strip(),
            upload_as_is=bool(spec.get("upload_as_is", False)),
            message="created",
            created_at=now,
            updated_at=now,
            kind=kind,
            git_ref=git_ref,
            cuda_arch=cuda_arch,
            repo_url=repo_url,
            archive_name=archive_name,
        )
        if kind == "train":
            if job.dataset_source == "ftp" and not job.dataset_archive:
                raise ValueError("select a dataset archive on the FTP server")
            if job.dataset_source == "local" and not job.dataset_local:
                raise ValueError("select a local dataset folder or archive")
            if job.dataset_source == "local":
                local = Path(job.dataset_local).expanduser()
                if not local.exists():
                    raise ValueError(f"local dataset not found: {local}")
            if job.config_local:
                cfg_path = Path(job.config_local).expanduser()
                if not cfg_path.is_file():
                    raise ValueError(f"LichtFeld config not found: {cfg_path}")
            if not job.build_archive:
                raise ValueError("select a LichtFeld build from the FTP server")
        self._save(job)
        self._spawn(job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        return self.store.get(job_id)

    def jobs(self) -> list[Job]:
        return self.store.all()

    def set_archived(self, job_id: str, archived: bool) -> Job:
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return self._update(job, archived=archived)

    def delete_listing(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        with self._lock:
            self._dropped.add(job_id)
            ssh = self._ssh.pop(job_id, None)
        if ssh is not None:
            ssh.close()
        self._clear_next_check(job_id)
        self.store.delete(job_id)
        log("job", f"{job_id} removed from listing (FTP/local results kept)")

    def discard_pod(self, pod_id: str) -> dict[str, Any]:
        pod_id = str(pod_id or "").strip()
        if not pod_id:
            raise ValueError("no pod id")
        jobs = [j for j in self.store.all() if j.pod_id == pod_id]
        for job in jobs:
            self._halt_job(job.id)
        try:
            cfg = load_app_config(self.workdir)
            RunpodClient(cfg.runpod.api_key).terminate(pod_id)
        except Exception:
            for job in jobs:
                if job.id in self._dropped:
                    continue
                self._unhalt_job(job.id)
                stored = self.store.get(job.id)
                if stored and stored.phase in ACTIVE_PHASES:
                    self._spawn(stored.id)
            raise
        log("pod", f"{pod_id} discarded")
        for job in jobs:
            stored = self.store.get(job.id)
            if stored is None:
                continue
            if stored.phase != "complete":
                self._update(stored, phase="error", error="pod discarded", message="pod discarded")
            else:
                self._clear_next_check(stored.id)
        with self._lock:
            self._api_pods = [p for p in self._api_pods if str(p.get("id") or "") != pod_id]
        self._refresh_pods()
        return {"ok": True, "id": pod_id}

    def abort_upload(self, job_id: str) -> Job:
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.phase not in UPLOAD_ABORT_PHASES:
            raise ValueError(f"cannot abort upload in phase {job.phase}")
        self._halt_job(job_id)
        job = self.store.get(job_id) or job
        remote = (job.dataset_archive or "").strip()
        if remote:
            try:
                cfg = load_app_config(self.workdir)
                netrc = self.store.workdir(job.id) / "netrc"
                remote_delete(
                    cfg.storage,
                    staging_remote_path(remote),
                    netrc if netrc.is_file() else None,
                )
            except Exception as e:
                log("ftp", f"abort cleanup: {e}")
        return self._update(job, phase="error", error="upload aborted", message="upload aborted")

    def local_results_dir(self, job: Job) -> Path | None:
        """Return the downloaded results folder if this completed job has one on disk."""
        if job.phase != "complete":
            return None
        expected = (self.workdir / "results" / job.id).resolve()
        recorded = (job.local_results or "").strip()
        candidates: list[Path] = []
        if recorded:
            candidates.append(Path(recorded).expanduser())
        candidates.append(expected)
        workdir = self.workdir.resolve()
        seen: set[Path] = set()
        for path in candidates:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            if not resolved.is_dir():
                continue
            try:
                resolved.relative_to(workdir)
            except ValueError:
                continue
            return resolved
        return None

    def open_local_results(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.phase != "complete":
            raise ValueError("results are only available after the job succeeds")
        dest = self.local_results_dir(job)
        if dest is None:
            raise FileNotFoundError("no local results folder for this job")
        open_in_file_manager(dest)
        return {"ok": True, "id": job.id, "path": str(dest)}

    def _halt_job(self, job_id: str) -> None:
        with self._lock:
            self._halted.add(job_id)
            ssh = self._ssh.pop(job_id, None)
        if ssh is not None:
            ssh.close()
        self._clear_next_check(job_id)

    def _unhalt_job(self, job_id: str) -> None:
        with self._lock:
            self._halted.discard(job_id)

    def _job_should_stop(self, job_id: str) -> bool:
        if job_id in self._dropped or job_id in self._halted:
            return True
        stored = self.store.get(job_id)
        return stored is None or stored.phase in {"complete", "error"}

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
        with self._lock:
            pending = dict(self._next_check)
            next_pod_list_at = self._next_pod_list_at
        for job in jobs:
            next_at, kind = pending.get(job.id, (None, None))
            job_rows.append(
                {
                    **job.to_dict(),
                    "color": job_indicator(job.phase, pod_color_by_job.get(job.id) if job.pod_id else None),
                    "next_check_at": next_at,
                    "next_check_kind": kind,
                    "local_results_ready": self.local_results_dir(job) is not None,
                }
            )
        return {"jobs": job_rows, "pods": pod_rows, "next_pod_list_at": next_pod_list_at}

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                self._next_pod_list_at = None
            try:
                self._refresh_pods()
            except Exception as e:
                log("manager", f"list pods: {e}")
            with self._lock:
                self._next_pod_list_at = time.time() + POLL_SECONDS
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
        if self._job_should_stop(job.id):
            return True
        stored = self.store.get(job.id)
        if stored is None:
            return True
        if not ftp_check_due(attempt):
            return False
        return self._maybe_presumed_complete(stored, cfg)

    def reload_job(self, job_id: str) -> Job:
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        self._refresh_pods()
        if job.phase == "complete":
            return job
        cfg = self._cfg_for(job)
        if job.ssh_host or self._ssh.get(job.id):
            self._probe_ssh(job, cfg)
        job = self.store.get(job_id) or job
        if job.phase != "complete" and job.result_dir:
            self._maybe_presumed_complete(job, cfg)
        return self.store.get(job_id) or job

    def _ssh_session(self, job: Job, cfg: AppConfig) -> Ssh | None:
        existing = self._ssh.get(job.id)
        if existing is not None:
            return existing
        if not (job.ssh_host and job.ssh_port):
            return None
        ssh_config = self.store.workdir(job.id) / "ssh_config"
        write_ssh_config(ssh_config, job.ssh_host, job.ssh_port, cfg.ssh.identity_file)
        ssh = Ssh(ssh_config)
        self._ssh[job.id] = ssh
        return ssh

    def _probe_ssh(self, job: Job, cfg: AppConfig) -> bool:
        ssh = self._ssh_session(job, cfg)
        if ssh is None:
            return False
        try:
            fields = poll_remote_state(ssh, timeout=20)
        except Exception as e:
            log("ssh", f"{job.id} reload poll failed: {e}")
            self._note_connection_error(job)
            return False
        job = self.store.get(job.id) or job
        self._on_poll_ok(job, fields, cfg, ssh)
        return True

    def _note_connection_error(self, job: Job) -> Job:
        stored = self.store.get(job.id) or job
        stored.connection_errors = int(stored.connection_errors or 0) + 1
        stored.message = _with_connection_error(stored.message)
        self._save(stored)
        return stored

    def _on_poll_ok(self, job: Job, fields: dict[str, str], cfg: AppConfig, ssh: Ssh | None) -> bool:
        """Apply a successful SSH poll. Returns True if the caller should stop watching."""
        job.connection_errors = 0
        job.last_ssh_ok = time.time()
        job.stage = fields.get("STAGE", job.stage)
        train = fields.get("TRAIN", "")
        job.message = format_progress(job.stage, fields, train, job.build_bytes, job.dataset_bytes)
        if ssh is not None:
            try:
                job.log_tail = fetch_log_tail(ssh)
            except Exception:
                pass
        if fields.get("DONE") == "1" or job.stage == "done":
            self._finish_ok(job, cfg)
            return True
        if fields.get("ERR") == "1":
            job.phase = "error"
            job.error = "remote pipeline failed"
            job.message = job.error
            if ssh is not None:
                try:
                    job.log_tail = fetch_log_tail(ssh)
                except Exception:
                    pass
            self._save(job)
            self._clear_next_check(job.id)
            return True
        pid_ok = fields.get("PIDOK") == "1"
        exit_code = fields.get("EXIT", "")
        if not pid_ok and job.stage not in {"done", "upload", "report"}:
            job.phase = "error"
            job.error = f"remote process exited (rc={exit_code or '?'})"
            job.message = job.error
            if ssh is not None:
                try:
                    job.log_tail = fetch_log_tail(ssh)
                except Exception:
                    pass
            self._save(job)
            self._clear_next_check(job.id)
            return True
        if job.phase != "running":
            job.phase = "running"
        self._save(job)
        return False

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

    def _set_next_check(self, job_id: str, seconds: float, kind: str) -> None:
        with self._lock:
            self._next_check[job_id] = (time.time() + float(seconds), kind)

    def _clear_next_check(self, job_id: str) -> None:
        with self._lock:
            self._next_check.pop(job_id, None)

    def _wait_next(self, job_id: str, seconds: float, kind: str) -> None:
        self._set_next_check(job_id, seconds, kind)
        self._stop.wait(seconds)

    def _save(self, job: Job) -> None:
        if job.id in self._dropped:
            return
        self.store.save(job)

    def _update(self, job: Job, **fields: Any) -> Job:
        if job.id in self._halted and fields.get("phase") not in {"error", "complete"}:
            stored = self.store.get(job.id)
            return stored or job
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
            image=job.image,
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
        if job is None or self._job_should_stop(job_id):
            return
        try:
            if job.phase in {"complete", "error"}:
                return
            cfg = self._cfg_for(job)
            run_dir = self.store.workdir(job.id)
            netrc = run_dir / "netrc"
            write_netrc(netrc, cfg.storage)

            if job.kind != "build" and job.dataset_source == "local" and job.phase in {"created", "uploading_dataset"}:
                self._upload_local_dataset(job, cfg, run_dir, netrc)
                job = self.store.get(job_id) or job
                cfg = self._cfg_for(job)
            if self._job_should_stop(job_id):
                return

            if job.phase in {"created", "uploading_dataset"}:
                self._update(job, phase="waiting_for_pod", message="waiting for GPU")

            if not job.pod_id:
                self._create_pod(job, cfg)
                job = self.store.get(job_id) or job
            if self._job_should_stop(job_id):
                return

            if job.pod_id and not job.injected:
                job = self.store.get(job_id) or job
                cfg = self._cfg_for(job)
                self._inject(job, cfg, run_dir)
                job = self.store.get(job_id) or job
            if self._job_should_stop(job_id):
                return

            if job.injected and job.phase not in {"complete", "error"}:
                self._watch(job, cfg)
        except UploadAborted:
            log("job", f"{job_id} upload aborted")
            if job_id in self._dropped or job_id in self._halted:
                return
            job = self.store.get(job_id)
            if job and job.phase not in {"complete"}:
                self._update(job, phase="error", error="upload aborted", message="upload aborted")
        except Exception as e:
            log("job", f"{job_id} failed: {e}")
            if job_id in self._dropped or job_id in self._halted:
                return
            job = self.store.get(job_id)
            if job and job.phase not in {"complete"}:
                msg = _with_connection_error(job.message) if isinstance(e, SshError) else str(e)
                extra: dict[str, Any] = {}
                if isinstance(e, SshError):
                    extra["connection_errors"] = int(job.connection_errors or 0) + 1
                self._update(job, phase="error", error=str(e), message=msg, **extra)
        finally:
            self._clear_next_check(job_id)

    def _upload_local_dataset(self, job: Job, cfg: AppConfig, run_dir: Path, netrc: Path) -> None:
        self._update(job, phase="uploading_dataset", message="packing dataset")
        src = Path(job.dataset_local).expanduser()
        as_is = bool(job.upload_as_is) and src.is_file() and is_archive_path(src)
        if as_is:
            local = src
        else:
            tar_path = run_dir / "dataset.tar"
            tar_directory(src, tar_path)
            local = tar_path
        if self._job_should_stop(job.id):
            raise UploadAborted("upload aborted")
        remote = uploaded_dataset_path(src, job.id)
        total = local.stat().st_size
        self._update(
            job,
            message=format_transfer_progress("upload dataset", 0, total),
            dataset_archive=remote,
            dataset_bytes=total,
            stage="uploading_dataset",
        )

        def on_progress(prog: CurlProgress) -> None:
            msg = format_transfer_progress(
                "upload dataset",
                prog.uploaded,
                prog.total or total,
                speed=prog.speed,
                eta=prog.eta,
            )
            self._update(job, message=msg, log_tail=msg)
            log("ftp", msg)

        curl_put(
            cfg.storage,
            local,
            remote,
            netrc,
            on_progress=on_progress,
            should_stop=lambda: self._job_should_stop(job.id),
        )
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
            should_stop=lambda: self._stop.is_set() or self._job_should_stop(job.id),
            on_attempt=on_attempt,
            on_wait=lambda delay: self._set_next_check(job.id, delay, "retry"),
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

        def on_wait(delay: float) -> None:
            self._set_next_check(job.id, delay, "retry")

        endpoint = client.wait_ssh(job.pod_id, should_stop=stop, on_wait=on_wait)
        if endpoint is None:
            if self._job_should_stop(job.id):
                return
            raise RuntimeError("pod SSH never appeared")
        host, port = endpoint
        job = self.store.get(job.id) or job
        if self._job_should_stop(job.id):
            return
        self._update(job, ssh_host=host, ssh_port=port)
        ssh_config = run_dir / "ssh_config"
        write_ssh_config(ssh_config, host, port, cfg.ssh.identity_file)
        ssh = Ssh(ssh_config)
        if not ssh.wait_ready(should_stop=stop, on_wait=on_wait):
            if self._job_should_stop(job.id):
                return
            raise RuntimeError("SSH never became ready")
        job = self.store.get(job.id) or job
        if self._job_should_stop(job.id):
            return
        self._ssh[job.id] = ssh
        self._clear_next_check(job.id)

        if job.kind == "build":
            inject_and_start(
                ssh,
                cfg,
                run_dir,
                job.pod_id,
                None,
                None,
                kind="build",
                git_ref=job.git_ref,
                cuda_arch=job.cuda_arch,
                repo_url=job.repo_url,
                archive_name=job.archive_name,
                app_workdir=self.workdir,
            )
            self._update(job, injected=True, phase="running", last_ssh_ok=time.time(), message="build started")
            return

        build_bytes = job.build_bytes or remote_size(cfg.storage, cfg.storage.build_archive)
        dataset_bytes = job.dataset_bytes or remote_size(cfg.storage, cfg.storage.dataset_archive)
        self._update(job, build_bytes=build_bytes, dataset_bytes=dataset_bytes)
        if job.config_local:
            src = Path(job.config_local).expanduser()
            if not src.is_file():
                raise FileNotFoundError(f"LichtFeld config not found: {src}")
            (run_dir / "lichtfeld-config.json").write_bytes(src.read_bytes())
        inject_and_start(ssh, cfg, run_dir, job.pod_id, build_bytes, dataset_bytes)
        self._update(job, injected=True, phase="running", last_ssh_ok=time.time(), message="pipeline started")

    def _watch(self, job: Job, cfg: AppConfig) -> None:
        ssh = self._ssh.get(job.id)
        if ssh is None:
            client = RunpodClient(cfg.runpod.api_key)

            def stop(n: int) -> bool:
                return self._ftp_stop(cfg, job, n)

            def on_wait(delay: float) -> None:
                self._set_next_check(job.id, delay, "retry")

            if job.ssh_host and job.ssh_port:
                host, port = job.ssh_host, job.ssh_port
            elif job.pod_id:
                endpoint = client.wait_ssh(job.pod_id, should_stop=stop, on_wait=on_wait)
                if endpoint is None:
                    if self._job_should_stop(job.id):
                        return
                    raise RuntimeError("pod SSH never appeared")
                job = self.store.get(job.id) or job
                if self._job_should_stop(job.id):
                    return
                host, port = endpoint
                self._update(job, ssh_host=host, ssh_port=port)
            else:
                raise RuntimeError("no SSH session")
            ssh_config = self.store.workdir(job.id) / "ssh_config"
            write_ssh_config(ssh_config, host, port, cfg.ssh.identity_file)
            ssh = Ssh(ssh_config)
            if not ssh.wait_ready(should_stop=stop, on_wait=on_wait):
                if self._job_should_stop(job.id):
                    return
                raise RuntimeError("SSH never became ready")
            job = self.store.get(job.id) or job
            if self._job_should_stop(job.id):
                return
            self._ssh[job.id] = ssh
        client = RunpodClient(cfg.runpod.api_key)
        while not self._stop.is_set():
            if self._job_should_stop(job.id):
                return
            stored = self.store.get(job.id)
            if stored is None:
                return
            job = stored
            if job.phase == "complete":
                return
            kind = "poll"
            try:
                fields = poll_remote_state(ssh)
                if self._job_should_stop(job.id):
                    return
                if self._on_poll_ok(job, fields, cfg, ssh):
                    return
            except Exception as e:
                if self._job_should_stop(job.id):
                    return
                kind = "retry"
                log("ssh", f"{job.id} poll failed: {e}")
                job = self._note_connection_error(job)
                if ftp_check_due(job.connection_errors) and self._maybe_presumed_complete(job, cfg):
                    return
                if job.pod_id and not client.pod_running(job.pod_id):
                    if self._maybe_presumed_complete(job, cfg):
                        return
                    if job.stage in {"done", "upload"}:
                        self._finish_ok(job, cfg)
                        return
                    self._update(job, phase="error", error="pod disappeared before the job finished")
                    return
            self._wait_next(job.id, cfg.progress_interval_seconds, kind)

    def _finish_ok(self, job: Job, cfg: AppConfig, *, presumed: bool = False) -> None:
        job.phase = "complete"
        job.error = None
        job.connection_errors = 0
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
        self._clear_next_check(job.id)
        ssh = self._ssh.pop(job.id, None)
        if ssh is not None:
            ssh.close()


CONNECTION_ERROR_NOTE = "connection error"


def _with_connection_error(message: str) -> str:
    base = message or ""
    marker = f" · {CONNECTION_ERROR_NOTE}"
    idx = base.find(marker)
    if idx >= 0:
        base = base[:idx]
    elif base.strip() == CONNECTION_ERROR_NOTE or base.startswith(f"{CONNECTION_ERROR_NOTE} ·"):
        base = ""
    if base:
        return f"{base} · {CONNECTION_ERROR_NOTE}"
    return CONNECTION_ERROR_NOTE


def _pod_gpu(pod: dict[str, Any]) -> str | None:
    gpu = pod.get("gpu") or pod.get("gpuTypeId") or pod.get("machine")
    if isinstance(gpu, dict):
        return str(gpu.get("id") or gpu.get("displayName") or gpu.get("name") or "")
    if gpu:
        return str(gpu)
    return None
