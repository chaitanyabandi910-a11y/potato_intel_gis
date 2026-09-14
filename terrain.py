import hashlib
import json
import math

import ee

from config import ee_auth  # noqa: F401  (initializes ee on import)
from indices import MOISTURE_PALETTE

DEM_SOURCE = "COPERNICUS/DEM/GLO30_2024_1"
DEM_SOURCE_NAME = "Copernicus DEM GLO-30"
DEM_RESOLUTION = "30m"
MERIT_HYDRO = "MERIT/Hydro/v1_0_1"


def _dem_mosaic():
    # GLO-30 is tiled; mosaic() alone leaves the image with a degenerate 1
    # degree/pixel default projection (no single native grid), which silently
    # breaks ee.Terrain.slope/aspect (wrong pixel size -> near-zero slope, and
    # masked-out results once clipped to an AOI smaller than that "pixel").
    # setDefaultProjection pins it to the real ~30m grid before any derivative
    # is computed from it.
    return (
        ee.ImageCollection(DEM_SOURCE)
        .select("DEM")
        .mosaic()
        .setDefaultProjection(crs="EPSG:4326", scale=30)
    )


def get_dem(aoi):
    return _dem_mosaic().clip(aoi).rename("DEM")


def get_slope(dem):
    # Slope/aspect must be derived before the input is clipped - compute them
    # from the unclipped mosaic, then clip the result for display/stats.
    return ee.Terrain.slope(_dem_mosaic()).clip(dem.geometry()).rename("SLOPE")  # degrees


def get_aspect(dem):
    return ee.Terrain.aspect(_dem_mosaic()).clip(dem.geometry()).rename("ASPECT")  # degrees, 0-360, clockwise from north


def get_flow_direction(aoi):
    # MERIT Hydro's D8 flow direction, precomputed globally - clipping only
    # windows the display, it doesn't change the upstream computation.
    return ee.Image(MERIT_HYDRO).select("dir").clip(aoi).rename("FLOW_DIR")


def get_flow_accumulation(aoi):
    # Upstream drainage area (km^2), MERIT Hydro's flow-accumulation output.
    return ee.Image(MERIT_HYDRO).select("upa").clip(aoi).rename("FLOW_ACC")


def get_twi(aoi, slope):
    upa = ee.Image(MERIT_HYDRO).select("upa").clip(aoi)
    slope_rad = slope.multiply(math.pi / 180)
    tan_slope = slope_rad.tan().max(0.001)  # avoid divide-by-zero on flat terrain
    return upa.multiply(1e6).divide(tan_slope).log().rename("TWI")


# --- Classification / legends -----------------------------------------------

DEM_PALETTE = [
    "#006400", "#2E8B57", "#7CFC00", "#ADFF2F", "#FFFF66",
    "#FFD700", "#FFA500", "#CD853F", "#A0522D", "#FFFFFF",
]
DEM_LABELS = [
    "0-100 m | Very Low Elevation",
    "100-200 m | Low Elevation",
    "200-400 m | Moderate Elevation",
    "400-600 m | Elevated Terrain",
    "600-800 m | Upland",
    "800-1200 m | Highland",
    "1200-1600 m | Hills",
    "1600-2200 m | Mountains",
    "2200-3000 m | High Mountains",
    ">3000 m | Very High Mountains",
]


def classify_dem(dem):
    e = dem.rename("e")
    equation = (
        "e < 100 ? 0 : e < 200 ? 1 : e < 400 ? 2 : e < 600 ? 3 : e < 800 ? 4 : "
        "e < 1200 ? 5 : e < 1600 ? 6 : e < 2200 ? 7 : e < 3000 ? 8 : 9"
    )
    # See classify_standard() in indices.py - .expression()'s ternary does not
    # reliably propagate the mask through tile rendering, so it's reapplied here.
    return e.expression(equation, {"e": e}).rename("class").updateMask(e.mask())


SLOPE_PALETTE = ["#1a9850", "#a6d96a", "#fdae61", "#d73027"]
SLOPE_LABELS = [
    "0-2° | Very Flat",
    "2-5° | Gentle Slope",
    "5-10° | Moderate Slope",
    ">10° | Steep",
]


