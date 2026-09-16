"""Loading and lookup for the Volvo truck specification table."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
TRUCKS_FILE = DATA_DIR / "trucks.json"


class UnknownModelError(KeyError):
    """Raised when a model identifier cannot be resolved to a spec entry.

    ``KeyError.__str__`` wraps its argument in ``repr()``, which would surface to
    callers as a double-quoted string. Override it so the message reads cleanly.
    """

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    return json.loads(TRUCKS_FILE.read_text(encoding="utf-8"))


def all_models() -> list[dict[str, Any]]:
    """Every model in the spec table."""
    return _load()["models"]


def reference_conditions() -> dict[str, Any]:
    """The conditions under which every ``base_consumption`` figure is quoted."""
    return _load()["reference_conditions"]


def list_models(powertrain: str | None = None) -> list[dict[str, Any]]:
    """Model summaries, optionally filtered to ``electric`` or ``diesel``."""
    models = all_models()
    if powertrain:
        wanted = powertrain.lower()
        if wanted not in {"electric", "diesel"}:
            raise ValueError("powertrain must be 'electric' or 'diesel'")
        models = [m for m in models if m["powertrain"] == wanted]

    return [
        {
            "id": m["id"],
            "name": m["name"],
            "powertrain": m["powertrain"],
            "segment": m["segment"],
            "capacity": _capacity(m),
            "capacity_unit": "kWh usable" if m["powertrain"] == "electric" else "L",
            "official_range_km": m.get("official_range_km"),
        }
        for m in models
    ]


def _normalise(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def get_model(model: str) -> dict[str, Any]:
    """Resolve a model by id or name, tolerantly.

    Accepts ``fh-electric``, ``FH Electric``, ``volvo fh electric`` and similar.
    Falls back to a unique substring match so the chatbot does not have to know
    exact identifiers, but refuses ambiguous matches rather than guessing.
    """
    if not model or not model.strip():
        raise UnknownModelError("model must be a non-empty string")

    key = _normalise(model)
    key = key.removeprefix("volvo")
    models = all_models()

    for m in models:
        if _normalise(m["id"]) == key or _normalise(m["name"]).removeprefix("volvo") == key:
            return m

    partial = [
        m for m in models
        if key in _normalise(m["id"]) or key in _normalise(m["name"])
    ]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        names = ", ".join(m["id"] for m in partial)
        raise UnknownModelError(
            f"{model!r} is ambiguous - it matches {names}. Please be more specific."
        )

    available = ", ".join(m["id"] for m in models)
    raise UnknownModelError(f"unknown model {model!r}. Available models: {available}")


def _capacity(m: dict[str, Any]) -> float:
    """Usable energy store: kWh for electric, litres for diesel."""
    if m["powertrain"] == "electric":
        return m["usable_battery_kwh"]
    return m["tank_capacity_l"]


def capacity_of(m: dict[str, Any]) -> float:
    return _capacity(m)
