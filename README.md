# FTIR Reference Spectra Service

Separate API service for finding **hypothetical** matches between a user FTIR
spectrum and the computed IR spectra from Zenodo record
[10.5281/zenodo.16417648](https://doi.org/10.5281/zenodo.16417648).

It is intentionally separate from the FTIR Analyzer:

- this service owns the 8.1 GB Zenodo dataset, its local index and its logs;
- the existing FTIR server will later call this API with a server-to-server
  token;
- the static browser frontend must not hold the service token or call this
  service directly;
- a match is supporting evidence only, never an experimental identification or
  proof that a reaction passed.

The Zenodo data are computed spectra for 177,461 molecules, not measured ATR
or transmission spectra. The service keeps the original provenance and marks
every result as `computed`.

## First start

1. Copy `.env.example` to `.env`.
2. Replace both tokens with different, long random strings. Do not commit
   `.env`.
3. Start the service:

   ```bash
   docker compose up -d --build
   ```

4. Open `http://localhost:8088` and paste `ADMIN_TOKEN` into the page.
5. Read and accept the dataset licence notice, then click **Download and build
   index**.

The installer obtains the nine `IR_data_chunkXXX_of_009.parquet` files directly
from the versioned Zenodo record. They are already Parquet files, so there is
no archive to unpack. It verifies Zenodo's published MD5 checksum for every
file and can resume an interrupted download from a `.part` file.

Reserve at least **20 GB** of free disk space for source files, the index and
temporary build files. The Docker volume `reference-data` holds the data and
survives a container rebuild.

## Local development without Docker

For a fast development check, do not download all 8.1 GB. Place one or more
files named `IR_data_chunkXXX_of_009.parquet` in the ignored `.source/`
directory next to this README. The default `.env.example` already uses local
`./data` and `./.source`; Docker Compose overrides both paths to `/data`.

Use Python 3.10+ (3.12 recommended):

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8088 --reload
```

Open `http://127.0.0.1:8088`, enter `ADMIN_TOKEN`, and choose **Build index
from local .source**. The resulting API is fully usable, but its matches cover
only the downloaded chunk(s). Every response explicitly reports this
limitation.

`CORS_ALLOW_ORIGINS` in `.env.example` permits only local browser origins and
the `file://` origin (`null`) for this diagnostic route. Docker Compose clears
it deliberately: the deployed static frontend must use the main FTIR server as
a proxy instead of sending `SERVICE_TOKEN` from the browser.

## Status and administration

`GET /health` is public and returns one of:

- `uninitialized` — waiting for the initial installation;
- `downloading` — Zenodo download is running;
- `indexing` — the local search index is building;
- `ready` — reference searches are available;
- `failed` — inspect the protected Admin logs.

The Admin UI is at `/`. Its API routes require:

```http
Authorization: Bearer <ADMIN_TOKEN>
```

Routes:

- `GET /api/admin/status`
- `GET /api/admin/logs?tail=200`
- `GET /api/admin/jobs/:id`
- `POST /api/admin/setup` with `{ "acceptLicense": true }`

For a public deployment, place the domain behind Cloudflare Access as an
additional gate. The application token remains required, so Cloudflare Access
is not the only protection.

## Search API

Search and reference-detail routes require a separate internal service token:

```http
X-Service-Token: <SERVICE_TOKEN>
```

Example request:

```bash
curl -X POST http://127.0.0.1:8088/api/v1/search \
  -H 'Content-Type: application/json' \
  -H "X-Service-Token: $SERVICE_TOKEN" \
  --data '{
    "signalType": "transmittance",
    "topK": 5,
    "points": [[4000, 95.3], [3990, 95.1], [3980, 94.9], [3970, 94.5], [3960, 94.7], [3950, 94.8], [3940, 94.6], [3930, 94.5]]
  }'
```

Endpoints:

- `POST /api/v1/search` — top-K normalised spectral-shape matches;
- `GET /api/v1/references/:id` — metadata and the normalised curve for an
  overlay.

The first version ranks with a reproducible cosine similarity over a
normalised 500–4000 cm⁻¹ grid. It is deliberately simple and inspectable.
Future versions can add derivative distance, peak overlap and an approximate
vector index without changing the public API.

## Data and licence

The service downloads only the IR Parquet chunks; it does not download the NMR
dataset. Zenodo record 16417648 is licensed under
[CDLA-Permissive-2.0](https://cdla.dev/permissive-2-0/). If the data are shared,
the CDLA-Permissive-2.0 agreement must accompany them. The service stores the
record manifest, DOI, source filenames and catalogue version as provenance.

The dataset, generated index, `.env` and logs are excluded from Git and from
the Docker image.
