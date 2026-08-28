from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

EXAMPLE_YAML = Path(__file__).resolve().parent.parent / "config.example.yaml"


class ConfigError(Exception):
    pass


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ if the key is not already set."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            found = os.environ.get(name)
            if found is not None:
                return found
            if default is not None:
                return default
            raise ConfigError(f"missing environment variable {name}")

        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _require(d: dict, *keys: str) -> Any:
    cur: Any = d
    path = []
    for k in keys:
        path.append(k)
        if not isinstance(cur, dict) or k not in cur:
            raise ConfigError(f"config missing {'.'.join(path)}")
        cur = cur[k]
    return cur


@dataclass
class StorageConfig:
    host: str
    user: str
    password: str
    protocol: str
    ftp_port: int
    sftp_port: int
    dataset_archive: str
    build_archive: str
    result_dir: str

    @property
    def transfer_port(self) -> int:
        return self.ftp_port if self.protocol == "ftp" else self.sftp_port

    @property
    def curl_scheme(self) -> str:
        return "ftp" if self.protocol == "ftp" else "sftp"


@dataclass
class RunpodConfig:
    api_key: str
    gpu: str
    gpu_count: int
    cloud: str
    image: str
    container_disk_gb: int
    volume_gb: int
    volume_mount: str
    allowed_cuda_versions: list[str]


@dataclass
class SshConfig:
    identity_file: Path
    public_key_file: Path


@dataclass
class LichtfeldConfig:
    config: str
    max_cap: int | None
    enable_sparsity: bool
    gut: bool
    headless: bool
    extra_args: list[str]
    iterations: int | None
    strategy: str | None


@dataclass
class AppConfig:
    job_name: str
    progress_interval_seconds: int
    terminate_when_done: bool
    runpod: RunpodConfig
    ssh: SshConfig
    storage: StorageConfig
    lichtfeld: LichtfeldConfig
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


def load_config(config_path: Path, env_path: Path | None = None) -> AppConfig:
    if env_path is None:
        env_path = config_path.parent / ".env"
    load_dotenv(env_path)
    if not config_path.is_file():
        raise ConfigError(f"config not found: {config_path}")
    data = _expand(yaml.safe_load(config_path.read_text(encoding="utf-8")) or {})

    job = data.get("job") or {}
    rp = _require(data, "runpod")
    ssh = data.get("ssh") or {}
    st = _require(data, "storage")
    lf = data.get("lichtfeld") or {}

    protocol = str(st.get("protocol") or "ftp").lower()
    if protocol not in {"ftp", "sftp"}:
        raise ConfigError("storage.protocol must be ftp or sftp")

    identity = Path(ssh.get("identity_file") or "~/.ssh/runpod_ed25519").expanduser()
    pubkey = Path(ssh.get("public_key_file") or str(identity) + ".pub").expanduser()

    gpu_count = int(rp.get("gpu_count") or 1)
    if gpu_count != 1:
        raise ConfigError("gpu_count must be 1 (one dedicated GPU, not a MIG slice or pack)")

    extra = lf.get("extra_args") or []
    if not isinstance(extra, list):
        raise ConfigError("lichtfeld.extra_args must be a list of strings")

    cfg = AppConfig(
        job_name=str(job.get("name") or "lichtfeld-job"),
        progress_interval_seconds=int(job.get("progress_interval_seconds") or 15),
        terminate_when_done=bool(job.get("terminate_when_done", True)),
        runpod=RunpodConfig(
            api_key=str(_require(rp, "api_key")).strip(),
            gpu=str(_require(rp, "gpu")),
            gpu_count=gpu_count,
            cloud=str(rp.get("cloud") or "SECURE").upper(),
            image=str(rp.get("image") or "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"),
            container_disk_gb=int(rp.get("container_disk_gb") or 50),
            volume_gb=int(rp.get("volume_gb") or 150),
            volume_mount=str(rp.get("volume_mount") or "/workspace"),
            allowed_cuda_versions=[str(v) for v in (rp.get("allowed_cuda_versions") or [])],
        ),
        ssh=SshConfig(identity_file=identity, public_key_file=pubkey),
        storage=StorageConfig(
            host=str(_require(st, "host")).strip(),
            user=str(_require(st, "user")).strip(),
            password=str(_require(st, "password")),
            protocol=protocol,
            ftp_port=int(st.get("ftp_port") or 21),
            sftp_port=int(st.get("sftp_port") or 22),
            dataset_archive=str(_require(st, "dataset_archive")),
            build_archive=str(_require(st, "build_archive")),
            result_dir=str(_require(st, "result_dir")).strip("/"),
        ),
        lichtfeld=LichtfeldConfig(
            config=str(lf.get("config") or "").strip(),
            max_cap=int(lf["max_cap"]) if lf.get("max_cap") not in (None, "") else None,
            enable_sparsity=bool(lf.get("enable_sparsity", True)),
            gut=bool(lf.get("gut", True)),
            headless=bool(lf.get("headless", True)),
            extra_args=[str(a) for a in extra],
            iterations=int(lf["iterations"]) if lf.get("iterations") not in (None, "") else None,
            strategy=str(lf["strategy"]) if lf.get("strategy") else None,
        ),
        raw=data,
    )
    protocol_env = os.environ.get("STORAGE_PROTOCOL", "").strip().lower()
    if protocol_env in {"ftp", "sftp"}:
        cfg = replace(cfg, storage=replace(cfg.storage, protocol=protocol_env))
    if not cfg.runpod.api_key or cfg.runpod.api_key.startswith("rpa_your"):
        raise ConfigError("set RUNPOD_API_KEY in .env (or runpod.api_key in config.yaml)")
    if not cfg.storage.password or cfg.storage.password.startswith("your_password"):
        raise ConfigError("set SFTP_PASSWORD in .env")
    return cfg


