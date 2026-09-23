from __future__ import annotations

import hmac
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .catalog import ReferenceCatalog
from .installer import LocalSourceIndexer, ZenodoInstaller
from .settings import Settings, load_settings
from .state import ServiceState


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ftir-reference-service")


class SetupRequest(BaseModel):
    acceptLicense: bool = Field(description="Explicit acceptance of the dataset licence before download.")


class SearchRequest(BaseModel):
    points: list[list[float]] = Field(min_length=8, description="[[wavenumber cm-1, intensity], ...]")
    signalType: Literal["absorbance", "transmittance"] = "absorbance"
    topK: int = Field(default=5, ge=1, le=20)


def _token_from_authorization(value: str | None) -> str:
    if not value:
        return ""
    prefix = "Bearer "
    return value[len(prefix):].strip() if value.startswith(prefix) else ""


def _require_token(expected: str, supplied: str, label: str) -> None:
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"{label} is not configured")
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def create_app() -> FastAPI:
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    state = ServiceState(settings.data_dir)
    catalog: ReferenceCatalog | None = None
    catalog_lock = threading.RLock()

    def get_catalog() -> ReferenceCatalog:
        nonlocal catalog
        with catalog_lock:
            if catalog is None:
                try:
                    catalog = ReferenceCatalog(settings.index_dir)
                    if state.snapshot().get("status") != "ready":
                        state.set_status("ready")
                except FileNotFoundError as error:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="reference_dataset_not_ready",
                    ) from error
            return catalog

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal catalog
        try:
            catalog = ReferenceCatalog(settings.index_dir)
            state.set_status("ready")
            logger.info("Reference catalogue loaded")
        except FileNotFoundError:
            if state.snapshot().get("status") == "ready":
                state.set_status("uninitialized")
            logger.info("Reference catalogue not installed yet")
        yield

    app = FastAPI(
        title="FTIR Reference Spectra Service",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allow_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "X-Service-Token"],
        )

    def require_admin(authorization: Annotated[str | None, Header()] = None) -> None:
        _require_token(settings.admin_token, _token_from_authorization(authorization), "ADMIN_TOKEN")

    def require_service(x_service_token: Annotated[str | None, Header()] = None) -> None:
        _require_token(settings.service_token, x_service_token or "", "SERVICE_TOKEN")

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        response = await call_next(request)
        logger.info("request %s %s %s", request.method, request.url.path, response.status_code)
        return response

    @app.get("/", include_in_schema=False)
    def admin_page() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "admin.html")

    @app.get("/health")
    def health() -> dict[str, object]:
        snapshot = state.snapshot()
        return {
            "ok": snapshot["status"] in {"uninitialized", "ready"},
            "service": "ftir-reference-service",
            "status": snapshot["status"],
            "catalogVersion": (snapshot.get("dataset") or {}).get("catalog", {}).get("catalogVersion"),
        }

    @app.get("/api/admin/status", dependencies=[Depends(require_admin)])
    def admin_status() -> dict[str, object]:
        return state.snapshot()

    @app.get("/api/admin/jobs/{job_id}", dependencies=[Depends(require_admin)])
    def admin_job(job_id: str) -> dict[str, object]:
        snapshot = state.snapshot()
        job = next((item for item in snapshot["jobs"] if item.get("id") == job_id), None)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        job["logs"] = [entry for entry in snapshot.get("logs", []) if entry.get("jobId") == job_id]
        return job

    @app.get("/api/admin/logs", dependencies=[Depends(require_admin)])
    def admin_logs(tail: int = 200) -> dict[str, object]:
        snapshot = state.snapshot()
        return {"logs": snapshot.get("logs", [])[-max(1, min(tail, 500)):]} 

    @app.post("/api/admin/setup", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_admin)])
    def setup(payload: SetupRequest) -> dict[str, object]:
        nonlocal catalog
        if not payload.acceptLicense:
            raise HTTPException(status_code=400, detail="License acceptance is required before downloading Zenodo data")
        try:
            job = state.start_job("zenodo-install")
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        catalog = None
        installer = ZenodoInstaller(settings, state)
        thread = threading.Thread(target=installer.install, args=(job["id"],), daemon=True, name="zenodo-install")
        thread.start()
        return {
            "job": job,
            "message": "Download and local index build started. It can take a long time and continues in the background.",
        }

    @app.post("/api/admin/index-local", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_admin)])
    def index_local_source(payload: SetupRequest) -> dict[str, object]:
        nonlocal catalog
        if not payload.acceptLicense:
            raise HTTPException(status_code=400, detail="License acceptance is required before indexing Zenodo data")
        try:
            job = state.start_job("local-source-index", initial_status="indexing")
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        catalog = None
        indexer = LocalSourceIndexer(settings, state)
        thread = threading.Thread(target=indexer.install, args=(job["id"],), daemon=True, name="local-source-index")
        thread.start()
        return {
            "job": job,
            "message": "Building a local development index from the Parquet files already present in REFERENCE_SOURCE_DIR.",
        }

    @app.post("/api/v1/search", dependencies=[Depends(require_service)])
    def search(payload: SearchRequest) -> dict[str, object]:
        current_catalog = get_catalog()
        try:
            matches = current_catalog.search(payload.points, payload.signalType, payload.topK)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "referenceType": "computed",
            "catalogVersion": current_catalog.manifest["catalogVersion"],
            "query": {"signalType": payload.signalType, "topK": payload.topK},
            "matches": matches,
            "limitations": [
                "Matches are hypotheses against computed reference spectra, not experimental identification.",
                "Instrument, sample phase, ATR/transmission mode and baseline can change similarity.",
                *(["This is a partial local catalogue; only pre-downloaded Parquet chunks were searched."]
                  if (state.snapshot().get("dataset") or {}).get("sourceMode") == "local-partial" else []),
            ],
        }

    @app.get("/api/v1/references/{reference_id}", dependencies=[Depends(require_service)])
    def reference(reference_id: str) -> dict[str, object]:
        item = get_catalog().get(reference_id)
        if not item:
            raise HTTPException(status_code=404, detail="Reference not found")
        return item

    return app


app = create_app()
