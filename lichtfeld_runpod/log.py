from __future__ import annotations

import sys
from datetime import datetime


def ts() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def log(stage: str, message: str) -> None:
    stage = (stage or "-")[:12]
    print(f"[{ts()}] {stage:<12} {message}", flush=True)


def die(stage: str, message: str, code: int = 1) -> None:
    log(stage, f"ERROR {message}")
    sys.exit(code)
