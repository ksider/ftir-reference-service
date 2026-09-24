from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Callable

import requests

from .catalog import build_catalog
from .settings import Settings
from .state import ServiceState


IR_FILE_PATTERN = re.compile(r"^IR_data_chunk\d{3}_of_009\.parquet$")
ZENODO_API = "https://zenodo.org/api/records/{record_id}"


def local_ir_files(settings: Settings) -> list[Path]:
    return sorted(path for path in settings.source_dir.glob("*.parquet") if IR_FILE_PATTERN.match(path.name))


def load_zenodo_record(settings: Settings) -> dict[str, object]:
    saved_manifest = settings.source_dir / "zenodo-manifest.json"
    if saved_manifest.exists():
        try:
            record = json.loads(saved_manifest.read_text(encoding="utf-8"))
            if isinstance(record, dict):
                return record
        except (OSError, json.JSONDecodeError):
            pass
    response = requests.get(ZENODO_API.format(record_id=settings.zenodo_record_id), timeout=(10, 60))
    response.raise_for_status()
    return response.json()


def zenodo_ir_files(record: dict[str, object]) -> list[dict[str, object]]:
    files = [item for item in record.get("files", []) if IR_FILE_PATTERN.match(str(item.get("key", "")))]
    if len(files) != 9:
        raise RuntimeError(f"Expected 9 IR Parquet files, received {len(files)} from Zenodo")
    return sorted(files, key=lambda item: str(item["key"]))


def select_zenodo_files(files: list[dict[str, object]], selected_names: list[str] | None) -> list[dict[str, object]]:
    if not selected_names:
        return files
    available = {str(item["key"]): item for item in files}
    requested = list(dict.fromkeys(str(name) for name in selected_names))
    invalid = [name for name in requested if name not in available]
    if invalid:
        raise ValueError(f"Unknown Zenodo file selection: {', '.join(invalid[:3])}")
    return [available[name] for name in requested]


def zenodo_file_inventory(settings: Settings) -> dict[str, object]:
    """Report file presence without re-hashing multi-GB files on each refresh."""
    record = load_zenodo_record(settings)
    files = zenodo_ir_files(record)
    result = []
    for item in files:
        filename = str(item["key"])
        destination = settings.source_dir / filename
        partial = destination.with_suffix(destination.suffix + ".part")
        expected_bytes = int(item.get("size") or 0)
        if destination.exists():
            local_bytes = destination.stat().st_size
            state = "present" if not expected_bytes or local_bytes == expected_bytes else "size_mismatch"
        elif partial.exists():
            local_bytes = partial.stat().st_size
            state = "partial"
        else:
            local_bytes = 0
            state = "missing"
        result.append({
            "name": filename,
            "sizeBytes": expected_bytes,
            "localBytes": local_bytes,
            "state": state,
            "checksum": str(item.get("checksum") or ""),
        })
    return {
        "recordId": settings.zenodo_record_id,
        "doi": record.get("doi") or "10.5281/zenodo.16417648",
        "license": (record.get("metadata") or {}).get("license", {}).get("id") or "CDLA-Permissive-2.0",
        "files": result,
    }


