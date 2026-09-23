from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import requests


PUBCHEM_PROPERTIES = "Title,IUPACName,MolecularFormula,InChIKey"
PUBCHEM_URL = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/smiles/property/{PUBCHEM_PROPERTIES}/JSON"


class PubChemResolver:
    """Resolve a reference SMILES to human-readable PubChem metadata.

    Results are cached locally because the original Zenodo Parquet contains no
    compound names and the public PubChem service should not be called for the
    same candidate on every spectrum search.
    """

    def __init__(self, data_dir: Path, timeout_seconds: float = 12.0) -> None:
        self.database_path = data_dir / "pubchem-cache.sqlite3"
        self.timeout_seconds = timeout_seconds
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pubchem_cache (
                  smiles TEXT PRIMARY KEY,
                  result_json TEXT NOT NULL
                )
                """
            )

    def resolve(self, smiles: str) -> dict[str, object]:
        query = smiles.strip()
        if not query:
            raise ValueError("SMILES is required")
        if len(query) > 2048:
            raise ValueError("SMILES is too long")

        with self._connect() as connection:
            row = connection.execute(
                "SELECT result_json FROM pubchem_cache WHERE smiles = ?", (query,)
            ).fetchone()
        if row:
            cached = json.loads(row[0])
            return {**cached, "cache": "hit"}

        try:
            response = requests.post(
                PUBCHEM_URL,
                data={"smiles": query},
                headers={"Accept": "application/json"},
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as error:
            raise RuntimeError("PubChem metadata service is unavailable") from error

        if response.status_code == 404:
            result: dict[str, object] = {
                "found": False,
                "source": "PubChem PUG REST",
                "smiles": query,
            }
        elif not response.ok:
            raise RuntimeError(f"PubChem returned HTTP {response.status_code}")
        else:
            try:
                properties = response.json()["PropertyTable"]["Properties"]
                item = properties[0] if properties else {}
            except (KeyError, IndexError, TypeError, ValueError) as error:
                raise RuntimeError("PubChem returned an unexpected metadata response") from error
            result = {
                "found": True,
                "source": "PubChem PUG REST",
                "smiles": query,
                "cid": item.get("CID"),
                "title": item.get("Title"),
                "iupacName": item.get("IUPACName"),
                "molecularFormula": item.get("MolecularFormula"),
                "inchiKey": item.get("InChIKey"),
            }

        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO pubchem_cache (smiles, result_json) VALUES (?, ?)",
                (query, json.dumps(result, ensure_ascii=False)),
            )
        return {**result, "cache": "miss"}
