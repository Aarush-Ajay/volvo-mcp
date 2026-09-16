# Volvo Fleet Assistant — MCP server + CLI chatbot

An MCP server exposing operational tools for Volvo truck fleets, plus a CLI chatbot
that acts as an **MCP host** — it spawns the server, discovers its tools at runtime,
and lets Claude call them.

Both halves are here on purpose. A chatbot alone is a wrapper; an MCP server alone is
a plugin. Together they demonstrate the actual protocol: tool discovery, schema
negotiation, and model-driven invocation over stdio.

```
┌──────────────┐   stdio / JSON-RPC   ┌─────────────────┐
│   chat.py    │ ───────────────────► │    server.py    │
│  (MCP host)  │ ◄─────────────────── │  (MCP server)   │
└──────┬───────┘   tools + results    └────────┬────────┘
       │                                       │
       ▼ Messages API                          ▼
  ┌──────────┐                    ┌────────────────────────┐
  │  Claude  │                    │ Open Charge Map        │
  └──────────┘                    │ OSM Overpass           │
                                  │ Nominatim              │
                                  │ (offline seed fallback)│
                                  └────────────────────────┘
```

## What it answers

- *"FH Electric at 40% charge, 38 tonnes, hilly, 2 °C — how far can I get?"*
- *"Nearest truck charging to Gothenburg with at least 350 kW?"*
- *"Can I make Gothenburg → Malmö on 55% in an FH Electric?"*
- *"Same trip in a diesel FH with a quarter tank."*

## Three ways to chat

| | `chat.py` | `fleet.cmd` | `chat_anthropic.py` |
|---|---|---|---|
| Host | this repo | Claude Code CLI | this repo |
| Model | Gemini 2.5 Flash | whatever Claude Code runs | Claude Opus 5 |
| Needs | `GEMINI_API_KEY` (free, no card) | Claude Code installed | `ANTHROPIC_API_KEY` (paid) |
| Cost | free tier | none extra | metered tokens |

All three drive the **same** MCP server over stdio and give the same answers —
the tools are the source of truth, so only the prose around them differs.

[`chat.py`](chat.py) is the default: a standalone chatbot that owns its terminal,
spawns the server itself, and costs nothing. [`fleet.cmd`](fleet.cmd) is a shortcut
for when you would rather not manage a key at all — it launches `claude` with the
dispatcher prompt, `--strict-mcp-config`, and only the six truck tools allowed.
[`chat_anthropic.py`](chat_anthropic.py) is the same host written against the
Anthropic SDK, kept because it drives the tool loop by hand rather than delegating
it to the SDK.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/). No admin rights needed:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then:

```powershell
cd volvo-mcp
uv sync
copy .env.example .env    # add your GEMINI_API_KEY
uv run chat.py
```

### Keys

