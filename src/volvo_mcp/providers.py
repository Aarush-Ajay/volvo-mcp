"""Station lookup providers, with a tagged offline fallback.

Two live sources:

* **Open Charge Map** for charging stations. Free, but requires an API key
  (openchargemap.org -> profile -> "my apps" -> Register An Application),
  supplied via the ``OCM_API_KEY`` environment variable.
* **OpenStreetMap Overpass** for fuel stations. Free, no key.

Every live call is wrapped: on a missing key, a timeout, or a bad response we
fall back to the bundled seed dataset. Results always carry a ``source`` field
(``"open_charge_map"`` / ``"overpass"`` / ``"offline_seed"``) so a stale answer is
never mistaken for a live one.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

from ._http import async_client
from .geo import haversine_km

DATA_DIR = Path(__file__).parent / "data"
SEED_FILE = DATA_DIR / "seed_stations.json"

OCM_URL = "https://api.openchargemap.io/v3/poi"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "volvo-mcp/0.1 (truck fleet assistant; MCP demo project)"


@lru_cache(maxsize=1)
def _seed() -> dict[str, Any]:
    return json.loads(SEED_FILE.read_text(encoding="utf-8"))


def _nearest(rows: list[dict[str, Any]], lat: float, lon: float, radius_km: float, limit: int):
    """Annotate rows with distance, filter to the radius, sort nearest-first."""
    out = []
    for row in rows:
        distance = haversine_km(lat, lon, row["lat"], row["lon"])
        if distance <= radius_km:
            out.append({**row, "distance_km": round(distance, 1)})
    out.sort(key=lambda r: r["distance_km"])
    return out[:limit]


def _seed_charging(lat, lon, radius_km, min_power_kw, limit, reason):
    rows = [c for c in _seed()["charging"] if c["power_kw"] >= min_power_kw]
    return {
        "source": "offline_seed",
        "fallback_reason": reason,
        "stations": _nearest(rows, lat, lon, radius_km, limit),
    }


def _seed_fuel(lat, lon, radius_km, limit, reason):
    return {
        "source": "offline_seed",
        "fallback_reason": reason,
        "stations": _nearest(list(_seed()["fuel"]), lat, lon, radius_km, limit),
    }


async def find_charging(
    lat: float,
    lon: float,
    *,
    radius_km: float = 50,
    min_power_kw: float = 150,
    limit: int = 10,
) -> dict[str, Any]:
    """Charging stations near a point, filtered to truck-capable DC power.

    ``min_power_kw`` defaults to 150 so results are DC fast chargers a truck could
    actually use, rather than the AC posts that dominate a raw query.
    """
    api_key = os.environ.get("OCM_API_KEY", "").strip()
    if not api_key:
        return _seed_charging(
            lat, lon, radius_km, min_power_kw, limit,
            "OCM_API_KEY is not set - register a free key at openchargemap.org",
        )

    params = {
        "output": "json",
        "latitude": lat,
        "longitude": lon,
        "distance": radius_km,
        "distanceunit": "KM",
        "maxresults": max(limit * 4, 40),
        "compact": "true",
        "verbose": "false",
    }
    headers = {"X-API-Key": api_key, "User-Agent": USER_AGENT}

    try:
        async with async_client(headers=headers) as client:
            response = await client.get(OCM_URL, params=params)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return _seed_charging(lat, lon, radius_km, min_power_kw, limit, f"live lookup failed: {exc}")

    stations = []
    for poi in payload:
        address = poi.get("AddressInfo") or {}
        poi_lat, poi_lon = address.get("Latitude"), address.get("Longitude")
        if poi_lat is None or poi_lon is None:
            continue

        connections = poi.get("Connections") or []
        powers = [c.get("PowerKW") for c in connections if c.get("PowerKW")]
        max_power = max(powers) if powers else 0
        if max_power < min_power_kw:
            continue

        stations.append({
            "id": f"ocm-{poi.get('ID')}",
            "name": address.get("Title") or "Unnamed site",
            "operator": (poi.get("OperatorInfo") or {}).get("Title"),
            "lat": poi_lat,
            "lon": poi_lon,
            "country": (address.get("Country") or {}).get("ISOCode"),
            "power_kw": max_power,
            "points": poi.get("NumberOfPoints") or len(connections),
            "address": address.get("AddressLine1"),
            "town": address.get("Town"),
            "distance_km": round(haversine_km(lat, lon, poi_lat, poi_lon), 1),
        })

    if not stations:
        return _seed_charging(
            lat, lon, radius_km, min_power_kw, limit,
            f"no live results at or above {min_power_kw} kW within {radius_km} km",
        )

    stations.sort(key=lambda s: s["distance_km"])
    return {"source": "open_charge_map", "stations": stations[:limit]}


_OVERPASS_QUERY = """
[out:json][timeout:25];
(
  nwr["amenity"="fuel"]{hgv_filter}(around:{radius_m},{lat},{lon});
);
out center tags;
"""


async def find_fuel(
    lat: float,
    lon: float,
    *,
    radius_km: float = 50,
    hgv_only: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    """Fuel stations near a point, via OpenStreetMap Overpass.

    ``nwr`` (not ``node``) is deliberate: fuel stations are frequently mapped as
    areas, and a node-only query silently misses them.

    ``hgv_only`` narrows to stations tagged ``hgv=yes``, but OSM coverage of that
    tag is patchy, so it is off by default - absence of the tag is not evidence a
    truck cannot use the site.
    """
    query = _OVERPASS_QUERY.format(
        hgv_filter='["hgv"="yes"]' if hgv_only else "",
        radius_m=int(radius_km * 1000),
        lat=lat,
        lon=lon,
    )

    try:
        async with async_client(headers={"User-Agent": USER_AGENT}) as client:
            response = await client.post(OVERPASS_URL, data={"data": query})
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return _seed_fuel(lat, lon, radius_km, limit, f"live lookup failed: {exc}")

    stations = []
    for element in payload.get("elements", []):
        # Nodes carry lat/lon directly; ways and relations carry a `center`.
        centre = element.get("center") or element
        el_lat, el_lon = centre.get("lat"), centre.get("lon")
        if el_lat is None or el_lon is None:
            continue

        tags = element.get("tags") or {}
        stations.append({
            "id": f"osm-{element.get('type')}-{element.get('id')}",
            "name": tags.get("name") or tags.get("brand") or "Unnamed fuel station",
            "operator": tags.get("operator") or tags.get("brand"),
            "lat": el_lat,
            "lon": el_lon,
            "hgv_access": tags.get("hgv") == "yes",
            "diesel": tags.get("fuel:diesel") == "yes" or tags.get("fuel:HGV_diesel") == "yes",
            "adblue": tags.get("fuel:adblue") == "yes",
            "opening_hours": tags.get("opening_hours"),
            "distance_km": round(haversine_km(lat, lon, el_lat, el_lon), 1),
        })

    if not stations:
        return _seed_fuel(lat, lon, radius_km, limit, f"no live results within {radius_km} km")

    stations.sort(key=lambda s: s["distance_km"])
    return {"source": "overpass", "stations": stations[:limit]}
