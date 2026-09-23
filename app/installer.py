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


class ZenodoInstaller:
    def __init__(self, settings: Settings, state: ServiceState):
        self.settings = settings
        self.state = state

    def install(self, job_id: str) -> None:
        def log(message: str, level: str = "INFO") -> None:
            self.state.log(job_id, message, level)

        try:
            log(f"Loading Zenodo record {self.settings.zenodo_record_id}")
            response = requests.get(ZENODO_API.format(record_id=self.settings.zenodo_record_id), timeout=(10, 60))
            response.raise_for_status()
            record = response.json()
            files = [item for item in record.get("files", []) if IR_FILE_PATTERN.match(item.get("key", ""))]
            if len(files) != 9:
                raise RuntimeError(f"Expected 9 IR Parquet files, received {len(files)} from Zenodo")
            files.sort(key=lambda item: item["key"])
            self.settings.source_dir.mkdir(parents=True, exist_ok=True)
            (self.settings.source_dir / "zenodo-manifest.json").write_text(json.dumps(record, indent=2), encoding="utf-8")

            for item in files:
                self._download_file(item, log)

            self.state.set_status("indexing")
            log("All Parquet files verified. Building local vector index.")
            manifest = build_catalog(
                [self.settings.source_dir / item["key"] for item in files],
                self.settings.index_dir,
                self.settings.vector_points,
                log,
            )
            dataset = {
                "recordId": self.settings.zenodo_record_id,
                "doi": "10.5281/zenodo.16417648",
                "referenceType": "computed",
                "license": "CDLA-Permissive-2.0",
                "files": [item["key"] for item in files],
                "catalog": manifest,
            }
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

    def install(self, job_id: str) -> None:
        def log(message: str, level: str = "INFO") -> None:
            self.state.log(job_id, message, level)

        try:
            files = sorted(path for path in self.settings.source_dir.glob("*.parquet") if IR_FILE_PATTERN.match(path.name))
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
            log("Local catalogue is ready. Search results cover only the supplied chunk(s).")
            self.state.finish_job(job_id, dataset=dataset)
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            self.state.log(job_id, message, "ERROR")
            self.state.finish_job(job_id, error=message)
