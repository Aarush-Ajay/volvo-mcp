"""Check which data sources are live, without spending any model quota.

    uv run check_keys.py

Calls the MCP server directly - no Gemini, no Claude - and reports whether
charging lookups are hitting Open Charge Map or falling back to the bundled
seed dataset.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PLACE = "Gothenburg"
RADIUS_KM = 100


async def main() -> int:
    load_dotenv()

    gemini = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    ocm = bool(os.environ.get("OCM_API_KEY", "").strip())
    print(f"GEMINI_API_KEY : {'set' if gemini else 'NOT set - chat.py will not start'}")
    print(f"OCM_API_KEY    : {'set' if ocm else 'not set - charging falls back to seed data'}")

    server = StdioServerParameters(command="uv", args=["run", "volvo-mcp-server"])
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "find_charging_stations", {"location": PLACE, "radius_km": RADIUS_KM}
            )
            data = json.loads(result.content[0].text)

    source = data.get("source")
    stations = data.get("stations", [])
    print(f"\ncharging near {PLACE} ({RADIUS_KM} km): {len(stations)} station(s)")
    print(f"source: {source}")
    if reason := data.get("fallback_reason"):
        print(f"reason: {reason}")

    for s in stations[:5]:
        operator = f" ({s['operator']})" if s.get("operator") else ""
        print(f"  {s['distance_km']:>6.1f} km  {s.get('power_kw', '?'):>4} kW  {s['name']}{operator}")

    if source == "open_charge_map":
        print("\nLIVE - Open Charge Map is answering.")
        return 0
    print("\nFALLBACK - still bundled seed data. Add OCM_API_KEY to .env to go live.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
