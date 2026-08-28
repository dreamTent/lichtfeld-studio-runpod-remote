from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path

from .host import IS_WINDOWS, posix_path, restrict_secret_file, which_tool, write_text_lf
from .log import log


class SshError(Exception):
    pass


def ensure_ed25519(identity: Path, pubkey: Path) -> str:
    identity.parent.mkdir(parents=True, exist_ok=True)
    keygen = which_tool("ssh-keygen")
    if not identity.is_file():
        log("ssh", f"generating {identity}")
        subprocess.run(
            [keygen, "-t", "ed25519", "-N", "", "-f", str(identity), "-C", "lichtfeld-runpod"],
            check=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )
    if not pubkey.is_file():
        pub = subprocess.check_output(
            [keygen, "-y", "-f", str(identity)],
            text=True,
            stdin=subprocess.DEVNULL,
        ).strip()
        write_text_lf(pubkey, pub + "\n")
        if not IS_WINDOWS:
            pubkey.chmod(0o644)
    restrict_secret_file(identity)
    return pubkey.read_text(encoding="utf-8").strip()


def ssh_config_text(
    host: str,
    port: int,
    identity: Path,
    known_hosts: Path,
    *,
    multiplex: bool | None = None,
) -> str:
    if multiplex is None:
        multiplex = not IS_WINDOWS
    ident = posix_path(identity)
    kh = posix_path(known_hosts)
    lines = [
        "Host runpod",
        f"    HostName {host}",
        f"    Port {port}",
        "    User root",
        f'    IdentityFile "{ident}"',
        "    IdentitiesOnly yes",
        "    PreferredAuthentications publickey",
        "    PubkeyAuthentication yes",
        "    PasswordAuthentication no",
        "    KbdInteractiveAuthentication no",
        "    NumberOfPasswordPrompts 0",
        "    BatchMode yes",
        "    StrictHostKeyChecking no",
        f'    UserKnownHostsFile "{kh}"',
        "    LogLevel ERROR",
        "    ServerAliveInterval 30",
        "    ServerAliveCountMax 10",
        "    ConnectTimeout 20",
        "    RequestTTY no",
    ]
    if multiplex:
        # ControlPath must stay under the ~108-byte Unix socket limit; job dirs are too long.
        lines.extend(
            [
                "    ControlMaster auto",
                "    ControlPath /tmp/lf-ssh-%C",
                "    ControlPersist 10m",
            ]
        )
    return "\n".join(lines) + "\n"


def write_ssh_config(path: Path, host: str, port: int, identity: Path) -> None:
    known_hosts = path.parent / "known_hosts"
    write_text_lf(path, ssh_config_text(host, port, identity, known_hosts))
    restrict_secret_file(path)


def _cmd_error(cmd: list[str], r: subprocess.CompletedProcess[str] | None, extra: str = "") -> str:
    bits = [f"ssh exit {r.returncode if r else '?'}"]
    if extra:
        bits.append(extra)
    if r is not None:
        err = (r.stderr or r.stdout or "").strip()
        if err:
            bits.append(err[-800:])
    bits.append(" ".join(cmd[-2:]))
    return " · ".join(bits)


class Ssh:
    def __init__(self, config_file: Path) -> None:
        self.config_file = config_file
        self._ssh = which_tool("ssh")
        self._scp = which_tool("scp")

    def _exec(
        self,
        cmd: list[str],
        *,
        check: bool = True,
        timeout: int | None = 120,
        attempts: int = 5,
    ) -> subprocess.CompletedProcess[str]:
        last: subprocess.CompletedProcess[str] | None = None
        last_extra = ""
        tries = attempts if check else 1
        for i in range(tries):
            try:
                r = subprocess.run(
                    cmd,
                    check=False,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired as e:
                last_extra = str(e)
                log("ssh", f"timeout try {i + 1}/{tries}")
                if i + 1 < tries:
                    time.sleep(2 * (i + 1))
                continue
            last = r
            if r.returncode == 0:
                return r
            err = (r.stderr or r.stdout or f"exit {r.returncode}").strip()
            log("ssh", f"exit {r.returncode} try {i + 1}/{tries} {err[-200:]}")
            if not check:
                return r
            if i + 1 < tries:
                time.sleep(2 * (i + 1))
        if not check and last is not None:
            return last
        raise SshError(_cmd_error(cmd, last, last_extra))

    def run(self, remote: str, check: bool = True, timeout: int | None = 120) -> subprocess.CompletedProcess[str]:
        cmd = [self._ssh, "-F", str(self.config_file), "runpod", remote]
        return self._exec(cmd, check=check, timeout=timeout)

    def check_output(self, remote: str, timeout: int | None = 120) -> str:
        r = self.run(remote, check=True, timeout=timeout)
        return r.stdout

    def put(self, local: Path, remote: str) -> None:
        cmd = [self._scp, "-F", str(self.config_file), str(local), f"runpod:{remote}"]
        self._exec(cmd, check=True, timeout=180)

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
            except (subprocess.TimeoutExpired, SshError) as e:
                last = str(e)
            tail = last.splitlines()[-1] if last else ""
            log("ssh", f"retry {i + 1}/{tries} {tail}")
            time.sleep(2)
        raise SshError(f"SSH never became ready: {last}")
