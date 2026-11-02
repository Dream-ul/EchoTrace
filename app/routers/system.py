"""Health and readiness endpoints for the EchoTrace deployment."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.readiness import dependency_readiness

router = APIRouter(tags=["system"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
def readyz():
    result = dependency_readiness()
    if result["status"] != "ready":
        return JSONResponse(status_code=503, content=result)
    return {**result, "agent": "live"}


@router.get("/api/system/status")
def status() -> dict[str, object]:
    return {"success": True, "service": "echotrace", "version": "3.0.0"}
