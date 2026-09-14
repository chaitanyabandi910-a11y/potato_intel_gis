import ee

from config import ee_auth  # noqa: F401  (initializes ee on import)

# Optical band aliases -> Sentinel-2 SR band names (satellite_map id 2 in py.pages)
S2_BANDS = {
    "A": "B1", "B": "B2", "G": "B3", "R": "B4",
    "RE1": "B5", "RE2": "B6", "RE3": "B7",
    "N": "B8", "N2": "B8A", "S1": "B11", "S2": "B12",
}

# Radar band aliases -> Sentinel-1 GRD band names (satellite_map id 1 in py.pages)
S1_BANDS = {"VV": "VV", "VH": "VH"}


# Normalized Difference Vegetation Index
def calculate_NDVI(image, bands=S2_BANDS):
    equation = "((N - R) / (N + R))"
    return image.expression(equation, {
        "N": image.select(bands.get("N")),
        "R": image.select(bands.get("R")),
    }).rename("NDVI")


# Radar Vegetation Index (Sentinel-1 SAR analog to NDVI)
# S1 GRD ships in dB, so VV/VH must be converted to linear power before the
# ratio is computed, then clamped to the valid 0-1 range.
def calculate_RVI(image, bands=S1_BANDS):
    vv_linear = ee.Image(10).pow(image.select(bands.get("VV")).divide(10))
    vh_linear = ee.Image(10).pow(image.select(bands.get("VH")).divide(10))
    equation = "(4 * VH) / (VV + VH)"
    rvi = image.expression(equation, {"VV": vv_linear, "VH": vh_linear}).rename("NDVI_SAR")
    return rvi.max(0).min(1)


# Enhanced Vegetation Index
def calculate_EVI(image, bands=S2_BANDS):
    equation = "2.5 * ((N - R) / (N + 6 * R - 7.5 * B + 1))"
    return image.expression(equation, {
        "N": image.select(bands.get("N")),
        "R": image.select(bands.get("R")),
        "B": image.select(bands.get("B")),
    }).rename("EVI")


# Soil-Adjusted Vegetation Index
def calculate_SAVI(image, bands=S2_BANDS):
    equation = "(1 + L) * (N - R) / (N + R + L)"
    return image.expression(equation, {
        "L": 0.5,
        "N": image.select(bands.get("N")),
        "R": image.select(bands.get("R")),
    }).rename("SAVI")


# Modified Soil-Adjusted Vegetation Index
def calculate_MSAVI(image, bands=S2_BANDS):
    equation = "(2 * N + 1 - sqrt((2 * N + 1) ** 2 - 8 * (N - R))) / 2"
    return image.expression(equation, {
        "N": image.select(bands.get("N")),
        "R": image.select(bands.get("R")),
    }).rename("MSAVI")


# Normalized Difference Red Edge Index
def calculate_NDRE(image, bands=S2_BANDS):
    equation = "((N - RE1) / (N + RE1))"
    return image.expression(equation, {
        "N": image.select(bands.get("N")),
        "RE1": image.select(bands.get("RE1")),
    }).rename("NDRE")


# Green Normalized Difference Vegetation Index
def calculate_GNDVI(image, bands=S2_BANDS):
    equation = "((N - G) / (N + G))"
    return image.expression(equation, {
        "N": image.select(bands.get("N")),
        "G": image.select(bands.get("G")),
    }).rename("GNDVI")


# MERIS Terrestrial Chlorophyll Index (Sentinel-2 red-edge equivalent)
def calculate_MTCI(image, bands=S2_BANDS):
    equation = "(RE2 - RE1) / (RE1 - R)"
    return image.expression(equation, {
        "RE2": image.select(bands.get("RE2")),
        "RE1": image.select(bands.get("RE1")),
        "R": image.select(bands.get("R")),
    }).rename("MTCI")


# Normalized Difference Moisture Index
def calculate_NDMI(image, bands=S2_BANDS):
    equation = "(N - S1) / (N + S1)"
    return image.expression(equation, {
        "N": image.select(bands.get("N")),
        "S1": image.select(bands.get("S1")),
    }).rename("NDMI")


# Normalized Difference Water Index
def calculate_NDWI(image, bands=S2_BANDS):
    equation = "(G - N) / (G + N)"
    return image.expression(equation, {
        "G": image.select(bands.get("G")),
        "N": image.select(bands.get("N")),
    }).rename("NDWI")


# Land Surface Water Index
def calculate_LSWI(image, bands=S2_BANDS):
    equation = "(N - S2) / (N + S2)"
    return image.expression(equation, {
        "N": image.select(bands.get("N")),
        "S2": image.select(bands.get("S2")),
    }).rename("LSWI")


