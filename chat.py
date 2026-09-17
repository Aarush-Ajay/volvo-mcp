"""Volvo fleet assistant - a CLI chatbot that is an MCP host.

Spawns the Volvo MCP server as a subprocess over stdio, discovers its tools at
runtime, and hands them to Gemini. Nothing about the truck domain is hard-coded
here: if you add a tool to the server, this client picks it up on next launch.
That late binding is the whole point of MCP.

The Gemini SDK accepts an MCP ``ClientSession`` directly as a tool, so it does
both halves itself - reads the schemas off the session, and calls back through
it when the model asks for a tool. We only render what happened.

Usage::

    uv run chat.py

Requires ``GEMINI_API_KEY`` - free, no card, from https://aistudio.google.com/apikey
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from dotenv import load_dotenv
from google import genai
from google.genai import types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

MODEL = "gemini-3.6-flash"
MAX_TOOL_ROUNDS = 8

SYSTEM_PROMPT = """\
You are a fleet dispatcher assistant for Volvo truck operators. You help drivers \
and fleet planners with range, charging and refuelling questions for both \
battery-electric and diesel Volvo trucks.

That is the whole of your remit. If asked anything outside it - general knowledge, \
coding, other manufacturers, advice of any other kind - say in one line that it is \
outside what you cover, and name what you can help with instead. Do not answer it \
anyway, and do not answer it first and then add a caveat. Answering from your own \
knowledge is the exact habit that makes a range figure untrustworthy, so the rule \
holds even when the question is harmless and you are certain of the answer.

Rules:
- Use the tools for anything factual. Never state a range, distance, or station \
from memory - the tools are the source of truth.
- Never invent a charging or fuel station. Only mention sites a tool returned.
- Always surface the assumptions behind a range figure (payload, terrain, \
temperature, reserve). A bare number implies precision this model does not have.
- Report assumptions exactly as the tool returned them. Do not annotate them with \
reasons, road names or conditions the tool did not supply - a default is a default, \
not a judgement about this particular trip.
- The reserve is a fraction of RANGE withheld, not a slice of the charge level: a \
10% reserve means 90% of the computed range, not "40% charge minus 10%". Do not \
explain how a figure was derived unless the tool said.
- State when a value was assumed rather than given, especially payload carried over \
from an earlier question.
- Quote only figures a tool returned. Never derive, interpolate or estimate a \
comparison figure - no "compared to X in optimal conditions" unless X came from a \
tool call you actually made. If a comparison would help, call the tool again with \
the other conditions.
- `plan_trip` searches near the ORIGIN and knows nothing about the route. Never \
describe a station as being "on the way", "along the route", or on a named road - \
not even when the station's own name contains one. Say how far it is from the \
origin and let the dispatcher judge.
- A reserve is withheld FROM the range. A range figure quoted to the user is what \
remains after the reserve, so never say a range "includes" it.
- If a result is tagged `source: offline_seed`, say so plainly - it is bundled \
fallback data, not live availability, and must not be treated as operational.
- Use metric units throughout.
- Be concise and practical. A dispatcher wants the answer and the caveat, not an essay.
"""

WELCOME = """\
[bold]Volvo Fleet Assistant[/bold]
Ask about range, charging or refuelling for Volvo electric and diesel trucks.

Commands:  [cyan]/tools[/cyan] list MCP tools   [cyan]/reset[/cyan] clear history   [cyan]/quit[/cyan] exit

Try: [dim]"FH Electric at 40%, 38 tonnes, hilly, 2 C - how far can I get?"[/dim]
     [dim]"Can I make Gothenburg to Malmo on 55% in an FH Electric?"[/dim]\
"""

# Plain ASCII on purpose: classic cmd.exe mangles emoji and many Unicode glyphs.
# Built as Text rather than markup, because rich would read any square bracket
# in the art as a style tag.
TRUCK = r"""
  _____________________________   _______
 |                             | |  __   \
 |                             | | |__|   \____
 |_____________________________|_|_____________|
    (O)  (O)                       (O)      (O)
