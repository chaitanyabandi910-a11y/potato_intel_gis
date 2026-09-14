import ee
from flask import Flask, jsonify, request
from flask_cors import CORS

from indices import (
    MOISTURE_INDICES,
    MOISTURE_LABELS,
    MOISTURE_PALETTE,
    RADAR_INDICES,
    VEGETATION_INDICES,
    VEGETATION_LABELS,
    VEGETATION_PALETTE,
    classify_standard,
    collection_functions,
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

app = Flask(__name__)
CORS(app)  # allow calls from a separate frontend/Node backend origin during local dev

S2_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"
S2_CLOUD_PROB_COLLECTION = "COPERNICUS/S2_CLOUD_PROBABILITY"
S1_COLLECTION = "COPERNICUS/S1_GRD"

DEFAULT_CLOUD_PROB_THRESHOLD = 40

DEFAULT_VIS = {"min": -1, "max": 1, "palette": ["#d7191c", "#ffffc0", "#1a9641"]}


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


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/indices", methods=["GET"])
def list_indices():
    return jsonify({"indices": sorted(list(index_functions.keys()) + list(collection_functions.keys()))})


@app.route("/api/index", methods=["POST"])
def compute_index():
    payload = request.get_json(force=True) or {}
    index_name = payload.get("index")
    aoi_geojson = payload.get("aoi")
    start_date = payload.get("start_date")
    end_date = payload.get("end_date")
    cloud = payload.get("cloud", 20)
    orbit_pass = payload.get("orbit_pass")

    all_indices = set(index_functions) | set(collection_functions)
    if index_name not in all_indices:
        return jsonify({"error": f"Unknown index '{index_name}'", "available": sorted(all_indices)}), 400
    if not aoi_geojson or not start_date or not end_date:
        return jsonify({"error": "aoi, start_date and end_date are required"}), 400

    aoi = ee.Geometry(aoi_geojson)

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
    payload = request.get_json(force=True) or {}
    boundary = payload.get("boundary")
    if not boundary:
        return jsonify({"error": "boundary (GeoJSON Polygon/MultiPolygon) is required"}), 400

    aoi = ee.Geometry(boundary)
    boundary_hash = compute_boundary_hash(boundary)

    dem = get_dem(aoi)
    slope = get_slope(dem)
    aspect = get_aspect(dem)
    flow_dir = get_flow_direction(aoi)
    flow_acc = get_flow_accumulation(aoi)
    twi = get_twi(aoi, slope)

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

    statistics = compute_statistics(aoi, dem, slope, aspect)

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
    app.run(debug=True, port=5000)
