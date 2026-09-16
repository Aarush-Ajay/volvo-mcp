"""Tests for the pure range model.

The most valuable test here is `test_green_truck_route_anchor`: it pins the model
against a real measured result rather than a marketing figure.
"""

from __future__ import annotations

import pytest

from volvo_mcp.range_model import (
    DEFAULT_RESERVE,
    RangeModelError,
    energy_needed,
    estimate_range,
    payload_factor,
    temperature_factor,
    terrain_factor,
)

ELECTRIC = dict(powertrain="electric", reference_gcw_tonnes=40.0)
DIESEL = dict(powertrain="diesel", reference_gcw_tonnes=40.0)


def test_green_truck_route_anchor():
    """Reproduce the measured Volvo FH Electric result.

    On the Green Truck Route the FH Electric averaged 1.1 kWh/km and covered
    345 km on a single charge, implying ~379.5 kWh actually drawn. With no
    reserve and reference conditions the model must return that distance.
    """
    est = estimate_range(
        capacity=379.5, base_consumption=1.1, level_pct=100, reserve=0.0, **ELECTRIC
    )
    assert est.range_km == pytest.approx(345.0, abs=0.5)
    assert est.effective_consumption == pytest.approx(1.1)


def test_reserve_is_withheld():
    full = estimate_range(capacity=460, base_consumption=1.1, level_pct=100, reserve=0.0, **ELECTRIC)
    held = estimate_range(capacity=460, base_consumption=1.1, level_pct=100, reserve=0.10, **ELECTRIC)
    assert held.range_km == pytest.approx(full.range_km * 0.9)
    assert held.range_without_reserve_km == pytest.approx(full.range_km)


def test_default_reserve_applied():
    est = estimate_range(capacity=460, base_consumption=1.1, level_pct=100, **ELECTRIC)
    assert est.assumptions["reserve_fraction"] == DEFAULT_RESERVE


def test_diesel_unit_conversion():
    """29 L/100km from a 1000 L tank is 3448 km before reserve, not 34 km."""
    est = estimate_range(
        capacity=1000, base_consumption=29.0, level_pct=100, reserve=0.0, **DIESEL
    )
    assert est.range_km == pytest.approx(1000 / 0.29, rel=1e-6)
    assert est.range_km > 3000
    # Consumption is reported back in the units it was supplied in.
    assert est.effective_consumption == pytest.approx(29.0)
    assert est.consumption_unit == "L/100km"
    assert est.energy_unit == "L"


def test_partial_level_scales_linearly():
    half = estimate_range(capacity=460, base_consumption=1.1, level_pct=50, **ELECTRIC)
    full = estimate_range(capacity=460, base_consumption=1.1, level_pct=100, **ELECTRIC)
    assert half.range_km == pytest.approx(full.range_km / 2)


def test_heavier_payload_shortens_range():
    light = estimate_range(capacity=460, base_consumption=1.1, level_pct=100, gcw_tonnes=25, **ELECTRIC)
    ref = estimate_range(capacity=460, base_consumption=1.1, level_pct=100, **ELECTRIC)
    heavy = estimate_range(capacity=460, base_consumption=1.1, level_pct=100, gcw_tonnes=44, **ELECTRIC)
    assert light.range_km > ref.range_km > heavy.range_km


def test_terrain_ordering():
    ranges = [
        estimate_range(
            capacity=460, base_consumption=1.1, level_pct=100, terrain=t, **ELECTRIC
        ).range_km
        for t in ("flat", "rolling", "hilly", "mountainous")
    ]
    assert ranges == sorted(ranges, reverse=True), "rougher terrain must not increase range"


def test_cold_hurts_electric_more_than_diesel():
    """A diesel heats its cab with waste heat; an EV pays for it out of the battery."""
    e_pen = temperature_factor(-10, "electric")
    d_pen = temperature_factor(-10, "diesel")
    assert e_pen > d_pen > 1.0


def test_mild_temperature_is_free():
    assert temperature_factor(15, "electric") == 1.0
    assert temperature_factor(20, "electric") == 1.0


def test_heat_also_costs_energy():
    assert temperature_factor(40, "electric") > 1.0


def test_payload_factor_is_clamped():
    assert payload_factor(500, 40) == 1.60
    assert payload_factor(0.5, 40) == 0.70


def test_terrain_factor_rejects_unknown():
    with pytest.raises(RangeModelError, match="unknown terrain"):
        terrain_factor("swamp")


@pytest.mark.parametrize("level", [-1, 101])
def test_level_validation(level):
    with pytest.raises(RangeModelError, match="level_pct"):
        estimate_range(capacity=460, base_consumption=1.1, level_pct=level, **ELECTRIC)


def test_reserve_validation():
    with pytest.raises(RangeModelError, match="reserve"):
        estimate_range(capacity=460, base_consumption=1.1, level_pct=50, reserve=1.0, **ELECTRIC)


def test_energy_needed_inverts_estimate_range():
    """Energy for a distance, then range from that energy, must round-trip."""
    kwargs = dict(base_consumption=1.1, terrain="hilly", ambient_c=-5, gcw_tonnes=44, **ELECTRIC)
    needed = energy_needed(distance_km=200, **kwargs)
    back = estimate_range(capacity=needed, level_pct=100, reserve=0.0, **kwargs)
    assert back.range_km == pytest.approx(200.0)


def test_assumptions_are_reported():
    est = estimate_range(
        capacity=460, base_consumption=1.1, level_pct=40,
        gcw_tonnes=38, terrain="hilly", ambient_c=2, **ELECTRIC,
    )
    a = est.assumptions
    assert a["gcw_tonnes"] == 38
    assert a["terrain"] == "hilly"
    assert a["ambient_c"] == 2
    assert a["terrain_factor"] == 1.20
    assert a["temperature_factor"] > 1.0
