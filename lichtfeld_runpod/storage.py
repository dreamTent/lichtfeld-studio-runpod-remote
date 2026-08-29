from __future__ import annotations

import subprocess
import tarfile
from ftplib import FTP, error_perm
from pathlib import Path
from urllib.parse import quote

from .config import StorageConfig
from .host import restrict_secret_file, which_tool, write_text_lf
from .log import log


ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".zip")


def uploaded_dataset_path(src: Path, job_id: str) -> str:
    """FTP path for a local upload: original name plus job id, under lichtfeld-datasets/."""
    name = src.name.strip() or "dataset"
    lower = name.lower()
    suffix = ".tar"
    stem = name
    for ext in ARCHIVE_SUFFIXES:
        if lower.endswith(ext):
            stem = name[: -len(ext)]
            suffix = name[len(name) - len(ext) :]
            break
    stem = stem.strip() or "dataset"
    return f"lichtfeld-datasets/{stem}-{job_id}{suffix}"


def ftp_connect(cfg: StorageConfig) -> FTP:
    ftp = FTP()
    ftp.connect(cfg.host, cfg.ftp_port, timeout=30)
    ftp.login(cfg.user, cfg.password)
    ftp.set_pasv(True)
    ftp.encoding = "utf-8"
    return ftp


def remote_size(cfg: StorageConfig, remote_path: str) -> int | None:
    """Best-effort SIZE. FTP only; SFTP skips (returns None)."""
    if cfg.protocol != "ftp":
        return None
    ftp = ftp_connect(cfg)
    try:
        ftp.voidcmd("TYPE I")
        n = ftp.size(remote_path)
        return int(n) if n is not None else None
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


def ensure_remote_dir(cfg: StorageConfig, remote_dir: str) -> None:
    if cfg.protocol != "ftp":
        return
    ftp = ftp_connect(cfg)
    try:
        parts = [p for p in remote_dir.strip("/").split("/") if p]
        acc = ""
        for p in parts:
            acc = f"{acc}/{p}" if acc else p
            try:
                ftp.mkd(acc)
            except error_perm as e:
                if not str(e).startswith("550"):
                    raise
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


def curl_url(cfg: StorageConfig, remote_path: str) -> str:
    encoded = quote(remote_path.lstrip("/"), safe="/")
    return f"{cfg.curl_scheme}://{cfg.host}:{cfg.transfer_port}/{encoded}"


