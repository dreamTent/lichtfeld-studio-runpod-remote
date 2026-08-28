from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import (
    ConfigError,
    load_app_config,
    mask_secret,
    read_env_file,
    write_env_file,
)
from .localfs import list_local_dir
from .log import log
from .manager import JobManager
from .runpod import RunpodClient, RunpodError
from .storage import ARCHIVE_SUFFIXES, list_dir_entries, list_remote_files

STATIC = Path(__file__).resolve().parent / "static"


class SettingsIn(BaseModel):
    runpod_api_key: str | None = None
    sftp_host: str | None = None
    sftp_user: str | None = None
    sftp_password: str | None = None
    storage_protocol: str | None = None


class JobIn(BaseModel):
    name: str = ""
    gpu: str
    cloud: str = "SECURE"
    build_archive: str
    dataset_source: str = "ftp"
    dataset_archive: str = ""
    dataset_local: str = ""
    result_dir: str = ""
    config: str = ""
    auto_download: bool = False
    terminate_when_done: bool = True
    max_cap: int | None = 10_000_000
    enable_sparsity: bool = True
    gut: bool = True


def create_app(workdir: Path | None = None) -> FastAPI:
    workdir = (workdir or Path.cwd()).resolve()
    manager = JobManager(workdir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        manager.start()
        log("ui", f"dashboard at http://127.0.0.1 — workdir {workdir}")
        yield
        manager.stop()

    app = FastAPI(title="LichtFeld RunPod", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def cfg():
        try:
            return load_app_config(workdir)
        except ConfigError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/settings")
    def get_settings() -> dict:
        env = read_env_file(workdir / ".env")
        protocol = env.get("STORAGE_PROTOCOL") or "ftp"
        try:
            loaded = load_app_config(workdir)
            protocol = loaded.storage.protocol
            host = loaded.storage.host
            user = loaded.storage.user
        except ConfigError:
            host = env.get("SFTP_HOST", "")
            user = env.get("SFTP_USER", "")
        return {
            "runpod_api_key": mask_secret(env.get("RUNPOD_API_KEY", "")),
            "runpod_api_key_set": bool(env.get("RUNPOD_API_KEY")),
            "sftp_host": host,
            "sftp_user": user,
            "sftp_password": mask_secret(env.get("SFTP_PASSWORD", "")),
            "sftp_password_set": bool(env.get("SFTP_PASSWORD")),
            "storage_protocol": protocol,
            "configured": bool(env.get("RUNPOD_API_KEY") and env.get("SFTP_PASSWORD")),
        }

    @app.put("/api/settings")
    def put_settings(body: SettingsIn) -> dict:
        updates: dict[str, str] = {}
        if body.runpod_api_key and not body.runpod_api_key.startswith("•"):
            updates["RUNPOD_API_KEY"] = body.runpod_api_key.strip()
        if body.sftp_host:
            updates["SFTP_HOST"] = body.sftp_host.strip()
        if body.sftp_user:
            updates["SFTP_USER"] = body.sftp_user.strip()
        if body.sftp_password and not body.sftp_password.startswith("•"):
            updates["SFTP_PASSWORD"] = body.sftp_password
        if body.storage_protocol:
            proto = body.storage_protocol.strip().lower()
            if proto not in {"ftp", "sftp"}:
                raise HTTPException(status_code=400, detail="protocol must be ftp or sftp")
            updates["STORAGE_PROTOCOL"] = proto
        if not updates:
            raise HTTPException(status_code=400, detail="no settings to save")
        write_env_file(workdir / ".env", updates)
        return get_settings()

    @app.get("/api/state")
    def state() -> dict:
        return manager.snapshot()

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        async def gen():
            last = ""
            while True:
                if await request.is_disconnected():
                    break
                snap = json.dumps(manager.snapshot())
                if snap != last:
                    yield f"data: {snap}\n\n"
                    last = snap
                await asyncio.sleep(1.5)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/jobs")
    def list_jobs() -> dict:
        return {"jobs": manager.snapshot()["jobs"]}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        snap = manager.snapshot()
        for row in snap["jobs"]:
            if row["id"] == job_id:
                return row
        raise HTTPException(status_code=404, detail="job not found")

    @app.get("/api/jobs/{job_id}/log")
    def job_log(job_id: str) -> dict:
        job = manager.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return {"id": job.id, "log": job.log_tail, "stage": job.stage, "message": job.message}

    @app.post("/api/jobs")
    def create_job(body: JobIn) -> dict:
        try:
            job = manager.submit(body.model_dump())
        except (ValueError, ConfigError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return job.to_dict()

    @app.get("/api/pods")
    def list_pods() -> dict:
        return {"pods": manager.snapshot()["pods"]}

    @app.get("/api/gpus")
    def gpus() -> dict:
        c = cfg()
        try:
            items = RunpodClient(c.runpod.api_key).list_gpus()
        except RunpodError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        return {"gpus": items}

    @app.get("/api/ftp/builds")
    def ftp_builds() -> dict:
        c = cfg()
        paths = list_remote_files(c.storage, "lichtfeld-builds")
        if not paths:
            paths = [p for p in list_remote_files(c.storage, "") if "build" in p.lower()]
        return {"files": paths}

    @app.get("/api/ftp/datasets")
    def ftp_datasets() -> dict:
        c = cfg()
        paths = list_remote_files(c.storage, "lichtfeld-datasets")
        for entry in list_dir_entries(c.storage, ""):
            name = str(entry["name"])
            if not entry["is_dir"] and name.lower().endswith(ARCHIVE_SUFFIXES):
                paths.append(name)
        # unique, keep order
        seen: set[str] = set()
        uniq = []
        for p in paths:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return {"files": uniq}

    @app.get("/api/fs")
    def fs_browse(path: str = Query(default="")) -> dict:
        home = Path.home().resolve()
        target = Path(path).expanduser() if path else home
        try:
            return list_local_dir(target, home)
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except (FileNotFoundError, NotADirectoryError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    return app


def serve(workdir: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    log("ui", f"http://{host}:{port}")
    uvicorn.run(create_app(workdir), host=host, port=port, log_level="info")
