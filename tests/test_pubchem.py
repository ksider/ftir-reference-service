from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.pubchem import PubChemResolver


class PubChemResolverTests(unittest.TestCase):
    def test_resolve_caches_successful_property_response(self) -> None:
        response = Mock()
        response.status_code = 200
        response.ok = True
        response.json.return_value = {
            "PropertyTable": {
                "Properties": [{
                    "CID": 702,
                    "Title": "Ethanol",
                    "IUPACName": "ethanol",
                    "MolecularFormula": "C2H6O",
                    "InChIKey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
                }]
            }
        }
        with tempfile.TemporaryDirectory() as directory, patch("app.pubchem.requests.post", return_value=response) as post:
            resolver = PubChemResolver(Path(directory))
            first = resolver.resolve("CCO")
            second = resolver.resolve("CCO")

        self.assertTrue(first["found"])
        self.assertEqual(first["title"], "Ethanol")
        self.assertEqual(first["cache"], "miss")
        self.assertEqual(second["cache"], "hit")
        post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