| Variable | Needed for | If missing |
|---|---|---|
| `GEMINI_API_KEY` | `chat.py` only | Chatbot refuses to start. Free, no card, from [aistudio.google.com/apikey](https://aistudio.google.com/apikey). The MCP server itself needs no key. |

| `ANTHROPIC_API_KEY` | `chat_anthropic.py` only | That variant refuses to start. Leave blank unless you use it. |

### Reading the transcript

Each tool call prints what was asked and what came back:

```
  > estimate_range(model='FH Electric', level_pct=40, payload_tonnes=38, ...)
    < range=111.9 km  1.4801 kWh/km
      assumed: 40%, 38 t, hilly, 2 C, 10% reserve
```

The `<` and `assumed:` lines are the **tool's own output**, not the model's account
of it. That distinction is the point. In testing, the model quoted every tool figure
correctly but garnished them with invented ones — a consumption figure to compare
against that no tool returned, a road name the tool could not know, a reserve
described as included when it had been withheld. The numbers were right and the prose
around them was not.

The system prompt forbids all three, and a small model still leaks. So the
authoritative version is printed alongside, where a wrong gloss is visible instead of
invisible. If the prose and the `<` line disagree, the `<` line is right.

### Free-tier limits

The Gemini free tier caps **requests per day, per model**, and one chat question can
cost more than one request when a tool is called. Hitting it gives a `429
RESOURCE_EXHAUSTED` naming the quota and a retry delay. It resets on its own.

The model is one constant at the top of [`chat.py`](chat.py):

```python
MODEL = "gemini-3.6-flash"
```

Quotas differ per model, so switching is the cheapest way around a 429 — the
`-lite` variants are the most generous. `uv run python -c "from google import genai;
print([m.name for m in genai.Client().models.list()])"` lists what your key can reach.
| `OCM_API_KEY` | Live charging lookups | Falls back to the bundled seed dataset, tagged `source: offline_seed`. Free key from [openchargemap.org](https://openchargemap.org) → profile → *my apps* → *Register An Application*. |

## Tools

| Tool | What it does |
|---|---|
| `list_truck_models` | Enumerate models, filterable `electric` / `diesel` |
| `get_truck_specs` | Battery/UBE or tank size, consumption baseline, charging rates, **and the source of every figure** |
| `estimate_range` | Range from current charge/fuel, adjusted for payload, terrain and temperature |
| `find_charging_stations` | Truck-capable DC charging near a place (defaults to ≥150 kW) |
| `find_fuel_stations` | Diesel stations near a place, via OpenStreetMap |
| `plan_trip` | Geocodes both ends, checks feasibility, and suggests a stop when short |

## The range model

An energy calculation rather than a lookup table, so it responds to load and weather:

```
available   = capacity × (level_pct / 100)
consumption = base × payload_factor × terrain_factor × temp_factor
range_km    = (available / consumption) × (1 − reserve)
```

Electric and diesel share one code path — only the units differ (kWh/km vs L/100km).
Cold weather is modelled as hurting an EV about 3× more than a diesel, because a
diesel heats its cab with waste heat while an EV pays for it out of the battery.

Every range response returns its **assumptions** alongside the number, so the
assistant reports *"~112 km, assuming 38 t, hilly, 2 °C, 10% reserve"* rather than a
bare figure implying false precision.

## Use from Claude Code

The same server works in any MCP host. From the repository root:

```powershell
claude mcp add volvo -- uv --directory $PWD run volvo-mcp-server
```

`uv --directory` needs an absolute path, so run this from the clone rather than
copying a path from elsewhere. On bash, use `$(pwd)` in place of `$PWD`.

## Development

```powershell
uv run pytest -v                          # range model + transcript tests
uv run check_keys.py                      # which data sources are live
uv run mcp dev src/volvo_mcp/server.py    # MCP Inspector
```

`check_keys.py` calls the MCP server directly, so it costs no model quota. It exits
non-zero while charging is still answering from the seed dataset, which makes it
usable as a smoke test.

The range model (`range_model.py`) is pure — no network, no file I/O — which is what
makes it worth testing. The `test_green_truck_route_anchor` test pins it against a
real measured result: Volvo's FH Electric averaged 1.1 kWh/km and covered 345 km on
the Green Truck Route.

## Data accuracy

**Specification figures are public manufacturer and press figures, not certified
engineering data.** Every model in `trucks.json` carries a `source` field saying where
its numbers came from and whether they are measured, derived, or estimated. Some are
explicitly estimates — the FM, FMX and FL diesel tank and consumption figures were not
published in the sources consulted and are scaled from neighbouring models.

Other limits worth knowing:

- **Distances are not routed.** `plan_trip` uses great-circle distance inflated by a
  1.25 winding factor. Good enough for a feasibility check; not a route plan.
- **Charging availability is not live.** Neither provider reports real-time occupancy.
- **OSM `hgv=yes` coverage is patchy**, so `hgv_only` defaults to off — absence of the
  tag is not evidence a truck cannot use a site.
- **Seed data is illustrative.** Locations are real freight-corridor places, but
  operator and power details in `seed_stations.json` do not reflect what is actually
  installed.

Do not use this for real dispatch decisions without validating against operational data.

## Corporate networks

If you see `CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain`,
that is TLS inspection. Python uses `certifi` rather than the OS trust store where the
corporate root CA lives. `_http.py` handles this via `truststore`, which delegates
verification to the OS store. Verification is never disabled.
