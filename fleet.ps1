<#
    Volvo Fleet Assistant - free CLI chatbot.

    Runs the same MCP server as chat.py, but uses the Claude Code CLI as the
    host instead of the Anthropic API. No ANTHROPIC_API_KEY, no per-token cost.

    Usage:
        .\fleet.ps1                  interactive chat
        .\fleet.ps1 "question"       one-shot answer, then exit
#>

$ErrorActionPreference = "Stop"

$prompt = Get-Content (Join-Path $PSScriptRoot "fleet-prompt.txt") -Raw

# Build the MCP config at run time rather than checking one in. `uv --directory`
# needs an absolute path, and hard-coding wherever this repo happened to be
# cloned would both break for everyone else and publish the author's home path.
$config = @{
    mcpServers = @{
        volvo = @{
            command = "uv"
            args    = @("--directory", $PSScriptRoot, "run", "volvo-mcp-server")
        }
    }
} | ConvertTo-Json -Depth 5

$mcp = Join-Path ([System.IO.Path]::GetTempPath()) "volvo-fleet-mcp.json"
# No BOM - a JSON parser will choke on one.
[System.IO.File]::WriteAllText($mcp, $config, (New-Object System.Text.UTF8Encoding($false)))

# Only the truck tools - no file access, no shell, no web.
$tools = @(
    "mcp__volvo__list_truck_models",
    "mcp__volvo__get_truck_specs",
    "mcp__volvo__estimate_range",
    "mcp__volvo__find_charging_stations",
    "mcp__volvo__find_fuel_stations",
    "mcp__volvo__plan_trip"
) -join ","

$args_ = @(
    "--system-prompt", $prompt,
    "--mcp-config", $mcp,
    "--strict-mcp-config",
    "--allowedTools", $tools
)

# A question on the command line runs headless; no arguments starts the REPL.
if ($args.Count -gt 0) {
    $args_ += @("-p", ($args -join " "))
}

& claude @args_
