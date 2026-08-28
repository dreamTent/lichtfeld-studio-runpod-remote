from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from .config import ConfigError, load_config
from .log import log
from .orchestrate import run_job


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lichtfeld-runpod",
        description="Create a RunPod GPU, restore LichtFeld Studio, train, upload results + logs.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="YAML config (default: ./config.yaml)",
    )
    parser.add_argument(
        "--env",
        default=None,
        help="Path to .env (default: same directory as --config)",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Write config.yaml and .env from the examples if they do not exist, then exit.",
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help="Start the local dashboard at http://127.0.0.1:8765 (does not start a GPU job).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="UI bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="UI port (default 8765)")
    args = parser.parse_args(argv)

    here = Path.cwd()
    examples = Path(__file__).resolve().parent.parent
    if args.init:
        _init_files(here, examples)
        return 0

    if args.ui:
        from .web import serve

        log("ui", "starting dashboard (localhost only)")
        serve(here, host=args.host, port=args.port)
        return 0

    cfg_path = Path(args.config).expanduser().resolve()
    env_path = Path(args.env).expanduser().resolve() if args.env else cfg_path.parent / ".env"
    if not cfg_path.is_file():
        log("config", f"{cfg_path} not found. Run: python3 -m lichtfeld_runpod --init")
        return 2
    try:
        cfg = load_config(cfg_path, env_path)
    except ConfigError as e:
        log("config", str(e))
        return 2
    log("config", f"job={cfg.job_name} gpu={cfg.runpod.gpu} cloud={cfg.runpod.cloud}")
    log("config", f"dataset={cfg.storage.dataset_archive}")
    log("config", f"build={cfg.storage.build_archive}")
    log("config", f"result={cfg.storage.result_dir}")
    return run_job(cfg, cfg_path.parent)


def _init_files(here: Path, examples: Path) -> None:
    mapping = {
        "config.example.yaml": "config.yaml",
        ".env.example": ".env",
    }
    for src_name, dest_name in mapping.items():
        src = examples / src_name
        dest = here / dest_name
        if dest.exists():
            log("init", f"keep existing {dest}")
            continue
        shutil.copy(src, dest)
        if dest_name == ".env":
            dest.chmod(0o600)
        log("init", f"wrote {dest}")
    log("init", "edit config.yaml and .env, then: python3 -m lichtfeld_runpod --config config.yaml")
    log("init", "or start the dashboard: python3 -m lichtfeld_runpod --ui")


if __name__ == "__main__":
    raise SystemExit(main())