class ZenodoInstaller:
    def __init__(self, settings: Settings, state: ServiceState):
        self.settings = settings
        self.state = state

    def install(
        self,
        job_id: str,
        selected_names: list[str] | None = None,
        on_catalog_ready: Callable[[], None] | None = None,
    ) -> None:
        def log(message: str, level: str = "INFO") -> None:
            self.state.log(job_id, message, level)

        try:
            log(f"Loading Zenodo record {self.settings.zenodo_record_id}")
            record = load_zenodo_record(self.settings)
            files = zenodo_ir_files(record)
            selected_files = select_zenodo_files(files, selected_names)
            self.settings.source_dir.mkdir(parents=True, exist_ok=True)
            (self.settings.source_dir / "zenodo-manifest.json").write_text(json.dumps(record, indent=2), encoding="utf-8")

            log(f"Selected {len(selected_files)} of {len(files)} Zenodo IR file(s)")
            for item in selected_files:
                self._download_file(item, log)

            index_files = local_ir_files(self.settings)
            if not index_files:
                raise RuntimeError("No downloaded IR Parquet files are available to index")
            self.state.set_status("indexing")
            log(f"Selected downloads complete. Building vector index from {len(index_files)} present IR file(s).")
            manifest = build_catalog(
                index_files,
                self.settings.index_dir,
                self.settings.vector_points,
                log,
            )
            dataset = {
                "recordId": self.settings.zenodo_record_id,
                "doi": "10.5281/zenodo.16417648",
                "referenceType": "computed",
                "license": "CDLA-Permissive-2.0",
                "files": [path.name for path in index_files],
                "fullDataset": len(index_files) == len(files),
                "catalog": manifest,
            }
            if on_catalog_ready:
                on_catalog_ready()
            log("Installation complete. Reference search is ready.")
            self.state.finish_job(job_id, dataset=dataset)
        except Exception as error:  # The exact message is retained for the Admin UI and logs.
            message = f"{type(error).__name__}: {error}"
            self.state.log(job_id, message, "ERROR")
            self.state.finish_job(job_id, error=message)

    def _download_file(self, item: dict[str, object], log: Callable[[str], None]) -> None:
        filename = str(item["key"])
        destination = self.settings.source_dir / filename
        partial = destination.with_suffix(destination.suffix + ".part")
        expected_checksum = str(item.get("checksum", ""))
        if destination.exists() and self._valid_checksum(destination, expected_checksum):
            log(f"Verified existing {filename}")
            return
        offset = partial.stat().st_size if partial.exists() else 0
        url = str((item.get("links") or {}).get("self") or "")
        if not url:
            url = f"https://zenodo.org/records/{self.settings.zenodo_record_id}/files/{filename}?download=1"
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        log(f"Downloading {filename}" + (f" (resuming at {offset:,} bytes)" if offset else ""))
        with requests.get(url, stream=True, headers=headers, timeout=(10, 120)) as response:
            response.raise_for_status()
            append = offset > 0 and response.status_code == 206
            if offset and not append:
                log(f"Server did not accept resume for {filename}; restarting this file")
            written = offset if append else 0
            mode = "ab" if append else "wb"
            content_length = int(response.headers.get("Content-Length", "0")) + written
            next_log_at = written + 64 * 1024 * 1024
            with partial.open(mode) as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    output.write(chunk)
                    written += len(chunk)
                    if written >= next_log_at:
                        percent = f" ({written / content_length:.0%})" if content_length else ""
                        log(f"Downloading {filename}: {written / 1024 / 1024:.0f} MB{percent}")
                        next_log_at = written + 64 * 1024 * 1024
        if not self._valid_checksum(partial, expected_checksum):
            raise RuntimeError(f"Checksum mismatch for {filename}; the partial file was kept for diagnosis")
        partial.replace(destination)
        log(f"Downloaded and verified {filename}")

    @staticmethod
    def _valid_checksum(path: Path, checksum: str) -> bool:
        if not path.exists() or not checksum.startswith("md5:"):
            return False
        digest = hashlib.md5()  # nosec B324 - Zenodo publishes MD5 for file-integrity verification.
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest().lower() == checksum.split(":", 1)[1].lower()


class LocalSourceIndexer:
    """Build a usable development catalogue from one or more pre-downloaded chunks."""

    def __init__(self, settings: Settings, state: ServiceState):
        self.settings = settings
        self.state = state

    def install(
        self,
        job_id: str,
        selected_names: list[str] | None = None,
        on_catalog_ready: Callable[[], None] | None = None,
    ) -> None:
        def log(message: str, level: str = "INFO") -> None:
            self.state.log(job_id, message, level)

        try:
            files = local_ir_files(self.settings)
            if selected_names:
                selected = set(selected_names)
                files = [path for path in files if path.name in selected]
            if not files:
                raise RuntimeError(
                    f"No IR_data_chunkXXX_of_009.parquet files found in {self.settings.source_dir}"
                )
            log(f"Building a development catalogue from {len(files)} local IR file(s)")
            self.state.set_status("indexing")
            manifest = build_catalog(files, self.settings.index_dir, self.settings.vector_points, log)
            dataset = {
                "recordId": self.settings.zenodo_record_id,
                "doi": "10.5281/zenodo.16417648",
                "referenceType": "computed",
                "license": "CDLA-Permissive-2.0",
                "sourceMode": "local-partial",
                "fullDataset": len(files) == 9,
                "files": [path.name for path in files],
                "catalog": manifest,
            }
            if on_catalog_ready:
                on_catalog_ready()
            log("Local catalogue is ready. Search results cover only the supplied chunk(s).")
            self.state.finish_job(job_id, dataset=dataset)
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            self.state.log(job_id, message, "ERROR")
            self.state.finish_job(job_id, error=message)