"""

console = Console()


def _banner() -> Group:
    """Truck art above the welcome text, for the startup panel."""
    *body, wheels = TRUCK.strip("\n").splitlines()
    art = Text()
    for line in body:
        art.append(line + "\n", style="cyan")
    art.append(wheels, style="bold white")
    return Group(art, Text(), WELCOME)

# The SDK advises using chats.send_message rather than models.generate_content
# for tool calling. We cannot: that path always converts the request config to a
# model object and then deep-copies it, which a live MCP session cannot survive
# (see `_config` below). The advice does not apply to the route we are forced
# onto, so drop just that one line rather than living with it every turn.
logging.getLogger("google_genai.models").addFilter(
    lambda record: "not recommended" not in record.getMessage()
)


def _format_args(args: object) -> str:
    if not isinstance(args, dict) or not args:
        return ""
    return ", ".join(f"{k}={v!r}" for k, v in args.items())


def _unwrap(payload: object) -> object:
    """Dig the tool's JSON out of however the SDK wrapped it.

    The MCP adapter stringifies the whole ``CallToolResult`` and hands it over as
    ``{"result": "meta=None content=[TextContent(... text='{...}' ...)]"}`` - a
    Python repr, not JSON. So: unwrap the known keys, and if what falls out is a
    string that merely *contains* JSON, fish the outermost object out of it.
    """
    if isinstance(payload, list):
        return _unwrap(payload[0]) if payload else None

    if isinstance(payload, dict):
        for key in ("result", "content", "text", "output", "response"):
            if key in payload:
                return _unwrap(payload[key])
        return payload

    if not isinstance(payload, str):
        return payload

    try:
        return json.loads(payload)
    except ValueError:
        pass

    # Unescape *before* scanning: in the repr a JSON \" is doubled to \\", which
    # a brace scanner would misread as the end of the string.
    forms = [payload]
    try:
        forms.insert(0, payload.encode("latin-1", "backslashreplace").decode("unicode_escape"))
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass

    for form in forms:
        blob = _first_json_object(form)
        if blob is None:
            continue
        try:
            return json.loads(blob)
        except ValueError:
            continue
    return None


def _first_json_object(text: str) -> str | None:
    """Slice out the first balanced ``{...}`` in ``text``.

    Not ``rfind('}')`` - a CallToolResult repr can carry a second object after
    the one we want (``structuredContent=...``), and spanning to the last brace
    would produce something that is not valid JSON.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _num(value: object) -> str:
    """Render a number without a pointless trailing ``.0``."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _summarise(payload: object) -> str | None:
    """A one-line hint about what a tool returned, for the transcript."""
    data = _unwrap(payload)
    if not isinstance(data, dict):
        return None
    if "error" in data:
        return f"[red]error: {data['error']}[/red]"
    bits = []
    if "source" in data:
        colour = "yellow" if data["source"] == "offline_seed" else "green"
        bits.append(f"[{colour}]source={data['source']}[/{colour}]")
    if isinstance(data.get("stations"), list):
        bits.append(f"{len(data['stations'])} station(s)")
    if "range_km" in data:
        bits.append(f"range={data['range_km']} km")
    if "available_range_km" in data:
        bits.append(f"range={data['available_range_km']} km")
    if "estimated_road_distance_km" in data:
        bits.append(f"distance={data['estimated_road_distance_km']} km")
    # The model has been caught inventing a consumption figure to compare
    # against, so print the real one.
    if "effective_consumption" in data:
        bits.append(f"{data['effective_consumption']} {data.get('consumption_unit', '')}".strip())
    if "feasible_without_stopping" in data:
        bits.append(f"feasible={data['feasible_without_stopping']}")
    return "  ".join(bits) or None


#: Assumption fields worth showing, in dispatcher-relevant order.
_ASSUMPTION_FORMAT = (
    ("level_pct", lambda v: f"{_num(v)}%"),
    ("gcw_tonnes", lambda v: f"{_num(v)} t"),
    ("terrain", str),
    ("ambient_c", lambda v: f"{_num(v)} C"),
    ("reserve_fraction", lambda v: f"{_num(v * 100)}% reserve"),
)


def _assumptions_line(payload: object) -> str | None:
    """The tool's own assumptions, verbatim.

    This exists because the model paraphrases them loosely - inventing reasons
    for a default, or misdescribing the reserve. Printing what the tool actually
    said puts ground truth on screen next to the prose, where a wrong gloss is
    visible rather than invisible.
    """
    data = _unwrap(payload)
    if not isinstance(data, dict):
        return None
    assumptions = data.get("assumptions")
    if not isinstance(assumptions, dict):
        return None
    parts = [
        render(assumptions[key])
        for key, render in _ASSUMPTION_FORMAT
        if assumptions.get(key) is not None
    ]
    return ", ".join(parts) or None


def _render_tool_activity(turn: list) -> None:
    """Print the tool calls the SDK made on our behalf during one turn."""
    for content in turn:
        for part in getattr(content, "parts", None) or []:
            call = getattr(part, "function_call", None)
            if call is not None:
                console.print(
                    f"  [dim]>[/dim] [cyan]{call.name}[/cyan]([dim]{_format_args(call.args)}[/dim])"
                )
            reply = getattr(part, "function_response", None)
            if reply is not None:
                summary = _summarise(reply.response)
                if summary:
                    console.print(f"    [dim]<[/dim] {summary}")
                assumed = _assumptions_line(reply.response)
                if assumed:
                    console.print(f"      [dim]assumed: {assumed}[/dim]")


def _config(session: ClientSession) -> dict:
    """Request config, as a plain dict - deliberately not a GenerateContentConfig.

    A live MCP ``ClientSession`` owns asyncio Tasks, which cannot be copied, so
    any code path that deep-copies the config dies with "cannot pickle
    '_asyncio.Task' object". ``models.generate_content`` only copies when handed
    a model object; given a dict it constructs the config instead. Hence a dict.

    This is also why we do not use ``client.aio.chats`` despite the SDK
    recommending it: ``send_message`` normalises any config to a model object
    before copying it, so there is no dict escape hatch there. Checked against
    google-genai 2.23.0.
    """
    return {
        "system_instruction": SYSTEM_PROMPT,
        "tools": [session],  # the SDK discovers and calls through this
        "automatic_function_calling": {"maximum_remote_calls": MAX_TOOL_ROUNDS},
    }


async def main() -> int:
    load_dotenv()

    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        console.print(
            "[red]GEMINI_API_KEY is not set.[/red] Get a free key (no card) at "
            "[cyan]https://aistudio.google.com/apikey[/cyan], then copy "
            "[cyan].env.example[/cyan] to [cyan].env[/cyan] and add it."
        )
        return 1

    client = genai.Client()
    server = StdioServerParameters(command="uv", args=["run", "volvo-mcp-server"])

    console.print(Panel(_banner(), border_style="blue", padding=(1, 2)))

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            mcp_tools = (await session.list_tools()).tools
            console.print(
                f"[green]Connected[/green] to MCP server - {len(mcp_tools)} tools available.\n"
            )

            history: list[types.Content] = []

            while True:
                try:
                    user_input = (
                        await asyncio.to_thread(console.input, "[bold blue]you>[/bold blue] ")
                    ).strip()
                except (EOFError, KeyboardInterrupt):
                    console.print("\nBye.")
                    return 0

                if not user_input:
                    continue
                if user_input in {"/quit", "/exit"}:
                    console.print("Bye.")
                    return 0
                if user_input == "/reset":
                    history.clear()
                    console.print("[yellow]History cleared.[/yellow]\n")
                    continue
                if user_input == "/tools":
                    for t in mcp_tools:
                        required = ", ".join(t.input_schema.get("required", []))
                        console.print(f"  [cyan]{t.name}[/cyan]([dim]{required}[/dim])")
                        if t.description:
                            console.print(f"    [dim]{t.description.splitlines()[0]}[/dim]")
                    console.print()
                    continue

                sent = history + [
                    types.Content(role="user", parts=[types.Part(text=user_input)])
                ]

                try:
                    response = await client.aio.models.generate_content(
                        model=MODEL, contents=sent, config=_config(session)
                    )
                except Exception as exc:  # noqa: BLE001 - keep the REPL alive
                    console.print(f"[red]Request failed:[/red] {exc}\n")
                    continue

                # AFC history, when present, is `sent` plus this turn's tool
                # traffic - so everything past len(sent) is new.
                afc = response.automatic_function_calling_history or []
                _render_tool_activity(afc[len(sent):])

                history = list(afc) if afc else sent
                if response.candidates and response.candidates[0].content:
                    history.append(response.candidates[0].content)

                if response.text:
                    console.print()
                    console.print(Markdown(response.text))
                console.print()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