# Soil Moisture proxy, Wagner change-detection method: temporal min-max
# normalization of VV backscatter, 0-1 relative scale. Needs the full S1
# ImageCollection over the date range (not a single composite) to build the
# per-pixel min/max, so this takes a collection, not an image like the others.
def calculate_SM_RELATIVE(collection, bands=S1_BANDS):
    vv = collection.select(bands.get("VV"))
    vv_stats = vv.reduce(ee.Reducer.minMax())
    vv_min = vv_stats.select(bands.get("VV") + "_min")
    vv_max = vv_stats.select(bands.get("VV") + "_max")
    vv_median = vv.median()
    sm = vv_median.subtract(vv_min).divide(vv_max.subtract(vv_min)).clamp(0, 1)
    return sm.rename("SM_RELATIVE")


# True Color composite (visualization only, not a derived index)
def get_true_color(image, bands=S2_BANDS):
    return image.select(
        [bands.get("R"), bands.get("G"), bands.get("B")]
    ).rename(["R", "G", "B"])


TRUE_COLOR_VIS = {"min": 0, "max": 3000, "gamma": 1.4}


# Indices computed from a single composite image, signature (image, bands).
index_functions = {
    "NDVI": calculate_NDVI,
    "NDVI_SAR": calculate_RVI,
    "EVI": calculate_EVI,
    "SAVI": calculate_SAVI,
    "MSAVI": calculate_MSAVI,
    "NDRE": calculate_NDRE,
    "GNDVI": calculate_GNDVI,
    "MTCI": calculate_MTCI,
    "NDMI": calculate_NDMI,
    "NDWI": calculate_NDWI,
    "LSWI": calculate_LSWI,
}

# Indices that need the raw ImageCollection (e.g. for a temporal min/max),
# not a single composite image - signature (collection, bands).
collection_functions = {
    "SM_RELATIVE": calculate_SM_RELATIVE,
}

# Radar indices need Sentinel-1 (VV/VH); everything else needs the optical bands.
RADAR_INDICES = {"NDVI_SAR", "SM_RELATIVE"}


# Buckets a roughly 0-1 index into 10 classes (0-9) on fixed 0.1-wide steps,
# matching the shared vegetation/moisture classification scheme.
def classify_standard(image):
    idx = image.rename("idx")
    equation = (
        "idx <= 0 ? 0 : "
        "idx < 0.1 ? 1 : "
        "idx < 0.2 ? 2 : "
        "idx < 0.3 ? 3 : "
        "idx < 0.4 ? 4 : "
        "idx < 0.5 ? 5 : "
        "idx < 0.6 ? 6 : "
        "idx < 0.7 ? 7 : "
        "idx < 0.8 ? 8 : 9"
    )
    return idx.expression(equation, {"idx": idx}).rename("class")


VEGETATION_INDICES = {"NDVI", "GNDVI", "NDRE", "EVI", "SAVI", "MSAVI", "MTCI", "NDVI_SAR"}
MOISTURE_INDICES = {"NDWI", "NDMI", "LSWI", "SM_RELATIVE"}

VEGETATION_PALETTE = [
    "#8B0000", "#D73027", "#F46D43", "#FDAE61", "#FEE08B",
    "#D9EF8B", "#A6D96A", "#66BD63", "#1A9850", "#006837",
]
VEGETATION_LABELS = [
    "≤ 0.0 | Bare Soil / Water",
    "0.0 - 0.1 | Very Sparse Vegetation",
    "0.1 - 0.2 | Sparse Vegetation",
    "0.2 - 0.3 | Low Vegetation",
    "0.3 - 0.4 | Moderate Vegetation",
    "0.4 - 0.5 | Moderately Healthy",
    "0.5 - 0.6 | Healthy Vegetation",
    "0.6 - 0.7 | Very Healthy",
    "0.7 - 0.8 | Dense Canopy",
    "0.8 - 1.0 | Very Dense Canopy",
]

MOISTURE_PALETTE = [
    "#8c510a", "#bf812d", "#dfc27d", "#f6e8c3", "#c7eae5",
    "#80cdc1", "#35978f", "#01665e", "#2166ac", "#053061",
]
MOISTURE_LABELS = [
    "≤ 0.0 | Extremely Dry",
    "0.0 - 0.1 | Very Dry",
    "0.1 - 0.2 | Dry",
    "0.2 - 0.3 | Slightly Dry",
    "0.3 - 0.4 | Moderate Moisture",
    "0.4 - 0.5 | Moist",
    "0.5 - 0.6 | Wet",
    "0.6 - 0.7 | Very Wet",
    "0.7 - 0.8 | Water Logged",
    "0.8 - 1.0 | Open Water",
]
