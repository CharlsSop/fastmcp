"""
Firewall Longevity Test - MCP Server
=====================================
An MCP server designed for firewall throughput and longevity testing.

Exposes all core MCP primitives:
  - Tools       (tools/list, tools/call)
  - Resources   (resources/list, resources/read) — static, large, template
  - Prompts     (prompts/list, prompts/get)
  - Notifications — notify_tool sends tools/list_changed server-push
  - Progress    — slow_tool reports incremental progress per step
  - Binary      — binary_tool returns base64-encoded image content
  - Errors      — error_tool returns isError: true response

Modes:
  Normal  : small tool set + all primitives
  Large   : 260 padding tools → tools/list ≈100 KB  (use --large)

Usage:
    python server.py
    python server.py --large
    python server.py --host 0.0.0.0 --port 9000 --large
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

from mcp.types import ToolListChangedNotification

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import Image

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser(description="Longevity MCP Server")
parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
parser.add_argument("--port", type=int, default=8000, help="Port to bind (default: 8000)")
parser.add_argument(
    "--large",
    action="store_true",
    default=False,
    help="Register many padded tools so tools/list response is ~100 KB",
)
args = parser.parse_args()

# ---------------------------------------------------------------------------
# Server definition
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "Longevity Test Server",
    instructions=(
        "This server is used for firewall longevity and throughput testing. "
        "It exposes all MCP primitives: tools, resources, prompts, notifications, "
        "progress, binary content, and error responses."
    ),
)

# ---------------------------------------------------------------------------
# Core tools (always registered)
# ---------------------------------------------------------------------------

LARGE_PAYLOAD_SIZE = 100_000  # bytes target for large_data tool response


@mcp.tool
def ping() -> str:
    """Lightweight heartbeat tool. Returns server timestamp."""
    return json.dumps({"status": "ok", "timestamp": time.time(), "server": "longevity-test"})


@mcp.tool
def large_data(size_kb: int = 100) -> str:
    """Return a large JSON payload of approximately `size_kb` kilobytes.

    Args:
        size_kb: Approximate size of the returned payload in kilobytes (default 100).
    """
    target = size_kb * 1024
    # Build a repeating data block
    block = "X" * 512
    entries = target // len(block) + 1
    payload = {
        "description": "Large test payload for firewall longevity testing",
        "size_kb": size_kb,
        "timestamp": time.time(),
        "data": [{"index": i, "chunk": block} for i in range(entries)],
    }
    return json.dumps(payload)


@mcp.tool
def server_info() -> str:
    """Return server configuration information."""
    return json.dumps(
        {
            "mode": "large" if args.large else "normal",
            "host": args.host,
            "port": args.port,
            "registered_tools": "many (padded)" if args.large else "few (lightweight)",
            "primitives": ["tools", "resources", "prompts", "notifications", "progress", "binary", "errors"],
            "timestamp": time.time(),
        }
    )


@mcp.tool
def error_tool(message: str = "Simulated error for firewall testing") -> str:
    """Tool that always responds with isError: true in the MCP result.

    Tests whether the firewall correctly passes through MCP error-response
    payloads (HTTP 200 with isError: true body). This is a distinct traffic
    pattern from normal tool call responses.

    Args:
        message: Error message to include in the response.
    """
    raise ToolError(f"[error_tool] {message}")


@mcp.tool
def binary_tool(size_kb: int = 50) -> Image:
    """Return a base64-encoded binary payload as an MCP ImageContent response.

    Tests how the firewall handles large base64-encoded binary data embedded
    in a JSON tool result (MCP ImageContent type).

    Args:
        size_kb: Size of the synthetic binary payload in kilobytes (default 50).
    """
    data = os.urandom(size_kb * 1024)
    return Image(data=data, format="png")


@mcp.tool
async def slow_tool(steps: int = 5, ctx: Context = None) -> str:
    """Slow tool that reports incremental progress via MCP progress notifications.

    Tests:
      - Firewall keep-alive for long-running MCP requests
      - MCP progress notifications (notifications/progress) through the firewall

    Args:
        steps: Number of processing steps, each taking 0.2 seconds (default 5, ~1 s total).
    """
    for i in range(steps):
        await asyncio.sleep(0.2)
        if ctx is not None:
            await ctx.report_progress(i + 1, steps, f"Step {i + 1}/{steps}")
    return json.dumps(
        {
            "steps_completed": steps,
            "elapsed_approx_s": round(steps * 0.2, 2),
            "timestamp": time.time(),
        }
    )


@mcp.tool
async def notify_tool(ctx: Context = None) -> str:
    """Send a tools/list_changed notification to the connected client.

    Tests server-push notification via SSE: the server emits a one-way
    notifications/tools/list_changed message. The client should respond
    by re-fetching tools/list. This exercises the firewall's SSE push path.
    """
    if ctx is not None:
        await ctx.send_notification(ToolListChangedNotification())
    return json.dumps({"notification_sent": "tools_list_changed", "timestamp": time.time()})


# ---------------------------------------------------------------------------
# Resources — tests resources/list + resources/read MCP paths
# ---------------------------------------------------------------------------


@mcp.resource("resource://status")
def status_resource() -> str:
    """Current server status — small JSON resource (~200 bytes).

    Tests resources/list discovery and resources/read with a small response.
    """
    return json.dumps(
        {
            "resource": "status",
            "server": "longevity-test",
            "mode": "large" if args.large else "normal",
            "timestamp": time.time(),
            "healthy": True,
        }
    )


@mcp.resource("resource://large-doc")
def large_doc_resource() -> str:
    """Large document resource (~100 KB of synthetic text).

    Tests resources/read with a large response. Mirrors the large_data tool
    but exercises the resources/read code path instead of tools/call.
    """
    line = "A" * 90 + "\n"  # ~91 bytes per line
    count = (100 * 1024) // len(line) + 1
    return "".join(f"Line {i:06d}: {line}" for i in range(count))


@mcp.resource("resource://config/{section}")
def config_section(section: str) -> str:
    """Dynamic config resource — demonstrates URI template resources.

    Tests resources/read with a parameterised URI (resource templates).
    Also validates resources/templates/list discovery.

    Args:
        section: Config section name — 'network', 'security', or 'debug'.
    """
    configs = {
        "network": {"mtu": 1500, "timeout_s": 30, "retries": 3},
        "security": {"tls_version": "1.3", "cipher": "ECDHE-RSA-AES256-GCM-SHA384"},
        "debug": {"log_level": "warning", "trace": False, "metrics_port": 9090},
    }
    data = configs.get(section, {"section": section, "note": "unknown section"})
    return json.dumps({"section": section, **data, "timestamp": time.time()})


# ---------------------------------------------------------------------------
# Prompts — tests prompts/list + prompts/get MCP paths
# ---------------------------------------------------------------------------


@mcp.prompt
def system_context(mode: str = "testing") -> str:
    """System context prompt for MCP firewall test scenarios.

    Tests prompts/list discovery and prompts/get retrieval.

    Args:
        mode: Test mode — 'testing', 'longevity', or 'stress' (default: testing).
    """
    return (
        f"You are operating in MCP firewall {mode} mode. "
        "All MCP protocol messages pass through a Check Point security firewall "
        "that performs deep packet inspection. Ensure all requests are compliant "
        "with the MCP specification (JSON-RPC 2.0 over Streamable HTTP). "
        f"Test mode: {mode}. Timestamp: {time.time():.0f}."
    )


@mcp.prompt
def debug_analysis(target: str, detail_level: str = "standard") -> str:
    """Debug analysis prompt template for firewall traffic investigation.

    Tests prompts/get with both required and optional arguments.

    Args:
        target: Target to analyze (e.g. 'firewall-logs', 'mcp-traffic', 'latency').
        detail_level: Level of detail — 'brief', 'standard', or 'verbose' (default: standard).
    """
    detail_map = {
        "brief": "Provide a one-paragraph summary.",
        "standard": "Provide a structured analysis with key findings and recommendations.",
        "verbose": (
            "Provide an exhaustive analysis including raw data, statistical breakdowns, "
            "timeline of events, root cause analysis, and actionable remediation steps."
        ),
    }
    detail_instruction = detail_map.get(detail_level, detail_map["standard"])
    return (
        f"Analyze the following for MCP firewall testing: {target}\n\n"
        f"Detail level: {detail_level}\n"
        f"{detail_instruction}\n\n"
        "Focus on: protocol compliance, traffic patterns, firewall rule hits, "
        "latency distribution, error rates, and anomaly detection."
    )


# ---------------------------------------------------------------------------
# Large-mode: register many padded tools to make tools/list ~100 KB
# ---------------------------------------------------------------------------
# Each padded tool description is ~200 bytes. We need ~500 tools to reach 100 KB.
# We register them programmatically via mcp.add_tool / direct registration.

if args.large:
    # Generate a long description once so all tools share the same padding text
    PAD_TEXT = (
        "This is a synthetic padding tool registered for firewall longevity testing. "
        "It simulates a real tool with rich documentation. Parameters, return types, "
        "and descriptions are all intentionally verbose to increase the tools/list "
        "response size. This helps test how the firewall handles large MCP responses. "
        "The tool itself does nothing useful -- it simply echoes its index number. "
    )  # ~370 chars

    NUM_PADDING_TOOLS = 260  # 260 * ~390 chars ≈ 100 KB in JSON

    for _i in range(NUM_PADDING_TOOLS):
        # We need a closure to capture _i correctly
        def _make_padded_tool(idx: int):
            description = (
                f"Padding tool #{idx:04d}. {PAD_TEXT}"
                f"Tool index: {idx}. "
                f"Category: synthetic-padding. "
                f"Group: longevity-test-batch-{idx // 50}."
            )

            def _padded(
                input_value: str = "test",
                multiplier: int = 1,
                extra_flag: bool = False,
            ) -> str:
                f"""Padding tool {idx}. {description}

                Args:
                    input_value: An arbitrary string input (default: 'test').
                    multiplier: An integer multiplier for the output count (default: 1).
                    extra_flag: An extra boolean flag (default: False).
                """
                return json.dumps(
                    {
                        "tool_index": idx,
                        "input_value": input_value,
                        "multiplier": multiplier,
                        "extra_flag": extra_flag,
                        "result": input_value * multiplier,
                    }
                )

            _padded.__name__ = f"padding_tool_{idx:04d}"
            _padded.__doc__ = description
            return _padded

        _fn = _make_padded_tool(_i)
        mcp.tool(_fn)

    print(
        f"[server] Large mode: registered {NUM_PADDING_TOOLS} padding tools "
        f"(tools/list response ≈100 KB)",
        file=sys.stderr,
    )
else:
    print("[server] Normal mode: minimal tool set", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(
        f"[server] Starting Longevity MCP Server on http://{args.host}:{args.port}/mcp",
        file=sys.stderr,
    )
    mcp.run(
        transport="http",
        host=args.host,
        port=args.port,
        log_level="warning",  # keep server output quiet so client output is readable
    )