def load_app_config(workdir: Path) -> AppConfig:
    env_path = workdir / ".env"
    cfg_path = workdir / "config.yaml"
    if not cfg_path.is_file():
        cfg_path = EXAMPLE_YAML
    return load_config(cfg_path, env_path)


def config_for_job(base: AppConfig, **overrides: Any) -> AppConfig:
    """Overlay job-form fields onto a loaded AppConfig."""
    storage = base.storage
    runpod = base.runpod
    lichtfeld = base.lichtfeld
    if "gpu" in overrides and overrides["gpu"]:
        runpod = replace(runpod, gpu=str(overrides["gpu"]))
    if "cloud" in overrides and overrides["cloud"]:
        runpod = replace(runpod, cloud=str(overrides["cloud"]).upper())
    if "dataset_archive" in overrides and overrides["dataset_archive"]:
        storage = replace(storage, dataset_archive=str(overrides["dataset_archive"]))
    if "build_archive" in overrides and overrides["build_archive"]:
        storage = replace(storage, build_archive=str(overrides["build_archive"]))
    if "result_dir" in overrides and overrides["result_dir"]:
        storage = replace(storage, result_dir=str(overrides["result_dir"]).strip("/"))
    lf_kw: dict[str, Any] = {}
    if "config" in overrides:
        lf_kw["config"] = str(overrides["config"] or "").strip()
    if "max_cap" in overrides:
        lf_kw["max_cap"] = overrides["max_cap"]
    if "enable_sparsity" in overrides:
        lf_kw["enable_sparsity"] = bool(overrides["enable_sparsity"])
    if "gut" in overrides:
        lf_kw["gut"] = bool(overrides["gut"])
    if lf_kw:
        lichtfeld = replace(lichtfeld, **lf_kw)
    return replace(
        base,
        job_name=str(overrides.get("job_name") or base.job_name),
        terminate_when_done=bool(overrides.get("terminate_when_done", base.terminate_when_done)),
        runpod=runpod,
        storage=storage,
        lichtfeld=lichtfeld,
    )


def read_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.is_file():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip().strip("'").strip('"')
    return data


def write_env_file(path: Path, updates: dict[str, str]) -> None:
    existing = read_env_file(path)
    existing.update({k: v for k, v in updates.items() if v is not None})
    order = ["RUNPOD_API_KEY", "SFTP_HOST", "SFTP_USER", "SFTP_PASSWORD", "STORAGE_PROTOCOL"]
    keys = list(dict.fromkeys(order + list(existing)))
    lines = [
        "# Secrets for lichtfeld-runpod. chmod 600. Do not commit.",
        "",
    ]
    for key in keys:
        if key not in existing:
            continue
        lines.append(f"{key}={existing[key]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    for key, value in updates.items():
        if value:
            os.environ[key] = value


def mask_secret(value: str, visible: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible:
        return "••••"
    return "••••" + value[-visible:]
