"""ECB euro foreign exchange reference rates (SDMX REST API, CSV)."""

import csv
import io
from datetime import date
from decimal import Decimal

from app.platform.outbound import OutboundClient

BASE = "https://data-api.ecb.europa.eu/service/data/EXR"


def fetch(currencies: list[str], start: date, client: OutboundClient) -> list[tuple[str, date, Decimal]]:
    ccys = sorted({c for c in currencies if c != "EUR"})
    if not ccys:
        return []
    url = f"{BASE}/D.{'+'.join(ccys)}.EUR.SP00.A?startPeriod={start.isoformat()}&format=csvdata"
    r = client.get(url)
    if r.status_code == 404:  # no data for the period (e.g. weekend only)
        return []
    r.raise_for_status()
    out = []
    for row in csv.DictReader(io.StringIO(r.text)):
        v = row.get("OBS_VALUE")
        if v:
            out.append((row["CURRENCY"], date.fromisoformat(row["TIME_PERIOD"]), Decimal(v)))
    return out
