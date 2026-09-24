from __future__ import annotations

import hmac
import hashlib
import json
import logging
import platform
import shutil
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .catalog import ReferenceCatalog, build_catalog
from .installer import LocalSourceIndexer, ZenodoInstaller, local_ir_files, zenodo_file_inventory
from .pubchem import PubChemResolver
from .settings import Settings, load_settings
from .state import ServiceState


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ftir-reference-service")


class SetupRequest(BaseModel):
    acceptLicense: bool = Field(description="Explicit acceptance of the dataset licence before download.")
    files: list[str] | None = Field(default=None, max_length=9, description="Optional selected Zenodo Parquet filenames.")


class RebuildRequest(BaseModel):
    files: list[str] | None = Field(default=None, max_length=9, description="Optional selected downloaded Parquet filenames.")


class SearchRequest(BaseModel):
    points: list[list[float]] = Field(min_length=8, description="[[wavenumber cm-1, intensity], ...]")
    signalType: Literal["absorbance", "transmittance"] = "absorbance"
    topK: int = Field(default=5, ge=1, le=20)


class MetadataResolveRequest(BaseModel):
    smiles: str = Field(min_length=1, max_length=2048, description="SMILES returned by a reference match")


def _query_diagnostics(points: list[list[float]]) -> dict[str, object]:
    """Small, non-reversible trace for distinguishing browser queries in logs."""
    fingerprint = hashlib.sha256(
        json.dumps(points, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()[:16]
    x_values = [point[0] for point in points]
    y_values = [point[1] for point in points]
    return {
        "fingerprint": fingerprint,
        "pointCount": len(points),
        "xRangeCm1": [min(x_values), max(x_values)],
        "yRange": [min(y_values), max(y_values)],
    }


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


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
    pubchem = PubChemResolver(settings.data_dir, settings.pubchem_timeout_seconds)
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

    def clear_catalog() -> None:
        nonlocal catalog
        with catalog_lock:
            catalog = None

    def selected_local_files(selected_names: list[str] | None) -> list[Path]:
        files = local_ir_files(settings)
        if not selected_names:
            return files
        by_name = {path.name: path for path in files}
        requested = list(dict.fromkeys(selected_names))
        unknown = [name for name in requested if name not in by_name]
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown downloaded file: {unknown[0]}")
        return [by_name[name] for name in requested]

    def current_dataset(manifest: dict[str, object], files: list[Path]) -> dict[str, object]:
        previous = state.snapshot().get("dataset") or {}
        return {
            **previous,
            "recordId": settings.zenodo_record_id,
            "doi": previous.get("doi") or "10.5281/zenodo.16417648",
            "referenceType": "computed",
            "license": previous.get("license") or "CDLA-Permissive-2.0",
            "files": [path.name for path in files],
            "fullDataset": len(files) == 9,
            "catalog": manifest,
            "searchAlgorithmVersion": manifest.get("searchAlgorithmVersion", "global-cosine-v1"),
        }

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
    if settings.cors_allow_origins or settings.cors_allow_origin_regex:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allow_origins),
            allow_origin_regex=settings.cors_allow_origin_regex,
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
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        started_at = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("request id=%s %s %s failed", request_id, request.method, request.url.path)
            raise
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request id=%s method=%s path=%s status=%s duration_ms=%d",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            round((time.perf_counter() - started_at) * 1000),
        )
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

    @app.get("/api/admin/diagnostics", dependencies=[Depends(require_admin)])
    def admin_diagnostics() -> dict[str, object]:
        usage = shutil.disk_usage(settings.data_dir)
        snapshot = state.snapshot()
        manifest = (snapshot.get("dataset") or {}).get("catalog") or {}
        return {
            "status": snapshot.get("status"),
            "activeJob": snapshot.get("activeJob"),
            "catalog": manifest,
            "storage": {
                "dataDir": str(settings.data_dir),
                "sourceDir": str(settings.source_dir),
                "sourceBytes": _directory_size(settings.source_dir),
                "indexBytes": _directory_size(settings.index_dir),
                "freeBytes": usage.free,
                "totalBytes": usage.total,
            },
            "runtime": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "numpy": __import__("numpy").__version__,
                "pyarrow": __import__("pyarrow").__version__,
            },
            "configuration": {
                "recordId": settings.zenodo_record_id,
                "vectorPoints": settings.vector_points,
                "corsOrigins": list(settings.cors_allow_origins),
                "corsRegexConfigured": bool(settings.cors_allow_origin_regex),
                "adminTokenConfigured": bool(settings.admin_token),
                "serviceTokenConfigured": bool(settings.service_token),
            },
        }

    @app.get("/api/admin/files", dependencies=[Depends(require_admin)])
    def admin_files() -> dict[str, object]:
        try:
            return zenodo_file_inventory(settings)
        except Exception as error:
            logger.warning("admin file inventory failed: %s", error)
            raise HTTPException(status_code=502, detail=f"Could not load Zenodo file inventory: {error}") from error

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

    @app.get("/api/admin/catalog/search", dependencies=[Depends(require_admin)])
    def admin_catalog_search(query: str = Query(min_length=1, max_length=4096)) -> dict[str, object]:
        matches = get_catalog().lookup(query)
        return {"query": query, "matches": matches}

    @app.get("/api/admin/catalog/{reference_id}", dependencies=[Depends(require_admin)])
    def admin_catalog_reference(reference_id: str) -> dict[str, object]:
        item = get_catalog().get(reference_id)
        if not item:
            raise HTTPException(status_code=404, detail="Reference not found")
        return item

    @app.post("/api/admin/setup", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_admin)])
    def setup(payload: SetupRequest) -> dict[str, object]:
        nonlocal catalog
        if not payload.acceptLicense:
            raise HTTPException(status_code=400, detail="License acceptance is required before downloading Zenodo data")
        try:
            job = state.start_job("zenodo-install")
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        installer = ZenodoInstaller(settings, state)
        thread = threading.Thread(
            target=installer.install,
            args=(job["id"], payload.files, clear_catalog),
            daemon=True,
            name="zenodo-install",
        )
        thread.start()
        return {
            "job": job,
            "message": "Download and index build started. It can continue from saved .part files.",
        }

    @app.post("/api/admin/rebuild-index", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_admin)])
    def rebuild_index(payload: RebuildRequest) -> dict[str, object]:
        files = selected_local_files(payload.files)
        if not files:
            raise HTTPException(status_code=400, detail="No downloaded IR Parquet files are available to index")
        try:
            job = state.start_job("rebuild-index", initial_status="indexing")
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

        def run() -> None:
            def log(message: str, level: str = "INFO") -> None:
                state.log(job["id"], message, level)

            try:
                log(f"Rebuilding index from {len(files)} selected verified/present file(s)")
                manifest = build_catalog(files, settings.index_dir, settings.vector_points, log)
                clear_catalog()
                state.finish_job(job["id"], dataset=current_dataset(manifest, files))
                log("Rebuild complete. The new catalogue will be loaded on the next search.")
            except Exception as error:
                message = f"{type(error).__name__}: {error}"
                log(message, "ERROR")
                state.finish_job(job["id"], error=message)

        thread = threading.Thread(target=run, daemon=True, name="rebuild-index")
        thread.start()
        return {"job": job, "message": "Index rebuild started from existing source files; no download is performed."}

    @app.post("/api/admin/index-local", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_admin)])
    def index_local_source(payload: SetupRequest) -> dict[str, object]:
        nonlocal catalog
        if not payload.acceptLicense:
            raise HTTPException(status_code=400, detail="License acceptance is required before indexing Zenodo data")
        try:
            job = state.start_job("local-source-index", initial_status="indexing")
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        indexer = LocalSourceIndexer(settings, state)
        thread = threading.Thread(target=indexer.install, args=(job["id"], None, clear_catalog), daemon=True, name="local-source-index")
        thread.start()
        return {
            "job": job,
            "message": "Building a local development index from the Parquet files already present in REFERENCE_SOURCE_DIR.",
        }

    @app.post("/api/v1/search", dependencies=[Depends(require_service)])
    def search(payload: SearchRequest, request: Request) -> dict[str, object]:
        started_at = time.perf_counter()
        current_catalog = get_catalog()
        diagnostics = _query_diagnostics(payload.points)
        try:
            matches = current_catalog.search(payload.points, payload.signalType, payload.topK)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        logger.info(
            "search request_id=%s query=%s points=%s x_range=%s y_range=%s signal=%s top=%s duration_ms=%d",
            getattr(request.state, "request_id", "unknown"),
            diagnostics["fingerprint"],
            diagnostics["pointCount"],
            diagnostics["xRangeCm1"],
            diagnostics["yRange"],
            payload.signalType,
            [(item["id"], item["score"]) for item in matches[:3]],
            round((time.perf_counter() - started_at) * 1000),
        )
        return {
            "referenceType": "computed",
            "catalogVersion": current_catalog.manifest["catalogVersion"],
            "query": {"signalType": payload.signalType, "topK": payload.topK, **diagnostics},
            "matches": matches,
            "limitations": [
                "Matches are hypotheses against computed reference spectra, not experimental identification.",
                "Instrument, sample phase, ATR/transmission mode and baseline can change similarity.",
                *(["This is a partial local catalogue; only pre-downloaded Parquet chunks were searched."]
                  if (state.snapshot().get("dataset") or {}).get("sourceMode") == "local-partial" else []),
            ],
        }

    @app.post("/api/v1/metadata/resolve", dependencies=[Depends(require_service)])
    def resolve_metadata(payload: MetadataResolveRequest) -> dict[str, object]:
        """Resolve a candidate's display name through cached PubChem metadata."""
        try:
            metadata = pubchem.resolve(payload.smiles)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return {"metadata": metadata}

    @app.get("/api/v1/references/{reference_id}", dependencies=[Depends(require_service)])
    def reference(reference_id: str) -> dict[str, object]:
        item = get_catalog().get(reference_id)
        if not item:
            raise HTTPException(status_code=404, detail="Reference not found")
        return item

    return app


app = create_app()
