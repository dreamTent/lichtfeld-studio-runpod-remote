from __future__ import annotations

import re
import subprocess
import tarfile
import threading
import time
from collections.abc import Callable
from ftplib import FTP, error_perm
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

from .config import StorageConfig
from .host import restrict_secret_file, which_tool, write_text_lf
from .log import log


ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".zip")
UPLOAD_SUFFIX = ".upload"


class UploadAborted(Exception):
    """Raised when an FTP upload is cancelled by the user."""


def is_archive_path(path: Path | str) -> bool:
    name = Path(path).name.lower()
    return any(name.endswith(ext) for ext in ARCHIVE_SUFFIXES)


def staging_remote_path(remote_path: str) -> str:
    """Path used while an FTP transfer is in progress."""
    path = remote_path.rstrip("/")
    if path.endswith(UPLOAD_SUFFIX):
        return path
    return path + UPLOAD_SUFFIX


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


def remote_rename(cfg: StorageConfig, src: str, dest: str, netrc: Path | None = None) -> None:
    """Rename a file or directory on the storage server (after a staged .upload)."""
    src = src.strip("/")
    dest = dest.strip("/")
    log("ftp", f"REN {src} -> {dest}")
    if cfg.protocol == "ftp":
        ftp = ftp_connect(cfg)
        try:
            try:
                ftp.delete(dest)
            except error_perm:
                pass
            ftp.rename(src, dest)
        finally:
            try:
                ftp.quit()
            except Exception:
                pass
        return
    if netrc is None:
        raise ValueError("netrc is required to rename over sftp")
    src_q = src.replace('"', "")
    dest_q = dest.replace('"', "")
    cmd = [
        which_tool("curl"),
        "--fail",
        "--netrc-file",
        str(netrc),
        "-Q",
        f'-rename "{src_q}" "{dest_q}"',
        curl_url(cfg, ""),
    ]
    subprocess.run(cmd, check=True)


def remote_delete(cfg: StorageConfig, remote_path: str, netrc: Path | None = None) -> None:
    """Best-effort delete of a file on the storage server (e.g. leftover .upload)."""
    path = remote_path.strip("/")
    if not path:
        return
    log("ftp", f"DEL {path}")
    if cfg.protocol == "ftp":
        ftp = ftp_connect(cfg)
        try:
            ftp.delete(path)
        except error_perm as e:
            log("ftp", f"DEL skip {path}: {e}")
        finally:
            try:
                ftp.quit()
            except Exception:
                pass
        return
    if netrc is None:
        raise ValueError("netrc is required to delete over sftp")
    path_q = path.replace('"', "")
    cmd = [
        which_tool("curl"),
        "--fail",
        "--netrc-file",
        str(netrc),
        "-Q",
        f'rm "{path_q}"',
        curl_url(cfg, ""),
    ]
    subprocess.run(cmd, check=True)


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


class CurlProgress(NamedTuple):
    uploaded: int
    total: int
    speed: str = ""
    eta: str = ""


_CURL_SIZE_RE = re.compile(r"^([\d.]+)([kMGTP])?$", re.I)
_CURL_METER_RE = re.compile(
    r"^\s*(\d+)\s+(\S+)\s+(\d+)\s+(\S+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$"
)
_CURL_BAR_RE = re.compile(r"([\d.]+)\s*%\s*$")
_SIZE_MULT = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4, "p": 1024**5}


def parse_curl_size(token: str) -> int | None:
    raw = token.strip().replace(",", "")
    if raw in {"0", "--"}:
        return 0
    m = _CURL_SIZE_RE.fullmatch(raw)
    if not m:
        return None
    n = float(m.group(1))
    mult = _SIZE_MULT.get((m.group(2) or "").lower())
    if mult is None:
        return None
    return int(n * mult)


def parse_curl_progress_line(line: str, expected: int | None = None) -> CurlProgress | None:
    text = line.strip()
    if not text or text.startswith("%") or "Total" in text or "Dload" in text:
        return None
    meter = _CURL_METER_RE.match(text)
    if meter:
        xfer_pct = int(meter.group(5))
        uploaded = parse_curl_size(meter.group(6))
        total = expected if expected and expected > 0 else parse_curl_size(meter.group(2))
        if uploaded is None and total:
            uploaded = int(round(total * xfer_pct / 100.0))
        if uploaded is None:
            uploaded = 0
        if not total:
            total = expected or 0
        if total and uploaded > total:
            uploaded = total
        speed = meter.group(12)
        eta = meter.group(11)
        return CurlProgress(uploaded=uploaded, total=total, speed=speed, eta=eta)
    bar = _CURL_BAR_RE.search(text)
    if bar and expected and expected > 0:
        pct = float(bar.group(1))
        uploaded = int(round(expected * min(pct, 100.0) / 100.0))
        return CurlProgress(uploaded=uploaded, total=expected)
    return None


