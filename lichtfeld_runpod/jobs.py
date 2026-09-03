from __future__ import annotations

import json
import random
import shutil
import string
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

PRESUMED_COMPLETE_MESSAGE = "completed (presumably)"


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def default_job_name() -> str:
    """Local date+time plus random letters, e.g. 20260829-001215-kqpwmz."""
    letters = "".join(random.choices(string.ascii_lowercase, k=6))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{letters}"


@dataclass
class Job:
    id: str
    name: str
    phase: str
    gpu: str
    cloud: str
    build_archive: str
    dataset_archive: str
    dataset_source: str
    dataset_local: str
    result_dir: str
    config_rel: str
    auto_download: bool
    terminate_when_done: bool
    max_cap: int | None
    enable_sparsity: bool | None
    gut: bool | None
    extra_args: str = ""
    image: str = ""
    config_local: str = ""
    upload_as_is: bool = False
    pod_id: str | None = None
    ssh_host: str | None = None
    ssh_port: int | None = None
    stage: str = ""
    message: str = ""
    error: str | None = None
    log_tail: str = ""
    last_ssh_ok: float | None = None
    connection_errors: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    local_results: str = ""
    build_bytes: int | None = None
    dataset_bytes: int | None = None
    injected: bool = False
    archived: bool = False
    kind: str = "train"
    git_ref: str = ""
    cuda_arch: str = ""
    repo_url: str = ""
    archive_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, job_id: str) -> Path:
        return self.root / f"{job_id}.json"

    def workdir(self, job_id: str) -> Path:
        d = self.root / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(self, job: Job) -> None:
        job.updated_at = time.time()
        path = self.path(job.id)
        path.write_text(json.dumps(job.to_dict(), indent=2), encoding="utf-8")

    def get(self, job_id: str) -> Job | None:
        path = self.path(job_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return Job.from_dict(data)

    def all(self) -> list[Job]:
        jobs: list[Job] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                jobs.append(Job.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    def delete(self, job_id: str) -> bool:
        """Remove the listing record and job workdir. Does not touch FTP or downloaded results."""
        path = self.path(job_id)
        existed = path.is_file()
        if existed:
            path.unlink()
        work = self.root / job_id
        if work.is_dir():
            shutil.rmtree(work, ignore_errors=True)
        return existed
