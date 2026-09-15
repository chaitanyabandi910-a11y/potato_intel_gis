# Potato Intel — GEE Indices & Terrain API

A Flask API that computes vegetation/water/radar indices and terrain layers
(DEM, slope, aspect, flow direction/accumulation) from Google Earth Engine,
returning ready-to-use XYZ tile URLs for map display. Both are served from
the same app (`api.py`) — one process, one port. Built for integration into
the Potato Intel platform.

## Indices

Computed via `POST /api/index` over an AOI + date range (satellite imagery,
so it varies over time).

| Index | Group | Source | Formula |
|---|---|---|---|
| NDVI | Vegetation | Sentinel-2 | `(N - R) / (N + R)` |
| GNDVI | Vegetation | Sentinel-2 | `(N - G) / (N + G)` |
| NDRE | Vegetation | Sentinel-2 | `(N - RE1) / (N + RE1)` |
| EVI | Vegetation | Sentinel-2 | `2.5 * (N - R) / (N + 6R - 7.5B + 1)` |
| SAVI | Vegetation | Sentinel-2 | `(1 + L) * (N - R) / (N + R + L)`, `L = 0.5` |
| MSAVI | Vegetation | Sentinel-2 | `(2N + 1 - sqrt((2N + 1)^2 - 8(N - R))) / 2` |
| MTCI | Vegetation | Sentinel-2 | `(RE2 - RE1) / (RE1 - R)` |
| NDVI_SAR (RVI) | Vegetation | Sentinel-1 | `(4 * VH) / (VV + VH)` on linear-power VV/VH (dB → linear), clamped to `[0, 1]` |
| NDWI | Moisture | Sentinel-2 | `(G - N) / (G + N)` |
| NDMI | Moisture | Sentinel-2 | `(N - S1) / (N + S1)` |
| LSWI | Moisture | Sentinel-2 | `(N - S2) / (N + S2)` |
| SM_RELATIVE | Moisture | Sentinel-1 | Wagner change-detection: temporal min/max normalization of VV backscatter, `[0, 1]` |
| TWI | Moisture | Sentinel-1 + Copernicus DEM + MERIT Hydro | Seasonal/dynamic: static topographic TWI × `SM_RELATIVE` for the requested date range — see below |
| TRUE_COLOR | — | Sentinel-2 | RGB composite (visualization only, not a derived index — no classification/legend) |

Band aliases (`N`, `R`, `G`, `B`, `RE1`, `RE2`, `S1`, `S2`, …) map to Sentinel-2
SR band names in `S2_BANDS`, and `VV`/`VH` map to Sentinel-1 GRD in `S1_BANDS`
— see [`indices.py`](indices.py).

### Classification / legend scheme

Every index above is grouped as **Vegetation** or **Moisture** (see
`VEGETATION_INDICES` / `MOISTURE_INDICES` in `indices.py`). Before rendering,
the raw continuous index is bucketed into 10 classes (0–9) on fixed 0.1-wide
steps by `classify_standard()`, then colored with a shared 10-color palette
per group (`VEGETATION_PALETTE`/`VEGETATION_LABELS` or
`MOISTURE_PALETTE`/`MOISTURE_LABELS`). This is what `/api/index` returns as
`vis_params` + `labels`.

**"Clouds" (grey, class 10):** every index's palette/labels carries an 11th
entry — `NO_DATA_CLASS = 10`, color `#808080`, label `"Clouds"` — for pixels
*inside* the requested AOI that have no valid data on the requested date
(cloud-masked, shadow, or simply no satellite pass covered that spot/date).
This is distinct from *outside* the AOI, which always stays fully
transparent. A date range with **zero** matching scenes at all (e.g. an
extreme monsoon week, or a 1-day window nothing passed over) used to crash
with an Earth Engine "Image with no bands" error — `_ensure_bands()` /
`_ensure_collection()` in `api.py` now guarantee a properly-banded, fully
masked placeholder in that case, so it flows into the same grey "Clouds"
rendering instead of failing the request. `TRUE_COLOR` gets the equivalent
treatment via a grey RGB fill rather than a palette class (see
`get_true_color()` in `indices.py`).

### Why TWI lives here, not with the static terrain layers

