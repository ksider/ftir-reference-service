from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import pyarrow.parquet as pq


CATALOG_VERSION = "zenodo-ir-v1"
X_MIN_CM1 = 500.0
X_MAX_CM1 = 4000.0


def _normalise(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    if not np.isfinite(values).all():
        valid = np.flatnonzero(np.isfinite(values))
        if valid.size < 2:
            return np.zeros(values.size, dtype=np.float32)
        values = np.interp(np.arange(values.size), valid, values[valid])
    values = values - np.quantile(values, 0.05)
    scale = float(np.linalg.norm(values))
    if scale <= 1e-12:
        return np.zeros(values.size, dtype=np.float32)
    return (values / scale).astype(np.float32)


def resample_and_normalise(points: Iterable[Iterable[float]], grid: np.ndarray, signal_type: str = "absorbance") -> np.ndarray:
    array = np.asarray(list(points), dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != 2:
        raise ValueError("points must be [[wavenumber, intensity], ...]")
    array = array[np.isfinite(array).all(axis=1)]
    if array.shape[0] < 8:
        raise ValueError("At least 8 finite spectrum points are required")
    array = array[np.argsort(array[:, 0])]
    x, y = array[:, 0], array[:, 1]
    unique_x, unique_indices = np.unique(x, return_index=True)
    y = y[unique_indices]
    if unique_x.size < 8:
        raise ValueError("At least 8 unique wavenumbers are required")
    if signal_type.lower() in {"transmittance", "%t", "t"}:
        # Percent values are accepted alongside fractions. Clamp avoids log(0).
        transmittance = y / 100.0 if float(np.nanmax(y)) > 1.5 else y
        y = -np.log10(np.clip(transmittance, 1e-6, 1.0))
    return _normalise(np.interp(grid, unique_x, y, left=y[0], right=y[-1]))


def build_catalog(
    parquet_files: list[Path],
    index_dir: Path,
    vector_points: int,
    log: Callable[[str], None],
) -> dict[str, object]:
    """Build a portable mmap matrix and SQLite metadata catalogue from Parquet."""
    index_dir.mkdir(parents=True, exist_ok=True)
    total_rows = sum(pq.ParquetFile(path).metadata.num_rows for path in parquet_files)
    grid = np.linspace(X_MIN_CM1, X_MAX_CM1, vector_points, dtype=np.float64)
    temp_vectors = index_dir / "vectors.building.npy"
    temp_database = index_dir / "metadata.building.sqlite3"
    temp_vectors.unlink(missing_ok=True)
    temp_database.unlink(missing_ok=True)
    vectors = np.lib.format.open_memmap(temp_vectors, mode="w+", dtype=np.float32, shape=(total_rows, vector_points))
    connection = sqlite3.connect(temp_database)
    connection.execute(
        "CREATE TABLE references_catalog (row_index INTEGER PRIMARY KEY, reference_id TEXT, smiles TEXT, source_file TEXT)"
    )
    connection.execute("CREATE INDEX idx_reference_id ON references_catalog(reference_id)")
    row_index = 0
    try:
        for source_file in parquet_files:
            log(f"Indexing {source_file.name}")
            parquet = pq.ParquetFile(source_file)
            for batch in parquet.iter_batches(
                batch_size=256,
                columns=["id", "smiles", "Frequency(cm^-1)", "ir_spectra"],
            ):
                for item in batch.to_pylist():
                    try:
                        frequencies = item.get("Frequency(cm^-1)")
                        intensities = item.get("ir_spectra")
                        if not frequencies or not intensities:
                            raise ValueError("missing frequency or intensity values")
                        spectrum = np.column_stack((frequencies, intensities))
                        vectors[row_index] = resample_and_normalise(spectrum, grid)
                    except (TypeError, ValueError):
                        vectors[row_index] = np.zeros(vector_points, dtype=np.float32)
                    connection.execute(
                        "INSERT INTO references_catalog VALUES (?, ?, ?, ?)",
                        (row_index, str(item.get("id", "")), str(item.get("smiles", "")), source_file.name),
                    )
                    row_index += 1
                if row_index % 2048 == 0:
                    connection.commit()
                    log(f"Indexed {row_index:,} / {total_rows:,} spectra")
        connection.commit()
        vectors.flush()
    finally:
        connection.close()
        del vectors

    if row_index != total_rows:
        raise RuntimeError(f"Index row count mismatch: {row_index} != {total_rows}")
    final_vectors = index_dir / "vectors.npy"
    final_database = index_dir / "metadata.sqlite3"
    temp_vectors.replace(final_vectors)
    temp_database.replace(final_database)
    np.save(index_dir / "x-axis.npy", grid)
    manifest = {
        "catalogVersion": CATALOG_VERSION,
        "referenceType": "computed",
        "rows": total_rows,
        "vectorPoints": vector_points,
        "xRangeCm1": [X_MIN_CM1, X_MAX_CM1],
        "vectors": final_vectors.name,
        "database": final_database.name,
    }
    (index_dir / "catalog-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


class ReferenceCatalog:
    def __init__(self, index_dir: Path):
        manifest_path = index_dir / "catalog-manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError("Reference catalogue is not built")
        self.index_dir = index_dir
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.grid = np.load(index_dir / "x-axis.npy")
        self.vectors = np.load(index_dir / self.manifest["vectors"], mmap_mode="r")
        self.database_path = index_dir / self.manifest["database"]

    def search(self, points: list[list[float]], signal_type: str, top_k: int) -> list[dict[str, object]]:
        query = resample_and_normalise(points, self.grid, signal_type)
        if not np.any(query):
            raise ValueError("Spectrum has no usable variation after normalisation")
        scores = np.empty(self.vectors.shape[0], dtype=np.float32)
        batch_size = 8192
        for start in range(0, self.vectors.shape[0], batch_size):
            stop = min(start + batch_size, self.vectors.shape[0])
            scores[start:stop] = self.vectors[start:stop] @ query
        count = min(max(1, top_k), scores.size)
        indices = np.argpartition(scores, -count)[-count:]
        indices = indices[np.argsort(scores[indices])[::-1]]
        connection = sqlite3.connect(self.database_path)
        try:
            result = []
            for row_index in indices.tolist():
                row = connection.execute(
                    "SELECT reference_id, smiles, source_file FROM references_catalog WHERE row_index = ?", (row_index,)
                ).fetchone()
                result.append(
                    {
                        "id": row[0],
                        "smiles": row[1],
                        "sourceFile": row[2],
                        "score": round(float(scores[row_index]), 6),
                        "referenceType": "computed",
                        "source": "Zenodo 10.5281/zenodo.16417648",
                        "license": "CDLA-Permissive-2.0",
                    }
                )
            return result
        finally:
            connection.close()

    def get(self, reference_id: str) -> dict[str, object] | None:
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute(
                "SELECT row_index, reference_id, smiles, source_file FROM references_catalog WHERE reference_id = ? LIMIT 1",
                (reference_id,),
            ).fetchone()
            if not row:
                return None
            vector = self.vectors[row[0]].astype(float).round(7).tolist()
            return {
                "id": row[1],
                "smiles": row[2],
                "sourceFile": row[3],
                "x": self.grid.round(4).tolist(),
                "normalisedIntensity": vector,
                "referenceType": "computed",
                "source": "Zenodo 10.5281/zenodo.16417648",
                "license": "CDLA-Permissive-2.0",
            }
        finally:
            connection.close()
