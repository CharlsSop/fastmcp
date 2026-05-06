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
  Fuzzing : ASGI middleware randomly corrupts JSON responses on the wire
            (use --fuzzing, tune rate with --fuzz-rate 0.0-1.0)

Fuzzing strategies (applied randomly per response):
  truncate           — cut response body mid-way
  extra_open_brace   — prepend extra '{' before valid JSON
  missing_close      — strip last '}' or ']'
  invalid_field_name — replace a key with !!invalid!! (no quotes)
  swap_quote         — flip one '"' to "'" (single quote = invalid JSON)
  insert_garbage     — inject '@#$%^&' at a random position
  trailing_garbage   — append garbage bytes after the JSON
  break_colon        — replace one ':' with '='
  null_byte          — insert a NUL byte (\\x00) at a random position
  duplicate_comma    — replace one ',' with ',,'

Usage:
    python server.py
    python server.py --large
    python server.py --host 0.0.0.0 --port 9000 --large
    python server.py --fuzzing --fuzz-rate 0.5
    python server.py --fuzzing --fuzz-rate 1.0   # fuzz every response
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
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
parser.add_argument(
    "--fuzzing",
    action="store_true",
    default=False,
    help="Enable JSON fuzzing middleware — randomly corrupts responses on the wire",
)
parser.add_argument(
    "--fuzz-rate",
    type=float,
    default=0.3,
    dest="fuzz_rate",
    help="Fraction of responses to fuzz (0.0-1.0, default: 0.3 = 30%%)",
)
args = parser.parse_args()

# ---------------------------------------------------------------------------
# Silence FastMCP/MCP library loggers — ToolError tracebacks are intentional
# (error_tool is a test primitive) and flood the terminal unhelpfully.
# The server's own print() startup messages are unaffected.
# ---------------------------------------------------------------------------

logging.getLogger("fastmcp").setLevel(logging.CRITICAL)
logging.getLogger("mcp").setLevel(logging.CRITICAL)

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

# ---------------------------------------------------------------------------
# JSON Fuzzing — ASGI middleware that corrupts responses on the wire
# ---------------------------------------------------------------------------

