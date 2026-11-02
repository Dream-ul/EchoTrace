"""Workbench settings API with a strict writable allow-list."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import config

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsInput(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


@router.get("")
def get_settings():
    return {"success": True, "settings": config.snapshot()}


@router.put("")
def update_settings(payload: SettingsInput):
    try:
        settings = config.save(payload.values)
        from app.routers import investigations
        investigations.reload_investigation_runtime()
        return {"success": True, "settings": settings, "restarted": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"settings reload failed: {exc}") from exc

