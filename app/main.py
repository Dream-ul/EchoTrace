"""EchoTrace application composition root.

Only the node-driven investigation API is mounted here.
"""

from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from app.routers import investigations, settings, system


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker_service = None
    if os.getenv("ECHOTRACE_PROCESS") == "local":
        from echotrace.worker import WorkerService

        worker_service = WorkerService(investigations.get_investigation_runtime())
        worker_service.start()
    try:
        yield
    finally:
        if worker_service is not None:
            worker_service.close()
        investigations.close_investigation_runtime()


app = FastAPI(title="EchoTrace Investigation API", version="3.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(system.router)
app.include_router(investigations.router)
app.include_router(settings.router)

_BASE = Path(__file__).resolve().parent.parent
_VUE_DIST = _BASE / "frontend" / "dist"
_INDEX_HTML = _VUE_DIST / "index.html"
if (_VUE_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=_VUE_DIST / "assets"), name="assets")


@app.get("/favicon.svg")
async def favicon():
    path = _VUE_DIST / "favicon.svg"
    return FileResponse(path) if path.exists() else Response(status_code=404)


@app.get("/{full_path:path}")
async def spa(full_path: str):
    if _INDEX_HTML.exists():
        return FileResponse(_INDEX_HTML)
    return HTMLResponse("<h1>EchoTrace</h1>")