def _netrc_quote(value: str) -> str:
    """Quote a netrc token. Unquoted, curl splits on whitespace and stops at non-ASCII."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_netrc(path: Path, cfg: StorageConfig) -> None:
    user = _netrc_quote(cfg.user)
    password = _netrc_quote(cfg.password)
    write_text_lf(path, f"machine {cfg.host} login {user} password {password}\n")
    restrict_secret_file(path)


def result_names_look_complete(names: list[str]) -> bool:
    """True if an FTP listing looks like a finished job (REPORT.md is the marker)."""
    bases = {n.rstrip("/").rsplit("/", 1)[-1].lower() for n in names if n and n not in (".", "..")}
    return "report.md" in bases


def results_look_complete(cfg: StorageConfig, remote_dir: str) -> bool:
    if not remote_dir:
        return False
    return result_names_look_complete(verify_uploaded(cfg, remote_dir))


def verify_uploaded(cfg: StorageConfig, remote_dir: str) -> list[str]:
    names: list[str] = []
    if cfg.protocol != "ftp":
        log("ftp", "skip listing (sftp); assuming upload succeeded")
        return names
    ftp = ftp_connect(cfg)
    try:
        ftp.cwd(remote_dir)
        ftp.retrlines("NLST", names.append)
    except Exception as e:
        log("ftp", f"could not list {remote_dir}: {e}")
    finally:
        try:
            ftp.quit()
        except Exception:
            pass
    return names


def list_dir_entries(cfg: StorageConfig, path: str = "") -> list[dict[str, str | bool]]:
    """List one FTP directory. Each item: name, is_dir."""
    if cfg.protocol != "ftp":
        return []
    ftp = ftp_connect(cfg)
    try:
        return _list_entries(ftp, path)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


def list_remote_files(
    cfg: StorageConfig,
    prefix: str = "",
    suffixes: tuple[str, ...] = ARCHIVE_SUFFIXES,
    max_depth: int = 4,
) -> list[str]:
    """Recursively list archive-like files under prefix."""
    if cfg.protocol != "ftp":
        return []
    ftp = ftp_connect(cfg)
    found: list[str] = []
    try:
        _walk(ftp, prefix.strip("/"), suffixes, max_depth, 0, found)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass
    found.sort()
    return found


def curl_put(cfg: StorageConfig, local: Path, remote_path: str, netrc: Path) -> None:
    parent = "/".join(remote_path.strip("/").split("/")[:-1])
    if parent:
        ensure_remote_dir(cfg, parent)
    url = curl_url(cfg, remote_path)
    extra = ["--ftp-pasv"] if cfg.protocol == "ftp" else []
    cmd = [
        which_tool("curl"),
        "--fail",
        "--netrc-file",
        str(netrc),
        "--ftp-create-dirs",
        "--connect-timeout",
        "30",
        "--retry",
        "10",
        "--retry-delay",
        "5",
        *extra,
        "-T",
        str(local),
        url,
    ]
    log("ftp", f"PUT {local} -> {remote_path}")
    subprocess.run(cmd, check=True)


def curl_get_file(cfg: StorageConfig, remote_path: str, local: Path, netrc: Path) -> None:
    local.parent.mkdir(parents=True, exist_ok=True)
    url = curl_url(cfg, remote_path)
    extra = ["--ftp-pasv"] if cfg.protocol == "ftp" else []
    cmd = [
        which_tool("curl"),
        "-C",
        "-",
        "--fail",
        "--netrc-file",
        str(netrc),
        "--connect-timeout",
        "30",
        "--retry",
        "10",
        "--retry-delay",
        "5",
        *extra,
        "--url",
        url,
        "-o",
        str(local),
    ]
    log("ftp", f"GET {remote_path} -> {local}")
    subprocess.run(cmd, check=True)


def tar_directory(src: Path, dest: Path) -> Path:
    src = src.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_file():
        if src != dest:
            dest.write_bytes(src.read_bytes())
        return dest
    if not src.is_dir():
        raise FileNotFoundError(f"dataset path is not a directory: {src}")
    with tarfile.open(dest, "w") as tf:
        tf.add(src, arcname=src.name)
    return dest


def download_result_dir(cfg: StorageConfig, remote_dir: str, dest: Path, netrc: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    names = verify_uploaded(cfg, remote_dir)
    saved: list[str] = []
    for name in names:
        if not name or name in (".", ".."):
            continue
        remote = f"{remote_dir.rstrip('/')}/{name}"
        local = dest / name
        try:
            curl_get_file(cfg, remote, local, netrc)
            saved.append(name)
        except subprocess.CalledProcessError as e:
            log("ftp", f"skip {remote}: {e}")
    return saved


def _list_entries(ftp: FTP, path: str) -> list[dict[str, str | bool]]:
    lines: list[str] = []
    target = path.strip("/") or "."
    try:
        ftp.dir(target, lines.append)
    except Exception:
        return []
    out: list[dict[str, str | bool]] = []
    for line in lines:
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        name = parts[8]
        if name in (".", ".."):
            continue
        out.append({"name": name, "is_dir": line.startswith("d")})
    return out


def _walk(
    ftp: FTP,
    prefix: str,
    suffixes: tuple[str, ...],
    max_depth: int,
    depth: int,
    found: list[str],
) -> None:
    if depth > max_depth:
        return
    for entry in _list_entries(ftp, prefix):
        name = str(entry["name"])
        rel = f"{prefix}/{name}" if prefix else name
        if entry["is_dir"]:
            _walk(ftp, rel, suffixes, max_depth, depth + 1, found)
            continue
        lower = name.lower()
        if any(lower.endswith(s) for s in suffixes):
            found.append(rel)