The classic TWI formula (`ln(As / tan(slope))`, in `terrain.get_twi()`) is
purely topographic — upstream catchment area and slope, both time-invariant.
On its own it's a static "which spots tend to pool water" map, not something
tied to a date. But actual wetness also depends on how wet the ground
currently *is* — the same low-lying spot is only actually wet if it's rained
recently. So `/api/index`'s `TWI` combines both:

```
TWI (for a date range) = static_topographic_twi(aoi) × SM_RELATIVE(aoi, start_date, end_date)
```

`SM_RELATIVE` is the same Sentinel-1-based relative soil moisture index
already in this table (0–1). A topographically low-lying area only scores
high TWI here if the requested period was also actually wet; the identical
terrain in a dry period scores lower. `SM_RELATIVE`'s own masking (no radar
coverage for that date range) propagates through the multiply, so "no data"
correctly means "no moisture reading for this period," not "bad terrain
data" — flowing into the same grey "Clouds" handling as every other index
(`classify_twi()` in `terrain.py`, not `classify_standard()`, since TWI's
value range is 0–20 rather than the other indices' roughly 0–1).

Takes the same `orbit_pass` parameter as other Sentinel-1-based indices
(`NDVI_SAR`, `SM_RELATIVE`).

## Terrain layers

Computed via `POST /api/farms/<farm_id>/dem/generate` over a farm boundary —
no date range, since these are genuinely static (unlike satellite imagery
and unlike TWI, which needs a date - see above). Full request/response
details in [Terrain (DEM) API](#terrain-dem-api) below.

| Layer | Source | What it is |
|---|---|---|
| `dem` | Copernicus DEM GLO-30 | Elevation (m) |
| `slope` | derived from `dem` | Slope, classified `Very Flat`/`Gentle`/`Moderate`/`Steep` |
| `aspect` | derived from `dem` | Compass direction the surface faces (8 directions + Flat) |
| `flow_direction` | MERIT Hydro | D8 flow direction, same 8-compass + Flat scheme as `aspect` |
| `flow_accumulation` | MERIT Hydro | Upstream drainage area (km²), log-scaled for display |

See [`terrain.py`](terrain.py) for the implementation.

## Project layout

```
Functions/
├── api.py                   # Flask API
├── indices.py                # Vegetation/moisture/radar index functions + palettes
├── terrain.py                 # DEM/slope/aspect/flow terrain functions + palettes (+ TWI's topographic building block)
├── config/
│   └── ee_auth.py           # GEE service-account auth (initializes on import)
├── credentials/
│   └── service-account.json # GEE key — gitignored, never commit this
├── requirements.txt
└── .gitignore
```

## Setup

```bash
cd Functions
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Place your GEE service-account JSON key at `credentials/service-account.json`
(or point the `GEE_SERVICE_ACCOUNT_FILE` env var at a different path). This
file is gitignored — **never commit it**.

## Run locally

```bash
source venv/bin/activate
python api.py
```

Server starts on `http://127.0.0.1:5000`. This runs Flask's built-in dev
server — for production, run behind a real WSGI server (gunicorn, etc.), see
[Deployment](#deployment).

## API

### `GET /health`
Liveness check → `{"status": "ok"}`.

### `GET /api/indices`
Lists all available index keys, e.g.
`{"indices": ["EVI", "GNDVI", "LSWI", ..., "TRUE_COLOR", "TWI"]}`.

### `GET /api/terrain/layers`
Lists the *static* terrain layer keys returned by the DEM endpoint below
(`TWI` is date-dependent, so it's listed under `/api/indices` instead - see
[Why TWI lives here](#why-twi-lives-here-not-with-the-static-terrain-layers)):
`{"layers": ["dem", "slope", "aspect", "flow_direction", "flow_accumulation"]}`.

### `POST /api/index`
Computes one index over an AOI and returns a tile URL for map display.

Request body:
```json
{
  "index": "NDVI",
  "aoi": { "type": "Point", "coordinates": [72.5714, 23.0225] },
  "start_date": "2026-07-18",
  "end_date": "2026-08-17",
  "cloud": 20,
  "orbit_pass": "DESCENDING"
}
```

| Field | Required | Applies to | Notes |
|---|---|---|---|
| `index` | yes | all | one of the keys from `/api/indices` |
| `aoi` | yes | all | any GeoJSON geometry (Point, Polygon, …) |
| `start_date` / `end_date` | yes | all | `YYYY-MM-DD` |
| `cloud` | no (default `20`) | optical indices only | max `CLOUDY_PIXEL_PERCENTAGE` scene filter |
| `orbit_pass` | no (default: auto) | radar indices only (`NDVI_SAR`, `SM_RELATIVE`) | `"ASCENDING"` or `"DESCENDING"`; omitted = auto-fallback (tries ASCENDING, then DESCENDING, then unfiltered) |

Response:
```json
{
  "index": "NDVI",
  "tile_url": "https://earthengine.googleapis.com/v1/projects/.../tiles/{z}/{x}/{y}",
  "vis_params": { "min": 0, "max": 10, "palette": ["#8B0000", "...", "#808080"] },
  "labels": ["≤ 0.0 | Bare Soil / Water", "...", "Clouds"]
}
```

`tile_url` is an XYZ template — drop it straight into Leaflet
(`L.tileLayer(tile_url).addTo(map)`), Mapbox GL, or any map library that
takes a raw XYZ URL. `vis_params` + `labels` are what you need to render a
matching legend on the frontend.

Optical indices are computed from a Sentinel-2 SR Harmonized composite that's
cloud/shadow/cirrus/snow-masked (s2cloudless + SCL) and reflectance-scaled
(÷10000) before the median composite. Radar indices are computed from a
Sentinel-1 GRD composite, filtered to IW mode, dual VV+VH polarization, and a
single orbit pass (explicit or auto-fallback).

## Extending — adding a new index

1. Write a `calculate_X(image, bands=S2_BANDS)` function in `indices.py`
   (or `calculate_X(collection, bands=S1_BANDS)` if it needs the raw
   collection, e.g. for a temporal statistic like `SM_RELATIVE`).
2. Register it in `index_functions` (single-image indices) or
   `collection_functions` (collection-based indices).
3. If it's radar-based, add its key to `RADAR_INDICES`.
4. Add its key to `VEGETATION_INDICES` or `MOISTURE_INDICES` to get the
   shared classification + palette + labels for free. Leaving it out of both
   falls back to a generic continuous palette (`DEFAULT_VIS` in `api.py`).

No changes to `api.py` are needed beyond that — `/api/indices` and
`/api/index` pick up new entries automatically.

## Terrain (DEM) API

Static terrain layers derived from Copernicus DEM GLO-30 (elevation/slope/
aspect) and MERIT Hydro (flow direction/accumulation) — one call per farm
boundary, no date range (these don't change like satellite imagery). `TWI`
is the exception - it's seasonal, so it lives on `/api/index` instead; see
[Why TWI lives here](#why-twi-lives-here-not-with-the-static-terrain-layers).

### `POST /api/farms/<farm_id>/dem/generate`

Request body:
```json
{
  "boundary": {
    "type": "Polygon",
    "coordinates": [[[73.75, 20.00], [73.76, 20.00], [73.76, 20.01], [73.75, 20.01], [73.75, 20.00]]]
  }
}
```
`boundary` is any GeoJSON Polygon or MultiPolygon.

Response:
```json
{
  "farm_id": "FARM123",
  "source": "Copernicus DEM GLO-30",
  "resolution": "30m",
  "boundary_hash": "571b08ba9e9eaf5f4f8f1eba981afcec687ccb13251db476d12a3ff775adc7fb",
  "layers": {
    "dem": "https://earthengine.googleapis.com/.../tiles/{z}/{x}/{y}",
    "slope": "...",
    "aspect": "...",
    "flow_direction": "...",
    "flow_accumulation": "..."
  },
  "legends": {
    "dem": { "vis_params": { "min": 0, "max": 9, "palette": ["#006400", "..."] }, "labels": ["0-100 m | Very Low Elevation", "..."] },
    "slope": { "vis_params": {...}, "labels": ["0-2° | Very Flat", "..."] },
    "aspect": { "vis_params": {...}, "labels": ["North", "North-East", "...", "Flat"] },
    "flow_direction": { "vis_params": {...}, "labels": ["North", "...", "Flat"] },
    "flow_accumulation": { "vis_params": { "min": 0, "max": 15, "palette": ["#f7fbff", "..."] }, "labels": null }
  },
  "statistics": {
    "min_elevation": 589.5,
    "max_elevation": 622.98,
    "avg_elevation": 602.74,
    "mean_slope": 3.42,
    "max_slope": 14.04,
    "dominant_aspect": "North"
  }
}
```

`layers.*` are XYZ tile templates (same as `/api/index`'s `tile_url`) — drop
straight into a map. `legends.*` gives you the vis params + labels to render
each layer's legend on the dashboard toggle list.

**How each layer is derived:**
- `dem` / `slope` / `aspect`: Copernicus DEM GLO-30, `ee.Terrain.slope`/
  `.aspect()`. GLO-30 is tiled with no single native projection, so the
  mosaic is pinned to a real ~30m grid (`setDefaultProjection`) *before*
  slope/aspect are derived — skipping this silently breaks both (near-zero
  slope, fully masked once clipped).
- `flow_direction` / `flow_accumulation`: MERIT Hydro's precomputed D8 flow
  direction (`dir` band) and upstream drainage area in km² (`upa` band) —
  this is a real global hydrological computation, not re-derived per farm
  boundary, so a small field near a large drainage network can correctly
  show high flow accumulation even though the "flow" happens far upstream.
- `flow_direction` and `aspect` share the same 8-compass-direction + "Flat"
  classification/palette (`ASPECT_LABELS`/`ASPECT_PALETTE` in `terrain.py`),
  since both are fundamentally "which way does this pixel face/drain".

### Caching — what's this service's job vs. the main backend's

This endpoint is **stateless**: every call recomputes and returns fresh tile
URLs. That's intentional and cheap — `getMapId` doesn't do the actual
raster computation, it just returns a tile template that Earth Engine
computes lazily per tile as the map is panned/zoomed.

The *"don't reprocess DEM for a farm whose boundary hasn't changed"* caching
described in the original spec belongs in the Node.js backend + its own
database, not here — this Python service has no database and shouldn't need
one for a static-per-boundary computation. The split:

- **This service** computes `boundary_hash` (`sha256` of the boundary
  GeoJSON, `json.dumps(..., sort_keys=True, separators=(",", ":"))` before
  hashing — see `compute_boundary_hash()` in `terrain.py`) and returns it in
  the response so Node can store it as-is.
- **Node + Postgres** (or whatever the main DB is) owns a `farm_dem_layers`
  table (`farm_id`, `dem_source`, `dem_resolution`, the 5 `*_raster_url`
  columns for the genuinely static layers, the statistics columns,
  `boundary_hash`, `created_at`, `updated_at` — as in the original spec, minus
  `twi` which is no longer part of this static payload) and implements the
  actual cache check: hash the incoming boundary the same way, compare to the
  stored `boundary_hash`, and only call this endpoint when they differ (or
  none is stored yet). Since GeoJSON round-trips through JSON identically in
  both languages, hashing the same canonical JSON string in Node
  (`JSON.stringify` with sorted keys) gives the same hash as this service's
  Python implementation — reimplement the identical canonicalization on the
  Node side rather than calling into Python just to get a hash.
  `TWI` (from `/api/index`) is date-dependent and isn't a good fit for this
  same per-boundary cache - if you want to cache it too, key it by
  `(farm_id, start_date, end_date)` instead, same as you would for any other
  `/api/index` result.

## Integrating with a Node.js backend

This API is a separate Python/Flask service — treat it as a microservice
your Node backend calls over HTTP, the same way you'd call any third-party
API. Keep the GEE service-account key on this Python service only; never
forward it to Node or the frontend.

**Typical architecture:**

```
Browser/App  →  Node.js backend  →  Flask index API  →  Google Earth Engine
                 (Potato Intel)      (this project)
```

Node calls this service server-side, optionally caches the result, and
forwards `tile_url` (+ `vis_params`/`labels`) to the frontend map.

**Example (Node.js, using `axios`):**

```js
const axios = require('axios');

const INDEX_API_URL = process.env.INDEX_API_URL || 'http://127.0.0.1:5000';
const INDEX_API_KEY = process.env.INDEX_API_KEY; // set only if the service has API_KEY enabled

const indexApiHeaders = INDEX_API_KEY ? { 'X-API-Key': INDEX_API_KEY } : {};

async function getIndexTile({ index, aoi, startDate, endDate, cloud, orbitPass }) {
  const { data } = await axios.post(
    `${INDEX_API_URL}/api/index`,
    { index, aoi, start_date: startDate, end_date: endDate, cloud, orbit_pass: orbitPass },
    { headers: indexApiHeaders },
  );
  return data; // { index, tile_url, vis_params, labels }
}

// Express route example
app.post('/api/fields/:fieldId/index/:indexName', async (req, res) => {
  try {
    const field = await getFieldGeometry(req.params.fieldId); // your own lookup
    const result = await getIndexTile({
      index: req.params.indexName,
      aoi: field.geometry, // GeoJSON geometry
      startDate: req.body.startDate,
      endDate: req.body.endDate,
      cloud: req.body.cloud,
    });
    res.json(result);
  } catch (err) {
    res.status(502).json({ error: 'Index service unavailable', detail: err.message });
  }
});
```

**Example: the DEM endpoint with the boundary-hash cache check** (the caching
described in [Terrain (DEM) API](#terrain-dem-api) — this lives in Node, not
the Python service):

```js
const crypto = require('crypto');

function boundaryHash(boundaryGeoJSON) {
  // Must match terrain.py's compute_boundary_hash() canonicalization exactly.
  const canonical = JSON.stringify(sortKeysDeep(boundaryGeoJSON));
  return crypto.createHash('sha256').update(canonical).digest('hex');
}

app.post('/api/fields/:fieldId/terrain', async (req, res) => {
  const field = await getFieldGeometry(req.params.fieldId);
  const hash = boundaryHash(field.geometry);

  const cached = await db.farmDemLayers.findOne({ where: { farm_id: field.id } });
  if (cached && cached.boundary_hash === hash) {
    return res.json(cached); // boundary unchanged - skip the GEE call entirely
  }

  const { data } = await axios.post(
    `${INDEX_API_URL}/api/farms/${field.id}/dem/generate`,
    { boundary: field.geometry },
  );

  await db.farmDemLayers.upsert({
    farm_id: field.id,
    dem_source: data.source,
    dem_resolution: data.resolution,
    boundary_hash: data.boundary_hash,
    dem_raster_url: data.layers.dem,
    slope_raster_url: data.layers.slope,
    aspect_raster_url: data.layers.aspect,
    flow_direction_raster_url: data.layers.flow_direction,
    flow_accumulation_raster_url: data.layers.flow_accumulation,
    min_elevation: data.statistics.min_elevation,
    max_elevation: data.statistics.max_elevation,
    avg_elevation: data.statistics.avg_elevation,
    mean_slope: data.statistics.mean_slope,
    max_slope: data.statistics.max_slope,
    dominant_aspect: data.statistics.dominant_aspect,
  });

  res.json(data);
});
```

`sortKeysDeep` (recursively sort object keys before stringifying) isn't a
built-in — use the `json-stable-stringify` npm package instead of hand-rolling
it, since Python's `sort_keys=True` and a naive JS key-sort can disagree on
edge cases (nested arrays of objects, key ordering within GeoJSON coordinate
arrays, etc.) and silently produce a different hash for the same boundary.

**Fetch, if you'd rather avoid the axios dependency:**

```js
const res = await fetch(`${INDEX_API_URL}/api/index`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ index: 'NDVI', aoi, start_date, end_date }),
});
if (!res.ok) throw new Error((await res.json()).error);
const data = await res.json();
```

**Notes:**
- Set `INDEX_API_URL` as an env var in Node so local dev, staging, and prod
  can point at different hosts for this service without code changes.
- `GET /api/indices` is handy to populate a dropdown or validate `indexName`
  before calling `/api/index`.
- If the service has `API_KEY` set (see [Production readiness](#production-readiness)),
  every call from Node needs the `X-API-Key` header, as shown above.
- CORS is open by default (fine for local dev) but configurable via
  `ALLOWED_ORIGINS` - moot for Node-to-Node calls either way, since CORS is a
  browser-only mechanism and doesn't apply to server-to-server requests.
- Earth Engine calls can take a few seconds; don't block a user-facing
  request on this synchronously without a loading state / timeout on the
  Node side.

## Production readiness

This service went through a hardening pass beyond the happy path. What's
covered and what's still your responsibility:

### Error handling

Every error response is JSON: `{"error": "..."}`. Status codes:

| Status | Meaning |
|---|---|
| 400 | Bad input - missing/malformed field, invalid GeoJSON, bad date, cloud out of range, unknown `orbit_pass`, unknown index, AOI over the area cap, malformed request body |
| 401 | Missing/wrong `X-API-Key` (only enforced if `API_KEY` is set - see below) |
| 404 | Unknown route |
| 413 | Request body over `MAX_CONTENT_LENGTH` (2MB) |
| 422 | Valid request, but no data for it (e.g. a DEM boundary entirely over open ocean) |
| 500 | Unexpected internal error (logged server-side; message is generic, no internals leaked) |

Earth Engine's own exceptions (bad geometry EE itself rejects, no imagery
found, etc.) are caught and surfaced as 400s with EE's own descriptive
message - safe to expose, these are about the request, not the server.

### Input validation

Before anything touches Earth Engine: `start_date`/`end_date` must be real
`YYYY-MM-DD` dates, `cloud` must be numeric in `[0, 100]`, `orbit_pass` (if
given) must be `ASCENDING`/`DESCENDING`, `aoi`/`boundary` must be a
`Point`/`Polygon`/`MultiPolygon` GeoJSON geometry with a non-empty
`coordinates` array, and its bounding box must be under `MAX_AOI_AREA_KM2`
(default 5000 km² - a pure-Python bbox estimate, no extra EE round-trip,
just a coarse cost/abuse cap). `farm_id` must be non-empty and ≤128 chars.

### Auth & CORS

Both are opt-in via env vars so local dev stays frictionless, but should be
set in any deployment reachable outside your own network:

- **`API_KEY`** - if set, every route except `/health` requires a matching
  `X-API-Key` header, or it 401s. Unset by default (open access), since this
  is meant to run behind Node on a private network in most deployments - but
  set it if the service has any public exposure at all, since an unauthenticated
  instance lets anyone run billable Earth Engine computations through it.
- **`ALLOWED_ORIGINS`** - comma-separated list of allowed origins for CORS
  (e.g. `https://app.potatointel.com,https://staging.potatointel.com`). If
  unset, CORS allows any origin (fine for local dev, not fine in production
  if browsers ever call this directly - Node-to-Node calls aren't subject to
  CORS at all, so this only matters if a frontend calls this service directly).

### Other config (env vars, all optional)

| Var | Default | Purpose |
|---|---|---|
| `GEE_SERVICE_ACCOUNT_FILE` | `credentials/service-account.json` | path to the GEE key |
| `API_KEY` | unset (disabled) | shared-secret auth, see above |
| `ALLOWED_ORIGINS` | unset (any origin) | CORS allowlist, see above |
| `MAX_AOI_AREA_KM2` | `5000` | reject AOIs with a larger bounding box |
| `FLASK_DEBUG` | `false` | **never enable in production** - turns on Werkzeug's interactive debugger, which allows arbitrary code execution if an unhandled exception is hit and the debugger PIN leaks |
| `HOST` | `127.0.0.1` | bind address for `python api.py` (use `0.0.0.0` in a container) |
| `PORT` | `5000` | bind port for `python api.py` |

`MAX_CONTENT_LENGTH` (request body cap, 2MB) isn't an env var - it's generous
for any real GeoJSON boundary; change it directly in `api.py` if you have an
unusual case.

### Logging

Basic structured logging is configured (`logging.basicConfig`, INFO level).
Unexpected errors are logged server-side with full tracebacks
(`logger.exception`) even though the client only sees a generic 500 message.

### What's still on you

- **Rate limiting** isn't implemented in-process - put this behind a gateway/
  reverse proxy (or Node itself) that rate-limits if it's reachable outside
  a trusted network, since each request can trigger real Earth Engine compute.
- **Concurrency**: run gunicorn with multiple **worker processes** (`-w N`),
  not threads within one process - the earthengine-api client isn't verified
  thread-safe for concurrent use within a single process, but separate
  worker processes each get their own clean `ee.Initialize()` at import time.
- **Secrets**: `credentials/service-account.json` should never end up in an
  image layer or repo - inject it at deploy time (mounted secret, volume, or
  point `GEE_SERVICE_ACCOUNT_FILE` at wherever your platform puts it).
- **Monitoring/alerting** on 5xx rates and Earth Engine quota usage is outside
  this service's scope - add it at whatever layer you already use for the
  rest of Potato Intel.

## Deployment

The dev server (`app.run()`) is not for production regardless of `FLASK_DEBUG`.
Run it with a real WSGI server:

```bash
pip install gunicorn
API_KEY=... ALLOWED_ORIGINS=https://app.potatointel.com \
  gunicorn -w 2 -b 0.0.0.0:5000 api:app
```

Keep `credentials/service-account.json` off the machine image / repo and
inject it at deploy time (secret manager, mounted volume, or the
`GEE_SERVICE_ACCOUNT_FILE` env var pointing at wherever it's provisioned).
