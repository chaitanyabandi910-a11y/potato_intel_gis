import logging
import math
import os
import re
from datetime import datetime

import ee
from flask import Flask, jsonify, request
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

from indices import (
    MOISTURE_INDICES,
    MOISTURE_LABELS,
    MOISTURE_PALETTE,
    RADAR_INDICES,
    TRUE_COLOR_VIS,
    VEGETATION_INDICES,
    VEGETATION_LABELS,
    VEGETATION_PALETTE,
    classify_standard,
    collection_functions,
    get_true_color,
    index_functions,
)
from terrain import (
    ASPECT_LABELS,
    ASPECT_PALETTE,
    DEM_LABELS,
    DEM_PALETTE,
    DEM_RESOLUTION,
    DEM_SOURCE_NAME,
    FLOW_ACC_VIS,
    SLOPE_LABELS,
    SLOPE_PALETTE,
    TWI_LABELS,
    TWI_VIS,
    classify_aspect,
    classify_dem,
    classify_flow_direction,
    classify_slope,
    compute_boundary_hash,
    compute_statistics,
    get_aspect,
    get_dem,
    get_flow_accumulation,
    get_flow_direction,
    get_slope,
    get_twi,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("potato_intel_api")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2MB - generous for a GeoJSON boundary

# --- Config (env-driven; sane defaults for local dev) -----------------------

API_KEY = os.getenv("API_KEY")  # unset = auth disabled (local dev only)

_allowed_origins = os.getenv("ALLOWED_ORIGINS")
if _allowed_origins:
    CORS(app, origins=[o.strip() for o in _allowed_origins.split(",") if o.strip()])
else:
    CORS(app)  # dev default: any origin. Set ALLOWED_ORIGINS in production.

MAX_AOI_AREA_KM2 = float(os.getenv("MAX_AOI_AREA_KM2", "5000"))

S2_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"
S2_CLOUD_PROB_COLLECTION = "COPERNICUS/S2_CLOUD_PROBABILITY"
S1_COLLECTION = "COPERNICUS/S1_GRD"

DEFAULT_CLOUD_PROB_THRESHOLD = 40

DEFAULT_VIS = {"min": -1, "max": 1, "palette": ["#d7191c", "#ffffc0", "#1a9641"]}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VALID_ORBIT_PASSES = {"ASCENDING", "DESCENDING"}
VALID_GEOMETRY_TYPES = {"Point", "Polygon", "MultiPolygon"}


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# --- Validation helpers -------------------------------------------------------

def _require_json():
    payload = request.get_json(silent=True)
    if payload is None:
        raise ApiError("Request body must be valid JSON")
    return payload


def _validate_date(value, field_name):
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ApiError(f"{field_name} must be a 'YYYY-MM-DD' string")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ApiError(f"{field_name} is not a valid calendar date")


def _validate_cloud(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ApiError("cloud must be a number between 0 and 100")
    if not (0 <= value <= 100):
        raise ApiError("cloud must be between 0 and 100")
    return value


def _validate_orbit_pass(value):
    if value is None:
        return None
    if value not in VALID_ORBIT_PASSES:
        raise ApiError(f"orbit_pass must be one of {sorted(VALID_ORBIT_PASSES)}")
    return value


def _validate_geometry(value, field_name):
    if not isinstance(value, dict) or "type" not in value or "coordinates" not in value:
        raise ApiError(f"{field_name} must be a GeoJSON geometry with 'type' and 'coordinates'")
    if value["type"] not in VALID_GEOMETRY_TYPES:
        raise ApiError(f"{field_name}.type must be one of {sorted(VALID_GEOMETRY_TYPES)}")
    if not isinstance(value["coordinates"], list) or not value["coordinates"]:
        raise ApiError(f"{field_name}.coordinates must be a non-empty list")

    area_km2 = _bbox_area_km2(value)
    if area_km2 > MAX_AOI_AREA_KM2:
        raise ApiError(
            f"{field_name} bounding box (~{area_km2:.0f} km²) exceeds the {MAX_AOI_AREA_KM2:.0f} km² limit"
        )


def _bbox_area_km2(geojson):
    # Coarse, pure-Python bounding-box estimate (no EE round-trip) - purely an
    # abuse/cost-control cap, not a precise geodesic area calculation.
    coords = []

    def _collect(node):
        if isinstance(node, (int, float)):
            return
        if len(node) == 2 and all(isinstance(v, (int, float)) for v in node):
            coords.append(node)
            return
        for item in node:
            _collect(item)

    _collect(geojson["coordinates"])
    if not coords:
        return 0.0
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)
    mean_lat_rad = math.radians((min_lat + max_lat) / 2)
    width_km = (max_lon - min_lon) * 111.32 * math.cos(mean_lat_rad)
    height_km = (max_lat - min_lat) * 111.32
    return abs(width_km * height_km)


# --- Composite builders --------------------------------------------------------

def _optical_image(aoi, start_date, end_date, cloud, cloud_prob_threshold=DEFAULT_CLOUD_PROB_THRESHOLD):
    s2_sr = (
        ee.ImageCollection(S2_COLLECTION)
        .filterBounds(aoi)
        .filterDate(start_date, end_date)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", cloud))
    )
    s2_clouds = (
        ee.ImageCollection(S2_CLOUD_PROB_COLLECTION)
        .filterBounds(aoi)
        .filterDate(start_date, end_date)
    )
    joined = ee.Join.saveFirst("cloud_mask").apply(
        primary=s2_sr,
        secondary=s2_clouds,
        condition=ee.Filter.equals(leftField="system:index", rightField="system:index"),
    )

    def _mask_clouds(img):
        cloud_prob = ee.Image(img.get("cloud_mask")).select("probability")
        is_not_cloud = cloud_prob.lt(cloud_prob_threshold)
        scl = img.select("SCL")
        shadow = scl.neq(3)
        cirrus = scl.neq(10)
        snow = scl.neq(11)
        return (
            img.updateMask(is_not_cloud).updateMask(shadow).updateMask(cirrus).updateMask(snow)
            .divide(10000)
            .copyProperties(img, img.propertyNames())
        )

    collection = ee.ImageCollection(joined).map(_mask_clouds)
    return collection.median().clip(aoi)


def _s1_collection(aoi, start_date, end_date, orbit_pass=None):
    collection = (
        ee.ImageCollection(S1_COLLECTION)
        .filterBounds(aoi)
        .filterDate(start_date, end_date)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
        .select(["VV", "VH"])
    )
    if orbit_pass:
        return collection.filter(ee.Filter.eq("orbitProperties_pass", orbit_pass))

    # No orbit specified: prefer ASCENDING, fall back to DESCENDING, fall back
    # to whatever's available - some AOIs only get coverage from one pass.
    ascending = collection.filter(ee.Filter.eq("orbitProperties_pass", "ASCENDING"))
    descending = collection.filter(ee.Filter.eq("orbitProperties_pass", "DESCENDING"))
    return ee.ImageCollection(
        ee.Algorithms.If(
            ascending.size().gt(0),
            ascending,
            ee.Algorithms.If(descending.size().gt(0), descending, collection),
        )
    )


def _radar_image(aoi, start_date, end_date, orbit_pass=None):
    return _s1_collection(aoi, start_date, end_date, orbit_pass).median().clip(aoi)


def _vis_for(index_name):
    if index_name in VEGETATION_INDICES:
        return {"min": 0, "max": 9, "palette": VEGETATION_PALETTE}, VEGETATION_LABELS, True
    if index_name in MOISTURE_INDICES:
        return {"min": 0, "max": 9, "palette": MOISTURE_PALETTE}, MOISTURE_LABELS, True
    return DEFAULT_VIS, None, False


# --- Auth / error handling -----------------------------------------------------

@app.before_request
def _check_api_key():
    # CORS preflight requests never carry custom headers (X-API-Key included)
    # and carry no sensitive data - they must be let through unauthenticated
    # or browser-based callers break entirely.
    if request.method == "OPTIONS" or request.path == "/health":
        return None
    if API_KEY and request.headers.get("X-API-Key") != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.errorhandler(ApiError)
def _handle_api_error(err):
    return jsonify({"error": err.message}), err.status


@app.errorhandler(ee.EEException)
def _handle_ee_error(err):
    # Earth Engine's own exceptions are descriptive and safe to expose (bad
    # geometry, no imagery for the date range, etc.) - almost always caller
    # input problems, not internal failures.
    logger.warning("Earth Engine error: %s", err)
    return jsonify({"error": f"Earth Engine error: {err}"}), 400


@app.errorhandler(Exception)
def _handle_unexpected_error(err):
    # Flask dispatches to the most specific registered handler automatically
    # (ApiError/EEException above take precedence). HTTPException (404, 405,
    # 413, ...) is also an Exception subclass, so it must be passed through
    # here rather than masked as a generic 500.
    if isinstance(err, HTTPException):
        return jsonify({"error": err.description or err.name}), err.code
    logger.exception("Unhandled error")
    return jsonify({"error": "Internal server error"}), 500


# --- Routes --------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/indices", methods=["GET"])
def list_indices():
    return jsonify({
        "indices": sorted(list(index_functions.keys()) + list(collection_functions.keys()) + ["TRUE_COLOR"]),
    })


@app.route("/api/terrain/layers", methods=["GET"])
def list_terrain_layers():
    return jsonify({"layers": ["dem", "slope", "aspect", "flow_direction", "flow_accumulation", "twi"]})


@app.route("/api/index", methods=["POST"])
def compute_index():
    payload = _require_json()
    index_name = payload.get("index")
    aoi_geojson = payload.get("aoi")
    start_date = payload.get("start_date")
    end_date = payload.get("end_date")
    cloud = payload.get("cloud", 20)
    orbit_pass = payload.get("orbit_pass")

    all_indices = set(index_functions) | set(collection_functions) | {"TRUE_COLOR"}
    if index_name not in all_indices:
        raise ApiError(f"Unknown index '{index_name}'. Available: {sorted(all_indices)}")
    if not aoi_geojson or not start_date or not end_date:
        raise ApiError("aoi, start_date and end_date are required")

    _validate_geometry(aoi_geojson, "aoi")
    _validate_date(start_date, "start_date")
    _validate_date(end_date, "end_date")
    cloud = _validate_cloud(cloud)
    orbit_pass = _validate_orbit_pass(orbit_pass)

    aoi = ee.Geometry(aoi_geojson)

    if index_name == "TRUE_COLOR":
        image = _optical_image(aoi, start_date, end_date, cloud)
        result_image = get_true_color(image)
        map_id = result_image.getMapId(TRUE_COLOR_VIS)
        return jsonify({
            "index": "TRUE_COLOR",
            "tile_url": map_id["tile_fetcher"].url_format,
            "vis_params": TRUE_COLOR_VIS,
        })

    if index_name in collection_functions:
        collection = _s1_collection(aoi, start_date, end_date, orbit_pass)
        result_image = collection_functions[index_name](collection).clip(aoi)
    elif index_name in RADAR_INDICES:
        image = _radar_image(aoi, start_date, end_date, orbit_pass)
        result_image = index_functions[index_name](image)
    else:
        image = _optical_image(aoi, start_date, end_date, cloud)
        result_image = index_functions[index_name](image)

    vis_params, labels, classified = _vis_for(index_name)
    tile_source = classify_standard(result_image) if classified else result_image
    map_id = tile_source.getMapId(vis_params)

    response = {
        "index": index_name,
        "tile_url": map_id["tile_fetcher"].url_format,
        "vis_params": vis_params,
    }
    if labels:
        response["labels"] = labels
    return jsonify(response)


@app.route("/api/farms/<farm_id>/dem/generate", methods=["POST"])
def generate_dem(farm_id):
    if not farm_id or not farm_id.strip() or len(farm_id) > 128:
        raise ApiError("farm_id must be a non-empty string (max 128 chars)")

    payload = _require_json()
    boundary = payload.get("boundary")
    if not boundary:
        raise ApiError("boundary (GeoJSON Polygon/MultiPolygon) is required")
    _validate_geometry(boundary, "boundary")
    if boundary["type"] not in {"Polygon", "MultiPolygon"}:
        raise ApiError("boundary must be a Polygon or MultiPolygon")

    aoi = ee.Geometry(boundary)
    boundary_hash = compute_boundary_hash(boundary)

    dem = get_dem(aoi)
    slope = get_slope(dem)
    aspect = get_aspect(dem)
    flow_dir = get_flow_direction(aoi)
    flow_acc = get_flow_accumulation(aoi)
    twi = get_twi(aoi, slope)

    statistics = compute_statistics(aoi, dem, slope, aspect)
    if statistics.get("min_elevation") is None:
        raise ApiError(
            "No DEM data available for this boundary (outside coverage, over water, "
            "or too small relative to the 30m grid)",
            status=422,
        )

    dem_vis = {"min": 0, "max": 9, "palette": DEM_PALETTE}
    slope_vis = {"min": 0, "max": 3, "palette": SLOPE_PALETTE}
    aspect_vis = {"min": 0, "max": 8, "palette": ASPECT_PALETTE}
    flow_dir_vis = {"min": 0, "max": 8, "palette": ASPECT_PALETTE}  # same compass classes

    dem_tile = classify_dem(dem).getMapId(dem_vis)
    slope_tile = classify_slope(slope).getMapId(slope_vis)
    aspect_tile = classify_aspect(aspect).getMapId(aspect_vis)
    flow_dir_tile = classify_flow_direction(flow_dir).getMapId(flow_dir_vis)
    # log1p-scaled for display - raw upstream area (km^2) spans orders of magnitude.
    flow_acc_tile = flow_acc.add(1).log().getMapId(FLOW_ACC_VIS)
    twi_tile = twi.getMapId(TWI_VIS)

    return jsonify({
        "farm_id": farm_id,
        "source": DEM_SOURCE_NAME,
        "resolution": DEM_RESOLUTION,
        "boundary_hash": boundary_hash,
        "layers": {
            "dem": dem_tile["tile_fetcher"].url_format,
            "slope": slope_tile["tile_fetcher"].url_format,
            "aspect": aspect_tile["tile_fetcher"].url_format,
            "flow_direction": flow_dir_tile["tile_fetcher"].url_format,
            "flow_accumulation": flow_acc_tile["tile_fetcher"].url_format,
            "twi": twi_tile["tile_fetcher"].url_format,
        },
        "legends": {
            "dem": {"vis_params": dem_vis, "labels": DEM_LABELS},
            "slope": {"vis_params": slope_vis, "labels": SLOPE_LABELS},
            "aspect": {"vis_params": aspect_vis, "labels": ASPECT_LABELS},
            "flow_direction": {"vis_params": flow_dir_vis, "labels": ASPECT_LABELS},
            "flow_accumulation": {"vis_params": FLOW_ACC_VIS, "labels": None},
            "twi": {"vis_params": TWI_VIS, "labels": TWI_LABELS},
        },
        "statistics": statistics,
    })


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "5000")))
