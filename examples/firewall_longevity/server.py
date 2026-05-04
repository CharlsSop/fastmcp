"""
Firewall Longevity Test - MCP Server
=====================================
An MCP server designed for firewall throughput and longevity testing.

Features:
  - Normal mode: small tool list + lightweight ping tool
  - Large mode (--large): registers hundreds of padded tools so that the
    tools/list response is ~100 KB, simulating heavy MCP traffic

Usage:
    # Normal response size
    python server.py

    # Large (~100K) tools/list response
    python server.py --large

    # Custom host/port
    python server.py --host 0.0.0.0 --port 9000 --large
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from fastmcp import FastMCP

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
        "It supports small and large response modes."
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
            "timestamp": time.time(),
        }
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
