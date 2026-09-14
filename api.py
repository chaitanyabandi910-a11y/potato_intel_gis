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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