def format_transfer_progress(
    label: str,
    uploaded: int,
    total: int,
    *,
    speed: str = "",
    eta: str = "",
) -> str:
    if total > 0:
        text = f"{label} {uploaded / total * 100:.1f}%  ({uploaded:,}/{total:,} bytes)"
    else:
        text = f"{label} {uploaded:,} bytes"
    extra: list[str] = []
    if speed and speed not in {"0", "--"}:
        extra.append(speed if speed.endswith("/s") else f"{speed}/s")
    if eta and eta not in {"", "--:--:--", "--:--"}:
        extra.append(f"remaining={eta}")
    if extra:
        text += "  " + "  ".join(extra)
    return text


def curl_put(
    cfg: StorageConfig,
    local: Path,
    remote_path: str,
    netrc: Path,
    *,
    on_progress: Callable[[CurlProgress], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    final = remote_path.rstrip("/")
    staging = staging_remote_path(final)
    parent = "/".join(staging.split("/")[:-1])
    if parent:
        ensure_remote_dir(cfg, parent)
    url = curl_url(cfg, staging)
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
    log("ftp", f"PUT {local} -> {staging}")
    if should_stop is not None and should_stop():
        raise UploadAborted("upload aborted")
    expected = local.stat().st_size if local.is_file() else 0
    if on_progress is None and should_stop is None:
        subprocess.run(cmd, check=True)
    else:
        progress_cmd = [cmd[0], "--progress-meter", *cmd[1:]] if on_progress else cmd
        _run_curl_with_progress(progress_cmd, expected, on_progress, should_stop)
    if should_stop is not None and should_stop():
        raise UploadAborted("upload aborted")
    remote_rename(cfg, staging, final, netrc)


def _split_progress_chunks(buf: bytes) -> tuple[list[bytes], bytes]:
    parts = re.split(rb"[\r\n]+", buf)
    if buf.endswith((b"\r", b"\n")):
        return [p for p in parts if p], b""
    return [p for p in parts[:-1] if p], parts[-1] if parts else b""


def _run_curl_with_progress(
    cmd: list[str],
    expected: int,
    on_progress: Callable[[CurlProgress], None] | None,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    stderr = proc.stderr
    assert stderr is not None
    aborted = threading.Event()

    def watch_stop() -> None:
        while proc.poll() is None:
            if should_stop is not None and should_stop():
                aborted.set()
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return
            time.sleep(0.2)

    watcher = threading.Thread(target=watch_stop, daemon=True)
    watcher.start()
    buf = b""
    last_emit = 0.0
    last_msg = ""
    err_tail = b""
    try:
        if on_progress is None:
            proc.wait()
        else:
            while True:
                chunk = stderr.read(512)
                if not chunk:
                    break
                err_tail = (err_tail + chunk)[-4000:]
                buf += chunk
                lines, buf = _split_progress_chunks(buf)
                for raw in lines:
                    parsed = parse_curl_progress_line(raw.decode("utf-8", "replace"), expected)
                    if parsed is None:
                        continue
                    now = time.monotonic()
                    msg = format_transfer_progress(
                        "upload dataset",
                        parsed.uploaded,
                        parsed.total or expected,
                        speed=parsed.speed,
                        eta=parsed.eta,
                    )
                    if msg == last_msg or now - last_emit < 0.5:
                        continue
                    last_emit = now
                    last_msg = msg
                    on_progress(parsed)
            if buf:
                parsed = parse_curl_progress_line(buf.decode("utf-8", "replace"), expected)
                if parsed is not None:
                    on_progress(parsed)
    finally:
        rc = proc.wait()
        stderr.close()
        watcher.join(timeout=1)
    if aborted.is_set() or (should_stop is not None and should_stop()):
        raise UploadAborted("upload aborted")
    if rc != 0:
        extra = err_tail.decode("utf-8", "replace").replace("\r", "\n").strip()[-800:]
        raise subprocess.CalledProcessError(rc, cmd, stderr=extra or None)
    if on_progress is not None and expected > 0:
        on_progress(CurlProgress(uploaded=expected, total=expected))


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
        lower = name.lower()
        if lower.endswith(UPLOAD_SUFFIX):
            continue
        if entry["is_dir"]:
            _walk(ftp, rel, suffixes, max_depth, depth + 1, found)
            continue
        if any(lower.endswith(s) for s in suffixes):
            found.append(rel)