def _fuzz_body(data: bytes) -> tuple[str, bytes]:
    """Apply one random corruption strategy to raw response bytes.

    Returns (strategy_name, corrupted_bytes).
    Only corrupts non-empty bodies; passes through empty bodies unchanged.
    """
    if not data:
        return "passthrough", data

    strategy = random.choice([
        "truncate",
        "extra_open_brace",
        "missing_close",
        "invalid_field_name",
        "swap_quote",
        "insert_garbage",
        "trailing_garbage",
        "break_colon",
        "null_byte",
        "duplicate_comma",
    ])

    try:
        s = data.decode("utf-8", errors="replace")

        if strategy == "truncate":
            cut = random.randint(len(s) // 4, max(len(s) // 4, len(s) - 1))
            return strategy, s[:cut].encode("utf-8", errors="replace")

        elif strategy == "extra_open_brace":
            return strategy, (("{" + s) if s.startswith("{") else ("[" + s)).encode("utf-8", errors="replace")

        elif strategy == "missing_close":
            # Strip the last } or ] from the string
            stripped = s.rstrip()
            if stripped and stripped[-1] in ('}', ']'):
                return strategy, stripped[:-1].encode("utf-8", errors="replace")
            return strategy, s[:-1].encode("utf-8", errors="replace")

        elif strategy == "invalid_field_name":
            # Replace a quoted JSON key like "fieldname": with !!invalid!!:
            patched = re.sub(
                r'"([a-zA-Z_][a-zA-Z0-9_]*)"(\s*:)',
                lambda m: f'!!{m.group(1)}!!{m.group(2)}',
                s, count=1,
            )
            return strategy, patched.encode("utf-8", errors="replace")

        elif strategy == "swap_quote":
            # Flip one " to ' at a random position (single quotes invalid in JSON)
            positions = [i for i, c in enumerate(s) if c == '"']
            if positions:
                idx = random.choice(positions)
                return strategy, (s[:idx] + "'" + s[idx + 1:]).encode("utf-8", errors="replace")
            return "passthrough", data

        elif strategy == "insert_garbage":
            pos = random.randint(1, max(1, len(s) - 1))
            garbage = "@#$%^&*!"
            return strategy, (s[:pos] + garbage + s[pos:]).encode("utf-8", errors="replace")

        elif strategy == "trailing_garbage":
            garbage = " FUZZ_GARBAGE_" + "".join(random.choices("ABCDEF0123456789", k=16))
            return strategy, (s + garbage).encode("utf-8", errors="replace")

        elif strategy == "break_colon":
            # Replace first : (used as key separator) with =
            idx = s.find('":')
            if idx != -1:
                pos = idx + 1  # position of ':'
                return strategy, (s[:pos] + "=" + s[pos + 1:]).encode("utf-8", errors="replace")
            return "passthrough", data

        elif strategy == "null_byte":
            pos = random.randint(0, max(0, len(data) - 1))
            return strategy, data[:pos] + b"\x00" + data[pos:]

        elif strategy == "duplicate_comma":
            idx = s.find(",")
            if idx != -1:
                return strategy, (s[:idx] + ",," + s[idx + 1:]).encode("utf-8", errors="replace")
            return "passthrough", data

    except Exception:
        pass

    return "passthrough", data


class FuzzingMiddleware:
    """ASGI middleware that randomly corrupts HTTP response bodies.

    Handles both application/json responses AND text/event-stream (SSE) responses,
    since MCP Streamable HTTP wraps JSON-RPC payloads inside SSE data: lines.
    Logs each corruption to stderr so you can correlate with firewall logs.
    """

    def __init__(self, app, fuzz_rate: float = 0.3) -> None:
        self.app = app
        self.fuzz_rate = fuzz_rate

    async def __call__(self, scope, receive, send) -> None:
        # Only intercept HTTP requests; pass websocket/lifespan through
        if scope["type"] != "http" or random.random() > self.fuzz_rate:
            await self.app(scope, receive, send)
            return

        # Collect the full response so we can inspect Content-Type and fuzz body
        response_start: dict = {}
        body_chunks: list[bytes] = []

        async def capture(message: dict) -> None:
            if message["type"] == "http.response.start":
                response_start.update(message)
            elif message["type"] == "http.response.body":
                body_chunks.append(message.get("body", b""))

        await self.app(scope, receive, capture)

        headers: list[tuple[bytes, bytes]] = response_start.get("headers", [])
        content_type = next(
            (v.decode("latin-1") for k, v in headers if k.lower() == b"content-type"),
            "",
        )
        full_body = b"".join(body_chunks)
        path = scope.get("path", "?")

        if "application/json" in content_type and full_body:
            # Direct JSON-RPC response
            strategy, fuzzed_body = _fuzz_body(full_body)
            print(
                f"[FUZZ/json] {path}  strategy={strategy}  "
                f"original={len(full_body)}B  fuzzed={len(fuzzed_body)}B",
                file=sys.stderr,
            )
            new_headers = [
                (k, str(len(fuzzed_body)).encode() if k.lower() == b"content-length" else v)
                for k, v in headers
            ]
            await send({"type": "http.response.start", "status": response_start.get("status", 200), "headers": new_headers})
            await send({"type": "http.response.body", "body": fuzzed_body, "more_body": False})

        elif "text/event-stream" in content_type and full_body:
            # MCP Streamable HTTP: JSON-RPC is embedded in SSE data: lines
            strategy, fuzzed_body = _fuzz_sse_body(full_body)
            print(
                f"[FUZZ/sse]  {path}  strategy={strategy}  "
                f"original={len(full_body)}B  fuzzed={len(fuzzed_body)}B",
                file=sys.stderr,
            )
            # Drop Content-Length (SSE bodies are variable length)
            new_headers = [(k, v) for k, v in headers if k.lower() != b"content-length"]
            await send({"type": "http.response.start", "status": response_start.get("status", 200), "headers": new_headers})
            await send({"type": "http.response.body", "body": fuzzed_body, "more_body": False})

        else:
            # Pass through unchanged (binary, empty, unknown type)
            await send({"type": "http.response.start", "status": response_start.get("status", 200), "headers": headers})
            await send({"type": "http.response.body", "body": full_body, "more_body": False})


def _fuzz_sse_body(data: bytes) -> tuple[str, bytes]:
    """Fuzz the JSON payload embedded inside SSE event data: lines.

    SSE format:  event: message\\r\\ndata: <JSON>\\r\\n\\r\\n
    We find data: lines and apply _fuzz_body to the JSON content within them.
    Each data: line is fuzzed independently with 70% probability so that
    multi-event SSE streams have a mix of good and bad events.
    """
    try:
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        fuzzed_lines = []
        used_strategy = "passthrough"
        for line in lines:
            if line.startswith("data: "):
                json_part = line[6:].rstrip("\r\n")
                if json_part and random.random() < 0.7:
                    s, fuzzed_json = _fuzz_body(json_part.encode("utf-8"))
                    if s != "passthrough":
                        used_strategy = f"sse:{s}"
                        line = "data: " + fuzzed_json.decode("utf-8", errors="replace") + "\n"
            fuzzed_lines.append(line)
        return used_strategy, "".join(fuzzed_lines).encode("utf-8", errors="replace")
    except Exception:
        return "passthrough", data


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    fuzz_info = (
        f"  FUZZING ON — rate={args.fuzz_rate:.0%}, {int(args.fuzz_rate * 100)}% of JSON responses will be corrupted"
        if args.fuzzing
        else "  Fuzzing: OFF (use --fuzzing to enable)"
    )
    print(
        f"[server] Starting Longevity MCP Server on http://{args.host}:{args.port}/mcp\n"
        f"{fuzz_info}",
        file=sys.stderr,
    )

    asgi_app = mcp.http_app()

    if args.fuzzing:
        asgi_app = FuzzingMiddleware(asgi_app, fuzz_rate=args.fuzz_rate)
        print(
            f"[server] FuzzingMiddleware active — strategies: truncate, extra_open_brace, "
            f"missing_close, invalid_field_name, swap_quote, insert_garbage, "
            f"trailing_garbage, break_colon, null_byte, duplicate_comma",
            file=sys.stderr,
        )

    uvicorn.run(asgi_app, host=args.host, port=args.port, log_level="warning")
