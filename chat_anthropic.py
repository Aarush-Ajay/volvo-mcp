"""Volvo fleet assistant - a CLI chatbot that is an MCP host.

Spawns the Volvo MCP server as a subprocess over stdio, discovers its tools at
runtime, and hands them to Claude. Nothing about the truck domain is hard-coded
here: if you add a tool to the server, this client picks it up on next launch.
That late binding is the whole point of MCP.

Usage::

    uv run chat.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

MODEL = "claude-opus-5"
MAX_TOKENS = 8000

SYSTEM_PROMPT = """\
You are a fleet dispatcher assistant for Volvo truck operators. You help drivers \
and fleet planners with range, charging and refuelling questions for both \
battery-electric and diesel Volvo trucks.

Rules:
- Use the tools for anything factual. Never state a range, distance, or station \
from memory - the tools are the source of truth.
- Never invent a charging or fuel station. Only mention sites a tool returned.
- Always surface the assumptions behind a range figure (payload, terrain, \
temperature, reserve). A bare number implies precision this model does not have.
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

console = Console()


def _format_tool_args(args: object) -> str:
    if not isinstance(args, dict) or not args:
        return ""
    return ", ".join(f"{k}={v!r}" for k, v in args.items())


def _summarise_result(payload: object) -> str | None:
    """A one-line hint about what a tool returned, for the transcript."""
    if not isinstance(payload, dict):
        return None
    if "error" in payload:
        return f"[red]error: {payload['error']}[/red]"
    bits = []
    if "source" in payload:
        colour = "yellow" if payload["source"] == "offline_seed" else "green"
        bits.append(f"[{colour}]source={payload['source']}[/{colour}]")
    if isinstance(payload.get("stations"), list):
        bits.append(f"{len(payload['stations'])} station(s)")
    if "range_km" in payload:
        bits.append(f"range={payload['range_km']} km")
    if "feasible_without_stopping" in payload:
        bits.append(f"feasible={payload['feasible_without_stopping']}")
    return "  ".join(bits) or None


async def converse(client: AsyncAnthropic, tools, history: list) -> None:
    """Run one user turn to completion, printing tool activity as it happens."""
    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        messages=history,
        tools=tools,
    )

    final_text: list[str] = []

    async for message in runner:
        # Mirror history ourselves - the runner keeps a private copy we cannot read.
        history.append({"role": "assistant", "content": message.content})

        for block in message.content:
            if block.type == "text" and block.text.strip():
                final_text.append(block.text)
            elif block.type == "tool_use":
                args = _format_tool_args(block.input)
                console.print(f"  [dim]>[/dim] [cyan]{block.name}[/cyan]([dim]{args}[/dim])")

        # Cached by the SDK - tools still execute exactly once.
        tool_response = await runner.generate_tool_call_response()
        if tool_response is None:
            break

        history.append(tool_response)
        for item in tool_response.get("content", []):
            if isinstance(item, dict) and item.get("type") == "tool_result":
                content = item.get("content")
                text = content[0]["text"] if isinstance(content, list) and content else content
                try:
                    summary = _summarise_result(json.loads(text))
                except (TypeError, ValueError):
                    summary = None
                if summary:
                    console.print(f"    [dim]<[/dim] {summary}")

    if final_text:
        console.print()
        console.print(Markdown("\n\n".join(final_text)))
    console.print()


async def main() -> int:
    load_dotenv()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print(
            "[red]ANTHROPIC_API_KEY is not set.[/red] Copy [cyan].env.example[/cyan] to "
            "[cyan].env[/cyan] and add your key, or export it in the shell."
        )
        return 1

    client = AsyncAnthropic()
    server = StdioServerParameters(command="uv", args=["run", "volvo-mcp-server"])

    console.print(Panel(WELCOME, border_style="blue", padding=(1, 2)))

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            mcp_tools = (await session.list_tools()).tools
            tools = [async_mcp_tool(t, session) for t in mcp_tools]
            console.print(
                f"[green]Connected[/green] to MCP server - {len(mcp_tools)} tools available.\n"
            )

            history: list = []

            while True:
                try:
                    user_input = (await asyncio.to_thread(console.input, "[bold blue]you>[/bold blue] ")).strip()
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

                # Snapshot so a failed turn can be rolled back whole - a partially
                # appended turn would leave tool_use blocks without their results.
                checkpoint = len(history)
                history.append({"role": "user", "content": user_input})
                try:
                    await converse(client, tools, history)
                except Exception as exc:  # noqa: BLE001 - keep the REPL alive
                    console.print(f"[red]Request failed:[/red] {exc}\n")
                    del history[checkpoint:]


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
