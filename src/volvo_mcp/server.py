"""MCP server exposing Volvo truck range, charging and refuelling tools.

Run directly over stdio::

    uv run volvo-mcp-server

or register with any MCP host, e.g. Claude Code::

    claude mcp add volvo -- uv run volvo-mcp-server
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp.server import MCPServer

from . import providers, specs
from .geo import GeocodingError, resolve_location, road_distance_km
from .range_model import DEFAULT_RESERVE, RangeModelError, energy_needed
from .range_model import estimate_range as _estimate_range

# Read .env here rather than relying on the host to pass keys through. An MCP
# host spawns this server as a subprocess with a *whitelisted* environment -
# PATH, TEMP, USERPROFILE and little else - so OCM_API_KEY set in the host's
# environment never arrives. Loading it at the server end makes the key work in
# every host: chat.py, Claude Code, the Inspector.
# Existing environment variables win; load_dotenv does not override.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")
load_dotenv()  # plus the usual search upward from the working directory

mcp = MCPServer(
    name="volvo-trucks",
    instructions=(
        "Operational assistant for Volvo truck fleets, covering both battery-electric "
        "and diesel models. Use these tools to look up vehicle specifications, estimate "
        "remaining range from a state of charge or fuel level, find truck-capable "
        "charging or fuel stations, and check whether a given trip is feasible.\n\n"
        "Always report the assumptions returned alongside a range figure (payload, "
        "terrain, temperature, reserve) - a bare number implies a precision this model "
        "does not have. Never invent a station that a tool did not return, and always "
        "state when results came from the offline seed dataset rather than a live API."
    ),
)


def _model_payload(m: dict[str, Any]) -> dict[str, Any]:
    """Spec summary shared by several tools."""
    common = {
        "id": m["id"],
        "name": m["name"],
        "powertrain": m["powertrain"],
        "segment": m["segment"],
        "reference_gcw_tonnes": m["reference_gcw_tonnes"],
        "max_gcw_tonnes": m["max_gcw_tonnes"],
        "base_consumption": m["base_consumption"],
        "consumption_unit": m["consumption_unit"],
        "source": m["source"],
    }
    if m["powertrain"] == "electric":
        common |= {
            "gross_battery_kwh": m["gross_battery_kwh"],
            "usable_battery_kwh": m["usable_battery_kwh"],
            "official_range_km": m["official_range_km"],
            "max_charge_kw": m["max_charge_kw"],
            "charge_connector": m["charge_connector"],
        }
    else:
        common |= {
            "tank_capacity_l": m["tank_capacity_l"],
            "max_tank_capacity_l": m["max_tank_capacity_l"],
            "adblue_tank_l": m["adblue_tank_l"],
        }
    return common


@mcp.tool()
def list_truck_models(powertrain: str | None = None) -> dict[str, Any]:
    """List the Volvo truck models this assistant knows about.

    Args:
        powertrain: Optionally filter to "electric" or "diesel". Omit for all models.
    """
    try:
        models = specs.list_models(powertrain)
    except ValueError as exc:
        return {"error": str(exc)}
    return {
        "count": len(models),
        "models": models,
        "reference_conditions": specs.reference_conditions(),
    }


@mcp.tool()
def get_truck_specs(model: str) -> dict[str, Any]:
    """Get full specifications for one Volvo truck model.

    Args:
        model: Model id or name, e.g. "fh-electric", "FH Electric", "FH diesel".
    """
    try:
        return _model_payload(specs.get_model(model))
    except specs.UnknownModelError as exc:
        return {"error": str(exc)}


@mcp.tool()
def estimate_range(
    model: str,
    level_pct: float,
    payload_tonnes: float | None = None,
    terrain: str = "flat",
    ambient_c: float = 15.0,
    reserve_pct: float = DEFAULT_RESERVE * 100,
) -> dict[str, Any]:
    """Estimate how far a truck can travel on its current charge or fuel.

    Args:
        model: Model id or name, e.g. "FH Electric".
        level_pct: Current state of charge (electric) or fuel level (diesel), 0-100.
        payload_tonnes: Gross combination weight in tonnes. Defaults to the model's
            reference GCW.
        terrain: One of "flat", "rolling", "hilly", "mountainous".
        ambient_c: Ambient temperature in Celsius. Cold weather cuts electric range
            significantly more than diesel.
        reserve_pct: Percentage of range held back as a safety reserve. Default 10.
    """
    try:
        m = specs.get_model(model)
    except specs.UnknownModelError as exc:
        return {"error": str(exc)}

    try:
        estimate = _estimate_range(
            powertrain=m["powertrain"],
            capacity=specs.capacity_of(m),
            base_consumption=m["base_consumption"],
            level_pct=level_pct,
            reference_gcw_tonnes=m["reference_gcw_tonnes"],
            gcw_tonnes=payload_tonnes,
            terrain=terrain,
            ambient_c=ambient_c,
            reserve=reserve_pct / 100.0,
        )
    except RangeModelError as exc:
        return {"error": str(exc)}

    return {
        "model": m["name"],
        "powertrain": m["powertrain"],
        **estimate.to_dict(),
        "caveat": (
            "Estimate from public specification figures and a simplified energy model. "
            "Real-world range varies with driver, traffic, wind and auxiliary loads."
        ),
    }


@mcp.tool()
async def find_charging_stations(
    location: str,
    radius_km: float = 50,
    min_power_kw: float = 150,
    limit: int = 10,
) -> dict[str, Any]:
    """Find truck-capable charging stations near a location.

    Args:
        location: Place name ("Gothenburg") or "lat,lon" coordinates.
        radius_km: Search radius in kilometres.
        min_power_kw: Minimum charger power. Default 150 kW filters out AC posts a
            truck could not realistically use.
        limit: Maximum number of stations to return.
    """
    try:
        place = await resolve_location(location)
    except GeocodingError as exc:
        return {"error": str(exc)}

    result = await providers.find_charging(
        place.lat, place.lon, radius_km=radius_km, min_power_kw=min_power_kw, limit=limit
    )
    return {"query_location": place.to_dict(), "radius_km": radius_km, **result}


@mcp.tool()
async def find_fuel_stations(
    location: str,
    radius_km: float = 50,
    hgv_only: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    """Find diesel fuel stations near a location.

    Args:
        location: Place name ("Hamburg") or "lat,lon" coordinates.
        radius_km: Search radius in kilometres.
        hgv_only: Restrict to stations explicitly tagged as HGV-accessible. OSM
            coverage of this tag is patchy, so leaving it false is usually better.
        limit: Maximum number of stations to return.
    """
    try:
        place = await resolve_location(location)
    except GeocodingError as exc:
        return {"error": str(exc)}

    result = await providers.find_fuel(
        place.lat, place.lon, radius_km=radius_km, hgv_only=hgv_only, limit=limit
    )
    return {"query_location": place.to_dict(), "radius_km": radius_km, **result}


@mcp.tool()
async def plan_trip(
    model: str,
    level_pct: float,
    origin: str,
    destination: str,
    payload_tonnes: float | None = None,
    terrain: str = "flat",
    ambient_c: float = 15.0,
) -> dict[str, Any]:
    """Check whether a truck can complete a trip, and suggest a stop if it cannot.

    Geocodes both ends, estimates road distance, compares it against available
    range, and - when the trip is not feasible in one leg - looks for a charging or
    fuel stop near the origin.

    Args:
        model: Model id or name, e.g. "FH Electric".
        level_pct: Current state of charge or fuel level, 0-100.
        origin: Starting place name or "lat,lon".
        destination: Destination place name or "lat,lon".
        payload_tonnes: Gross combination weight in tonnes.
        terrain: One of "flat", "rolling", "hilly", "mountainous".
        ambient_c: Ambient temperature in Celsius.
    """
    try:
        m = specs.get_model(model)
    except specs.UnknownModelError as exc:
        return {"error": str(exc)}

    try:
        start = await resolve_location(origin)
        end = await resolve_location(destination)
    except GeocodingError as exc:
        return {"error": str(exc)}

    distance = road_distance_km(start.lat, start.lon, end.lat, end.lon)

    try:
        estimate = _estimate_range(
            powertrain=m["powertrain"],
            capacity=specs.capacity_of(m),
            base_consumption=m["base_consumption"],
            level_pct=level_pct,
            reference_gcw_tonnes=m["reference_gcw_tonnes"],
            gcw_tonnes=payload_tonnes,
            terrain=terrain,
            ambient_c=ambient_c,
        )
        needed = energy_needed(
            distance_km=distance,
            powertrain=m["powertrain"],
            base_consumption=m["base_consumption"],
            reference_gcw_tonnes=m["reference_gcw_tonnes"],
            gcw_tonnes=payload_tonnes,
            terrain=terrain,
            ambient_c=ambient_c,
        )
    except RangeModelError as exc:
        return {"error": str(exc)}

    feasible = estimate.range_km >= distance
    result: dict[str, Any] = {
        "model": m["name"],
        "powertrain": m["powertrain"],
        "origin": start.to_dict(),
        "destination": end.to_dict(),
        "estimated_road_distance_km": round(distance, 1),
        "distance_basis": "great-circle distance inflated by a 1.25 road winding factor, not a routed distance",
        "available_range_km": round(estimate.range_km, 1),
        "feasible_without_stopping": feasible,
        "margin_km": round(estimate.range_km - distance, 1),
        "energy_required": round(needed, 1),
        "energy_required_unit": estimate.energy_unit,
        "energy_available": round(estimate.available_energy, 1),
        "assumptions": estimate.assumptions,
    }

    if feasible:
        result["verdict"] = (
            f"Reachable with about {result['margin_km']} km to spare "
            f"(after a {int(estimate.assumptions['reserve_fraction'] * 100)}% reserve)."
        )
        return result

    shortfall = distance - estimate.range_km
    result["shortfall_km"] = round(shortfall, 1)
    result["verdict"] = (
        f"Not reachable in one leg - about {result['shortfall_km']} km short. "
        "One charging or refuelling stop is needed."
    )

    # Look for a stop near the origin so the driver can top up before setting off.
    if m["powertrain"] == "electric":
        stops = await providers.find_charging(
            start.lat, start.lon, radius_km=max(estimate.range_km, 50), min_power_kw=150, limit=3
        )
    else:
        stops = await providers.find_fuel(
            start.lat, start.lon, radius_km=max(estimate.range_km, 50), limit=3
        )
    result["suggested_stops"] = stops
    return result


def main() -> None:
    """Entry point - serve over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
