from __future__ import annotations

import base64
import queue
import secrets
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .host import IS_WINDOWS, posix_path, restrict_secret_file, which_tool, write_text_lf
from .log import log

SSH_FAILS_BEFORE_FTP = 5
_SESSION_REMOTE = "exec bash --noprofile --norc -s"


def ftp_check_due(attempt: int) -> bool:
    """True on the first attempt that is due for an FTP probe. Later retries wait for a manual reload."""
    return attempt == SSH_FAILS_BEFORE_FTP


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


def session_wrap(remote: str, token: str) -> tuple[str, str, str]:
    begin = f"__LF_B_{token}__"
    end = f"__LF_E_{token}__"
    script = (
        f"printf '%s\\n' {shlex.quote(begin)}; "
        f"set +e; "
        f"{remote.rstrip()}\n"
        f"__rc=$?; "
        f"printf '\\n%s %s\\n' {shlex.quote(end)} \"$__rc\"\n"
    )
    return script, begin, end


def try_parse_framed(lines: list[str], begin: str, end: str) -> tuple[str, int] | None:
    started = False
    out: list[str] = []
    for text in lines:
        if not started:
            if text == begin:
                started = True
            continue
        if text == end or text.startswith(end + " "):
            rc_s = text[len(end) :].strip()
            try:
                rc = int(rc_s) if rc_s else 1
            except ValueError:
                rc = 1
            stdout = "\n".join(out)
            if out:
                stdout += "\n"
            return stdout, rc
        out.append(text)
    return None


def _cmd_error(cmd: str, r: subprocess.CompletedProcess[str] | None, extra: str = "") -> str:
    bits = [f"ssh exit {r.returncode if r else '?'}"]
    if extra:
        bits.append(extra)
    if r is not None:
        err = (r.stderr or r.stdout or "").strip()
        if err:
            bits.append(err[-800:])
    bits.append(cmd)
    return " · ".join(bits)


def _preview(remote: str) -> str:
    return remote.strip().splitlines()[0][:120] if remote.strip() else remote


def _reader(proc: subprocess.Popen[bytes], q: queue.Queue[bytes | None]) -> None:
    stream = proc.stdout
    try:
        if stream is None:
            return
        while True:
            line = stream.readline()
            if not line:
                break
            q.put(line)
    except Exception:
        pass
    finally:
        q.put(None)


