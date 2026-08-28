from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path

from .log import log


class SshError(Exception):
    pass


def ensure_ed25519(identity: Path, pubkey: Path) -> str:
    identity.parent.mkdir(parents=True, exist_ok=True)
    if not identity.is_file():
        log("ssh", f"generating {identity}")
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(identity), "-C", "lichtfeld-runpod"],
            check=True,
            capture_output=True,
        )
    if not pubkey.is_file():
        pub = subprocess.check_output(["ssh-keygen", "-y", "-f", str(identity)], text=True).strip()
        pubkey.write_text(pub + "\n", encoding="utf-8")
        pubkey.chmod(0o644)
    identity.chmod(0o600)
    return pubkey.read_text(encoding="utf-8").strip()


def write_ssh_config(path: Path, host: str, port: int, identity: Path) -> None:
    """ControlPath must stay under the ~108-byte Unix socket limit; job dirs are too long."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""Host runpod
    HostName {host}
    Port {port}
    User root
    IdentityFile {identity}
    IdentitiesOnly yes
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
    ServerAliveInterval 30
    ServerAliveCountMax 10
    ConnectTimeout 20
    ControlMaster auto
    ControlPath /tmp/lf-ssh-%C
    ControlPersist 10m
""",
        encoding="utf-8",
    )
    path.chmod(0o600)


class Ssh:
    def __init__(self, config_file: Path) -> None:
        self.config_file = config_file

    def run(self, remote: str, check: bool = True, timeout: int | None = 120) -> subprocess.CompletedProcess[str]:
        cmd = ["ssh", "-F", str(self.config_file), "runpod", remote]
        return subprocess.run(cmd, check=check, text=True, capture_output=True, timeout=timeout)

    def check_output(self, remote: str, timeout: int | None = 120) -> str:
        r = self.run(remote, check=True, timeout=timeout)
        return r.stdout

    def put(self, local: Path, remote: str) -> None:
        subprocess.run(
            ["scp", "-F", str(self.config_file), str(local), f"runpod:{remote}"],
            check=True,
            capture_output=True,
        )

    def put_text(self, text: str, remote: str, mode: str = "644") -> None:
        quoted = shlex.quote(text)
        self.run(f"umask 077; printf %s {quoted} > {shlex.quote(remote)}; chmod {mode} {shlex.quote(remote)}")

    def wait_ready(self, tries: int = 40) -> None:
        last = ""
        for i in range(tries):
            try:
                r = self.run("echo SSH_OK && hostname", check=False, timeout=20)
                if r.returncode == 0 and "SSH_OK" in (r.stdout or ""):
                    host = (r.stdout or "").splitlines()[-1].strip()
                    log("ssh", f"ready ({host})")
                    return
                err = (r.stderr or r.stdout or f"exit {r.returncode}").strip()
                last = err[-800:]
            except subprocess.TimeoutExpired as e:
                last = str(e)
            tail = last.splitlines()[-1] if last else ""
            log("ssh", f"retry {i + 1}/{tries} {tail}")
            time.sleep(2)
        raise SshError(f"SSH never became ready: {last}")
