import json
import os

import ee

CREDENTIALS_PATH = os.getenv(
    "GEE_SERVICE_ACCOUNT_FILE",
    os.path.join(os.path.dirname(__file__), "..", "credentials", "service-account.json"),
)


def initialize():
    with open(CREDENTIALS_PATH) as f:
        key = json.load(f)
    credentials = ee.ServiceAccountCredentials(key["client_email"], CREDENTIALS_PATH)
    ee.Initialize(credentials)


initialize()