class Ssh:
    def __init__(self, config_file: Path, *, ssh_bin: str | None = None) -> None:
        self.config_file = config_file
        self._ssh = ssh_bin or which_tool("ssh")
        self._proc: subprocess.Popen[bytes] | None = None
        self._out_q: queue.Queue[bytes | None] | None = None
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _close_unlocked(self) -> None:
        proc = self._proc
        self._proc = None
        self._out_q = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    def _open_unlocked(self, timeout: int = 20) -> str:
        self._close_unlocked()
        cmd = [self._ssh, "-T", "-F", str(self.config_file), "runpod", _SESSION_REMOTE]
        log("ssh", "opening session")
        kw: dict = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "bufsize": 0,
        }
        if IS_WINDOWS:
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        self._proc = subprocess.Popen(cmd, **kw)
        self._out_q = queue.Queue()
        threading.Thread(target=_reader, args=(self._proc, self._out_q), daemon=True).start()
        r = self._run_unlocked("echo SSH_OK && hostname", timeout=timeout)
        if r.returncode != 0 or "SSH_OK" not in (r.stdout or ""):
            err = (r.stdout or "").strip() or "ssh probe failed"
            self._close_unlocked()
            raise SshError(err)
        lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip() and ln.strip() != "SSH_OK"]
        return lines[-1].strip() if lines else "runpod"

    def _run_unlocked(self, remote: str, timeout: int) -> subprocess.CompletedProcess[str]:
        if not self._alive() or self._proc is None or self._proc.stdin is None or self._out_q is None:
            raise SshError("ssh session closed")
        token = secrets.token_hex(8)
        script, begin, end = session_wrap(remote, token)
        stdin = self._proc.stdin
        q = self._out_q
        try:
            stdin.write(script.encode("utf-8"))
            stdin.flush()
        except BrokenPipeError as e:
            self._close_unlocked()
            raise SshError("ssh session closed") from e
        deadline = time.monotonic() + timeout
        seen: list[str] = []
        preamble: list[str] = []
        started = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._close_unlocked()
                raise subprocess.TimeoutExpired(cmd=self._ssh, timeout=timeout)
            try:
                item = q.get(timeout=remaining)
            except queue.Empty:
                self._close_unlocked()
                raise subprocess.TimeoutExpired(cmd=self._ssh, timeout=timeout)
            if item is None:
                extra = " · ".join(preamble[-3:]) if preamble else "ssh session closed"
                self._close_unlocked()
                raise SshError(extra)
            text = item.decode("utf-8", errors="replace").rstrip("\r\n")
            if not started:
                if text == begin:
                    started = True
                elif text:
                    preamble.append(text)
                continue
            seen.append(text)
            parsed = try_parse_framed([begin, *seen], begin, end)
            if parsed is None:
                continue
            stdout, rc = parsed
            return subprocess.CompletedProcess(["ssh", remote], rc, stdout, "")

    def _exec(
        self,
        remote: str,
        *,
        check: bool = True,
        timeout: int | None = 120,
        attempts: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        last: subprocess.CompletedProcess[str] | None = None
        last_extra = ""
        i = 0
        limit = timeout if timeout is not None else 120
        while True:
            i += 1
            log("ssh", f"exec try {i} {_preview(remote)}")
            with self._lock:
                try:
                    if not self._alive():
                        self._open_unlocked(timeout=min(limit, 20))
                    r = self._run_unlocked(remote, timeout=limit)
                except subprocess.TimeoutExpired as e:
                    last_extra = str(e)
                    log("ssh", f"timeout try {i}")
                    self._close_unlocked()
                    if not check or (attempts is not None and i >= attempts):
                        break
                except SshError as e:
                    last_extra = str(e)
                    log("ssh", f"exit ? try {i} {e}")
                    self._close_unlocked()
                    if not check or (attempts is not None and i >= attempts):
                        break
                else:
                    last = r
                    if r.returncode == 0:
                        return r
                    err = (r.stderr or r.stdout or f"exit {r.returncode}").strip()
                    log("ssh", f"exit {r.returncode} try {i} {err[-200:]}")
                    if not check:
                        return r
                    if attempts is not None and i >= attempts:
                        break
            time.sleep(min(2 * i, 30))
        if not check and last is not None:
            return last
        raise SshError(_cmd_error(_preview(remote), last, last_extra))

    def run(
        self,
        remote: str,
        check: bool = True,
        timeout: int | None = 120,
        attempts: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self._exec(remote, check=check, timeout=timeout, attempts=attempts)

    def check_output(self, remote: str, timeout: int | None = 120, attempts: int | None = None) -> str:
        r = self.run(remote, check=True, timeout=timeout, attempts=attempts)
        return r.stdout

    def put(self, local: Path, remote: str) -> None:
        b64 = base64.b64encode(local.read_bytes()).decode("ascii")
        dest = shlex.quote(remote)
        self.run(f"umask 077; printf %s {shlex.quote(b64)} | base64 -d > {dest}")

    def put_text(self, text: str, remote: str, mode: str = "644") -> None:
        quoted = shlex.quote(text)
        self.run(f"umask 077; printf %s {quoted} > {shlex.quote(remote)}; chmod {mode} {shlex.quote(remote)}")

    def wait_ready(self, *, should_stop: Callable[[int], bool] | None = None) -> bool:
        last = ""
        i = 0
        while True:
            i += 1
            log("ssh", f"try {i}")
            with self._lock:
                try:
                    self._close_unlocked()
                    host = self._open_unlocked(timeout=20)
                    log("ssh", f"ready ({host})")
                    return True
                except (subprocess.TimeoutExpired, SshError) as e:
                    last = str(e)
                    self._close_unlocked()
            tail = last.splitlines()[-1] if last else ""
            log("ssh", f"retry {i} {tail}")
            if should_stop and should_stop(i):
                return False
            time.sleep(20)