def classify_slope(slope):
    s = slope.rename("s")
    equation = "s < 2 ? 0 : s < 5 ? 1 : s < 10 ? 2 : 3"
    return s.expression(equation, {"s": s}).rename("class").updateMask(s.mask())


# Compass classes shared by aspect AND flow direction (both are "which way is
# this pixel facing / draining"), so they use the same 8-direction + flat scheme.
ASPECT_LABELS = [
    "North", "North-East", "East", "South-East",
    "South", "South-West", "West", "North-West", "Flat",
]
ASPECT_PALETTE = [
    "#2b83ba", "#66c2a5", "#1a9850", "#a6d96a",
    "#fee08b", "#fdae61", "#d73027", "#984ea3", "#bdbdbd",
]


def classify_aspect(aspect):
    a = aspect.rename("a")
    equation = (
        "a < 0 ? 8 : a < 22.5 ? 0 : a < 67.5 ? 1 : a < 112.5 ? 2 : a < 157.5 ? 3 : "
        "a < 202.5 ? 4 : a < 247.5 ? 5 : a < 292.5 ? 6 : a < 337.5 ? 7 : 0"
    )
    return a.expression(equation, {"a": a}).rename("class").updateMask(a.mask())


# MERIT Hydro D8 codes (ESRI convention): 1=E 2=SE 4=S 8=SW 16=W 32=NW 64=N 128=NE.
# 0/-1/-9 are river-mouth/ocean/inland-depression - all folded into "Flat".
def classify_flow_direction(flow_dir):
    return flow_dir.remap(
        [1, 2, 4, 8, 16, 32, 64, 128, 0, -1, -9],
        [2, 3, 4, 5, 6, 7, 0, 1, 8, 8, 8],
        8,
    ).rename("class")


FLOW_ACC_PALETTE = ["#f7fbff", "#c6dbef", "#6baed6", "#2171b5", "#08306b"]
FLOW_ACC_VIS = {"min": 0, "max": 15, "palette": FLOW_ACC_PALETTE}  # log1p-scaled

TWI_VIS = {"min": 0, "max": 20, "palette": MOISTURE_PALETTE}
TWI_LABELS = [
    "0-2 | Very Low Wetness Tendency",
    "2-4 | Low",
    "4-6 | Low-Moderate",
    "6-8 | Moderate",
    "8-10 | Moderate-High",
    "10-12 | High",
    "12-14 | Very High",
    "14-16 | Wet-Prone",
    "16-18 | Highly Wet-Prone",
    "18-20 | Extreme Wetness Tendency",
]


# --- Statistics --------------------------------------------------------------

def compute_statistics(aoi, dem, slope, aspect):
    elevation_stats = dem.reduceRegion(
        reducer=ee.Reducer.minMax().combine(ee.Reducer.mean(), sharedInputs=True),
        geometry=aoi, scale=30, maxPixels=1e13, tileScale=4,
    ).getInfo()

    slope_stats = slope.reduceRegion(
        reducer=ee.Reducer.mean().combine(ee.Reducer.max(), sharedInputs=True),
        geometry=aoi, scale=30, maxPixels=1e13, tileScale=4,
    ).getInfo()

    aspect_mode = classify_aspect(aspect).reduceRegion(
        reducer=ee.Reducer.mode(), geometry=aoi, scale=30, maxPixels=1e13, tileScale=4,
    ).getInfo()
    dominant_idx = aspect_mode.get("class")
    dominant_label = ASPECT_LABELS[round(dominant_idx)] if dominant_idx is not None else None

    return {
        "min_elevation": elevation_stats.get("DEM_min"),
        "max_elevation": elevation_stats.get("DEM_max"),
        "avg_elevation": elevation_stats.get("DEM_mean"),
        "mean_slope": slope_stats.get("SLOPE_mean"),
        "max_slope": slope_stats.get("SLOPE_max"),
        "dominant_aspect": dominant_label,
    }


# --- Boundary hash (for cache-key consistency with the caller's DB layer) ---

def compute_boundary_hash(boundary_geojson):
    canonical = json.dumps(boundary_geojson, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
