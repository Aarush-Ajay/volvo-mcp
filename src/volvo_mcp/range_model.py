"""Energy-based range estimation for Volvo trucks.

Pure functions only - no network, no file I/O, no globals. Everything needed to
compute a range is passed in, which is what makes this module cheap to test and
safe to reason about.

The model is deliberately the same shape for both powertrains::

    available_energy = capacity * (level_pct / 100)
    consumption      = base * payload_factor * terrain_factor * temp_factor
    usable_range     = (available_energy / consumption) * (1 - reserve)

For electric, "energy" is kWh and consumption is kWh/km. For diesel, "energy" is
litres and consumption is L/km. One code path, two fuel types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Terrain = Literal["flat", "rolling", "hilly", "mountainous"]

#: Multiplier applied to baseline consumption for each terrain profile.
TERRAIN_FACTORS: dict[str, float] = {
    "flat": 1.00,
    "rolling": 1.08,
    "hilly": 1.20,
    "mountainous": 1.35,
}

#: Temperature at which every baseline consumption figure is quoted.
REFERENCE_TEMP_C = 15.0

#: Fractional consumption change per tonne of GCW away from the model's reference.
CONSUMPTION_PER_TONNE = 0.015

#: Clamp on the payload factor, so absurd inputs cannot produce absurd ranges.
PAYLOAD_FACTOR_BOUNDS = (0.70, 1.60)

#: Fraction of capacity held back as a reserve - you do not plan a trip to empty.
DEFAULT_RESERVE = 0.10

# Cold weather hurts an EV far more than a diesel: the battery is less efficient
# AND cabin heating is resistive, whereas a diesel heats the cab with waste heat.
_COLD_PENALTY_PER_C = {"electric": 0.012, "diesel": 0.004}
# Above ~25 C both powertrains pay for air conditioning, electric slightly more.
_HEAT_PENALTY_PER_C = {"electric": 0.005, "diesel": 0.003}
_AC_THRESHOLD_C = 25.0


class RangeModelError(ValueError):
    """Raised when inputs are outside the range the model can honestly handle."""


@dataclass(frozen=True)
class RangeEstimate:
    """A range estimate together with every assumption behind it."""

    range_km: float
    range_without_reserve_km: float
    available_energy: float
    energy_unit: str
    effective_consumption: float
    consumption_unit: str
    assumptions: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "range_km": round(self.range_km, 1),
            "range_without_reserve_km": round(self.range_without_reserve_km, 1),
            "available_energy": round(self.available_energy, 1),
            "energy_unit": self.energy_unit,
            "effective_consumption": round(self.effective_consumption, 4),
            "consumption_unit": self.consumption_unit,
            "assumptions": self.assumptions,
        }


def payload_factor(gcw_tonnes: float, reference_gcw_tonnes: float) -> float:
    """Consumption multiplier for running heavier or lighter than the reference GCW.

    Roughly 1.5% per tonne, which is the usual rule of thumb for heavy vehicles,
    clamped so a nonsense payload cannot yield a nonsense range.
    """
    if gcw_tonnes <= 0:
        raise RangeModelError("gcw_tonnes must be positive")
    raw = 1.0 + CONSUMPTION_PER_TONNE * (gcw_tonnes - reference_gcw_tonnes)
    low, high = PAYLOAD_FACTOR_BOUNDS
    return max(low, min(high, raw))


def terrain_factor(terrain: str) -> float:
    """Consumption multiplier for the route profile."""
    try:
        return TERRAIN_FACTORS[terrain]
    except KeyError:
        raise RangeModelError(
            f"unknown terrain {terrain!r}; expected one of {sorted(TERRAIN_FACTORS)}"
        ) from None


def temperature_factor(ambient_c: float, powertrain: str) -> float:
    """Consumption multiplier for ambient temperature.

    Symmetric around a comfortable band: cold costs energy (battery efficiency and
    cabin heating), heat costs energy (air conditioning), and 15 C is free.
    """
    if powertrain not in _COLD_PENALTY_PER_C:
        raise RangeModelError(f"unknown powertrain {powertrain!r}")
    if ambient_c < REFERENCE_TEMP_C:
        return 1.0 + _COLD_PENALTY_PER_C[powertrain] * (REFERENCE_TEMP_C - ambient_c)
    if ambient_c > _AC_THRESHOLD_C:
        return 1.0 + _HEAT_PENALTY_PER_C[powertrain] * (ambient_c - _AC_THRESHOLD_C)
    return 1.0


def estimate_range(
    *,
    powertrain: str,
    capacity: float,
    base_consumption: float,
    level_pct: float,
    reference_gcw_tonnes: float,
    gcw_tonnes: float | None = None,
    terrain: str = "flat",
    ambient_c: float = REFERENCE_TEMP_C,
    reserve: float = DEFAULT_RESERVE,
) -> RangeEstimate:
    """Estimate how far a truck can travel on the energy it currently has.

    Args:
        powertrain: ``"electric"`` or ``"diesel"``.
        capacity: Usable battery energy in kWh, or tank capacity in litres.
        base_consumption: kWh/km (electric) or L/100km (diesel) at reference conditions.
        level_pct: Current state of charge or fuel level, 0-100.
        reference_gcw_tonnes: GCW at which ``base_consumption`` was measured.
        gcw_tonnes: Actual gross combination weight; defaults to the reference.
        terrain: One of ``TERRAIN_FACTORS``.
        ambient_c: Ambient temperature in Celsius.
        reserve: Fraction of range held back, 0-1.

    Returns:
        A :class:`RangeEstimate` carrying the number and its assumptions.
    """
    if not 0 <= level_pct <= 100:
        raise RangeModelError("level_pct must be between 0 and 100")
    if not 0 <= reserve < 1:
        raise RangeModelError("reserve must be in [0, 1)")
    if capacity <= 0 or base_consumption <= 0:
        raise RangeModelError("capacity and base_consumption must be positive")

    gcw = reference_gcw_tonnes if gcw_tonnes is None else gcw_tonnes

    p_factor = payload_factor(gcw, reference_gcw_tonnes)
    t_factor = terrain_factor(terrain)
    temp_f = temperature_factor(ambient_c, powertrain)

    # Normalise diesel's L/100km to L/km so both powertrains share the maths.
    per_km_base = base_consumption if powertrain == "electric" else base_consumption / 100.0
    effective = per_km_base * p_factor * t_factor * temp_f

    available = capacity * (level_pct / 100.0)
    raw_range = available / effective
    usable_range = raw_range * (1.0 - reserve)

    return RangeEstimate(
        range_km=usable_range,
        range_without_reserve_km=raw_range,
        available_energy=available,
        energy_unit="kWh" if powertrain == "electric" else "L",
        effective_consumption=effective if powertrain == "electric" else effective * 100,
        consumption_unit="kWh/km" if powertrain == "electric" else "L/100km",
        assumptions={
            "level_pct": level_pct,
            "gcw_tonnes": gcw,
            "reference_gcw_tonnes": reference_gcw_tonnes,
            "terrain": terrain,
            "ambient_c": ambient_c,
            "reserve_fraction": reserve,
            "payload_factor": round(p_factor, 3),
            "terrain_factor": round(t_factor, 3),
            "temperature_factor": round(temp_f, 3),
        },
    )


def energy_needed(
    *,
    distance_km: float,
    powertrain: str,
    base_consumption: float,
    reference_gcw_tonnes: float,
    gcw_tonnes: float | None = None,
    terrain: str = "flat",
    ambient_c: float = REFERENCE_TEMP_C,
) -> float:
    """Energy (kWh) or fuel (litres) required to cover ``distance_km``.

    The inverse of :func:`estimate_range`, used for trip planning: how much do we
    need, versus how much is in the truck right now.
    """
    if distance_km < 0:
        raise RangeModelError("distance_km must be non-negative")

    gcw = reference_gcw_tonnes if gcw_tonnes is None else gcw_tonnes
    per_km_base = base_consumption if powertrain == "electric" else base_consumption / 100.0
    effective = (
        per_km_base
        * payload_factor(gcw, reference_gcw_tonnes)
        * terrain_factor(terrain)
        * temperature_factor(ambient_c, powertrain)
    )
    return distance_km * effective
