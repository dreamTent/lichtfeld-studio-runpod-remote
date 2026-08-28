from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from pathlib import Path

from .log import log

IS_WINDOWS = os.name == "nt"


def which_tool(name: str) -> str:
    candidates = [f"{name}.exe", name] if IS_WINDOWS else [name]
    for cand in candidates:
        found = shutil.which(cand)
        if found:
            return found
    hint = (
        "On Windows, enable OpenSSH Client under Settings → Apps → Optional features "
        "and keep curl.exe on PATH (included with Windows 10+)."
        if IS_WINDOWS
        else f"Install {name} and ensure it is on PATH."
    )
    raise FileNotFoundError(f"{name} not found on PATH. {hint}")


def posix_path(path: Path) -> str:
    return path.expanduser().resolve().as_posix()


def write_text_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def restrict_secret_file(path: Path) -> None:
    """Make a secret readable only by the current user (OpenSSH on Windows requires this)."""
    if not IS_WINDOWS:
        path.chmod(0o600)
        return
    user = getpass.getuser()
    r = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(F)"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        log("host", f"could not lock down {path}: {(r.stderr or r.stdout or '').strip()}")
