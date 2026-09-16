"""Geocoding and distance helpers.

Nominatim is used for place-name lookup (free, no key, but it requires a
descriptive User-Agent and tolerates only light use). Distances are great-circle
with a road-winding allowance - good enough for feasibility checks, and honest
about being an estimate rather than a routed distance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import httpx

from ._http import async_client

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "volvo-mcp/0.1 (truck fleet assistant; MCP demo project)"

EARTH_RADIUS_KM = 6371.0

#: Real roads are longer than a straight line. 1.25 is a common planning factor
#: for European road networks; it keeps feasibility checks conservative.
ROAD_WINDING_FACTOR = 1.25


class GeocodingError(RuntimeError):
    """Raised when a place name cannot be resolved to coordinates."""


@dataclass(frozen=True)
class Place:
    name: str
    lat: float
    lon: float

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "lat": round(self.lat, 5), "lon": round(self.lon, 5)}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def road_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Estimated road distance: great-circle inflated by a winding factor."""
    return haversine_km(lat1, lon1, lat2, lon2) * ROAD_WINDING_FACTOR


async def geocode(place: str, *, country_codes: str | None = None) -> Place:
    """Resolve a place name to coordinates via Nominatim.

    Args:
        place: A free-text place name, e.g. "Gothenburg" or "Rotterdam, NL".
        country_codes: Optional comma-separated ISO codes to narrow the search.

    Raises:
        GeocodingError: if the place cannot be resolved or the service is
            unreachable. Callers decide whether that is fatal.
    """
    params: dict[str, str | int] = {"q": place, "format": "json", "limit": 1}
    if country_codes:
        params["countrycodes"] = country_codes

    try:
        async with async_client(headers={"User-Agent": USER_AGENT}) as client:
            response = await client.get(NOMINATIM_URL, params=params)
            response.raise_for_status()
            results = response.json()
    except httpx.HTTPError as exc:
        raise GeocodingError(f"geocoding service unreachable for {place!r}: {exc}") from exc

    if not results:
        raise GeocodingError(f"could not find a location matching {place!r}")

    top = results[0]
    return Place(name=top.get("display_name", place), lat=float(top["lat"]), lon=float(top["lon"]))


async def resolve_location(location: str) -> Place:
    """Accept either ``"lat,lon"`` or a place name and return a :class:`Place`."""
    if "," in location:
        left, _, right = location.partition(",")
        try:
            lat, lon = float(left.strip()), float(right.strip())
        except ValueError:
            pass  # Not a coordinate pair - fall through to geocoding.
        else:
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return Place(name=f"{lat:.4f}, {lon:.4f}", lat=lat, lon=lon)
    return await geocode(location)
