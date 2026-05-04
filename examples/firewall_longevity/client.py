"""
Firewall Longevity Test - MCP Client
======================================
A longevity traffic generator that drives all MCP protocol primitives through
a security firewall under test.

Modes (--mode):
  ping       - Call ping tool (small, fast — baseline)
  tools      - Fetch full tools/list (large response if server started with --large)
  resources  - List resources + read resource://status and resource://large-doc
  prompts    - List prompts + get system_context and debug_analysis
  errors     - Call error_tool (expects isError: true response)
  binary     - Call binary_tool (expects base64 image in response)
  progress   - Call slow_tool with MCP progress notification tracking
  notify     - Call notify_tool and receive tools/list_changed server-push notification
  mixed      - Randomly rotate through all modes each iteration

Legacy:
  --large    - Alias for --mode tools (backward compat)

Usage:
    # All primitives, 8 threads, 4 hours
    python client.py --mode mixed --threads 8 --duration 4

    # Large tools/list throughput test
    python client.py --mode tools --threads 4 --duration 1

    # SSE server-push / notification test
    python client.py --mode notify --threads 2 --duration 0

    # Quick 2-minute smoke test, all modes
    python client.py --mode mixed --duration 0 --threads 4

    # Remote firewall
    python client.py --server-url http://10.0.0.1:8000/mcp --mode mixed --duration 8 --threads 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import mcp.types

from fastmcp import Client
from fastmcp.client.messages import MessageHandler
from fastmcp.client.transports import StreamableHttpTransport

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

VALID_MODES = ["ping", "tools", "resources", "prompts", "errors", "binary", "progress", "notify", "mixed"]

parser = argparse.ArgumentParser(
    description="Longevity MCP traffic generator — tests all MCP primitives through a firewall",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=__doc__,
)
parser.add_argument(
    "--server-url",
    default="http://127.0.0.1:8000/mcp",
    help="MCP server URL (default: http://127.0.0.1:8000/mcp)",
)
parser.add_argument(
    "--duration",
    type=float,
    default=1.0,
    help=(
        "How long to run in hours. "
        "Use -1 for infinite, 0 for a quick 2-minute smoke test. "
        "(default: 1)"
    ),
)
parser.add_argument(
    "--threads",
    type=int,
    default=4,
    help="Number of concurrent worker tasks (default: 4)",
)
parser.add_argument(
    "--mode",
    default="ping",
    choices=VALID_MODES,
    help="Traffic mode — see module docstring for details. (default: ping)",
)
parser.add_argument(
    "--large",
    action="store_true",
    default=False,
    help="Legacy alias for --mode tools.",
)
parser.add_argument(
    "--timeout",
    type=float,
    default=30.0,
    help="Per-request timeout in seconds (default: 30). Timeouts are counted but ignored.",
)
parser.add_argument(
    "--request-delay",
    type=float,
    default=0.05,
    help="Seconds to sleep between requests per task (default: 0.05).",
)
args = parser.parse_args()

# Resolve legacy --large flag
if args.large:
    args.mode = "tools"

# ---------------------------------------------------------------------------
# Duration helpers
# ---------------------------------------------------------------------------

if args.duration == -1:
    RUN_SECONDS: float | None = None  # infinite
elif args.duration == 0:
    RUN_SECONDS = 120.0  # 2-minute smoke test
else:
    RUN_SECONDS = args.duration * 3600.0

# ---------------------------------------------------------------------------
# Shared stats (safe — single asyncio event loop)
# ---------------------------------------------------------------------------

@dataclass
class ThreadStats:
    thread_id: int
    sent: int = 0
    success: int = 0
    timeout: int = 0
    error: int = 0
    notifications: int = 0
    last_status: str = "starting"
    last_response_size: int = 0
    last_latency_ms: float = 0.0
    last_mode: str = ""


STATS: list[ThreadStats] = [ThreadStats(thread_id=i) for i in range(args.threads)]
_stop_event = asyncio.Event()

# ---------------------------------------------------------------------------
# Notification handler — counts tools/list_changed server-push messages
# ---------------------------------------------------------------------------


class _NotifyHandler(MessageHandler):
    """MessageHandler that counts incoming tools/list_changed server-push notifications."""

    def __init__(self, stats: ThreadStats) -> None:
        self._stats = stats

    async def on_tool_list_changed(
        self, message: mcp.types.ToolListChangedNotification
    ) -> None:
        self._stats.notifications += 1


# ---------------------------------------------------------------------------
# Per-request dispatcher
# ---------------------------------------------------------------------------

_ACTIVE_MODES = [m for m in VALID_MODES if m != "mixed"]


async def _do_request(client: Client, mode: str, stats: ThreadStats) -> tuple[int, str]:
    """Execute one MCP request for the given mode. Returns (response_size_bytes, status_str)."""

    if mode == "ping":
        result = await client.call_tool("ping", {})
        # call_tool() returns CallToolResult; .data has the parsed string
        text = result.data if isinstance(result.data, str) else (
            result.content[0].text if result.content else ""
        )
        return len(text.encode() if isinstance(text, str) else b""), "OK"

    elif mode == "tools":
        tools = await client.list_tools()
        size = sum(len(t.name) + len(t.description or "") for t in tools)
        return size, f"OK({len(tools)}tools)"

    elif mode == "resources":
        resources = await client.list_resources()
        # Read status (small) and large-doc (large) to cover both sizes
        total_size = 0
        for uri in ["resource://status", "resource://large-doc"]:
            contents = await client.read_resource(uri)
            for c in contents:
                if hasattr(c, "text") and c.text:
                    total_size += len(c.text.encode())
                elif hasattr(c, "blob") and c.blob:
                    total_size += len(c.blob)
        return total_size, f"OK({len(resources)}res)"

    elif mode == "prompts":
        prompts = await client.list_prompts()
        total_size = 0
        # Get system_context (no required args) + debug_analysis (has required arg)
        for name, kwargs in [("system_context", {"mode": "testing"}), ("debug_analysis", {"target": "mcp-traffic"})]:
            result = await client.get_prompt_mcp(name, kwargs)
            for msg in result.messages:
                if hasattr(msg.content, "text") and msg.content.text:
                    total_size += len(msg.content.text.encode())
        return total_size, f"OK({len(prompts)}prompts)"

    elif mode == "errors":
        result = await client.call_tool_mcp("error_tool", {})
        if result.isError:
            return 64, "OK(isError✓)"
        return 64, "WARN:noError"

    elif mode == "binary":
        result = await client.call_tool("binary_tool", {"size_kb": 50})
        # result.content[0] is ImageContent with .data (base64 string)
        size = len(result.content[0].data) if result.content and hasattr(result.content[0], "data") else 0
        return size, "OK(image)"

    elif mode == "progress":
        received: list[int] = [0]

        async def _on_progress(p: float, t: float | None, msg: str | None) -> None:
            received[0] += 1
            stats.last_status = f"prog {int(p)}/{int(t or 0)}"

        result = await client.call_tool("slow_tool", {"steps": 5}, progress_handler=_on_progress)
        text = result.data if isinstance(result.data, str) else (
            result.content[0].text if result.content else "{}"
        )
        try:
            data = json.loads(text) if isinstance(text, str) else {}
        except Exception:
            data = {}
        return len(text.encode() if isinstance(text, str) else b""), f"OK(steps={data.get('steps_completed', '?')},notifs={received[0]})"

    elif mode == "notify":
        result = await client.call_tool("notify_tool", {})
        text = result.data if isinstance(result.data, str) else (
            result.content[0].text if result.content else "{}"
        )
        return len(text.encode() if isinstance(text, str) else b""), "OK(push->)"

    return 0, "SKIP"


# ---------------------------------------------------------------------------
# Worker coroutine — uses FastMCP Client (handles MCP protocol correctly)
# ---------------------------------------------------------------------------


async def worker(thread_id: int) -> None:
    stats = STATS[thread_id]

    while not _stop_event.is_set():
        mode = random.choice(_ACTIVE_MODES) if args.mode == "mixed" else args.mode
        stats.last_mode = mode

        t0 = time.monotonic()
        try:
            transport = StreamableHttpTransport(
                url=args.server_url,
                headers={"mcp-session-id": str(uuid.uuid4())},
            )
            msg_handler = _NotifyHandler(stats) if mode == "notify" else None
            async with Client(transport, timeout=args.timeout, message_handler=msg_handler) as client:
                size, status = await _do_request(client, mode, stats)
                stats.last_response_size = size

            latency_ms = (time.monotonic() - t0) * 1000
            stats.last_latency_ms = latency_ms
            stats.success += 1
            if not stats.last_status.startswith("prog"):
                stats.last_status = status

        except TimeoutError:
            stats.timeout += 1
            stats.last_status = "TIMEOUT"
            stats.last_latency_ms = args.timeout * 1000
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            stats.last_latency_ms = latency_ms
            name = type(exc).__name__
            msg = str(exc)[:25]
            if "timeout" in msg.lower() or "Timeout" in name:
                stats.timeout += 1
                stats.last_status = "TIMEOUT"
            elif "connect" in msg.lower() or "Connection" in name:
                stats.error += 1
                stats.last_status = "CONN-ERR"
            else:
                stats.error += 1
                stats.last_status = f"{name[:20]}"

        stats.sent += 1

        if args.request_delay > 0:
            await asyncio.sleep(args.request_delay)


# ---------------------------------------------------------------------------
# Status dashboard (runs in the same event loop)
# ---------------------------------------------------------------------------

CLEAR_LINE = "\033[2K\033[1G"
MOVE_UP = "\033[{}A"
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _format_duration(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _status_color(status: str) -> str:
    if status.startswith("OK"):
        return GREEN
    if status == "TIMEOUT":
        return YELLOW
    return RED


_MODE_DESC = {
    "ping":      "PING (tools/call ping)",
    "tools":     "TOOLS (tools/list ~100KB)",
    "resources": "RESOURCES (resources/list + resources/read)",
    "prompts":   "PROMPTS (prompts/list + prompts/get)",
    "errors":    "ERRORS (error_tool -> isError:true)",
    "binary":    "BINARY (binary_tool -> base64 image)",
    "progress":  "PROGRESS (slow_tool + progress notifs)",
    "notify":    "NOTIFY (notify_tool + list_changed push)",
    "mixed":     "MIXED (random all modes)",
}


async def dashboard(start_time: float) -> None:
    """Periodically print a live status table."""
    print()
    print(f"{BOLD}{CYAN}{'='*78}{RESET}")
    print(f"{BOLD}{CYAN}  MCP Firewall Longevity Test — All Primitives{RESET}")
    print(f"{BOLD}{CYAN}{'='*78}{RESET}")
    print(f"  Server URL  : {args.server_url}")
    print(f"  Mode        : {_MODE_DESC.get(args.mode, args.mode)}")
    print(f"  Threads     : {args.threads}")
    duration_str = (
        "INFINITE"
        if RUN_SECONDS is None
        else ("2min smoke-test" if args.duration == 0 else f"{args.duration}h ({_format_duration(RUN_SECONDS)})")
    )
    print(f"  Duration    : {duration_str}")
    print(f"  Timeout/req : {args.timeout}s")
    print(f"  Started     : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{BOLD}{CYAN}{'='*78}{RESET}")
    print()

    HDR = (
        f"  {'TH':>3}  {'SENT':>8}  {'OK':>8}  {'TO':>6}  {'ERR':>6}  {'NOTIF':>6}"
        f"  {'MODE':<10}  {'LAST_STATUS':<24}  {'LATENCY':>9}  {'RESP_SIZE':>10}"
    )
    SEP = (
        f"  {'-'*3}  {'-'*8}  {'-'*8}  {'-'*6}  {'-'*6}  {'-'*6}"
        f"  {'-'*10}  {'-'*24}  {'-'*9}  {'-'*10}"
    )

    LINES_PER_UPDATE = 2 + args.threads + 4
    first_print = True

    while not _stop_event.is_set():
        elapsed = time.monotonic() - start_time
        remaining = (
            "∞"
            if RUN_SECONDS is None
            else _format_duration(max(0.0, RUN_SECONDS - elapsed))
        )

        totals = ThreadStats(thread_id=-1)
        for s in STATS:
            totals.sent += s.sent
            totals.success += s.success
            totals.timeout += s.timeout
            totals.error += s.error
            totals.notifications += s.notifications

        rate = (totals.success / totals.sent * 100) if totals.sent else 0.0
        drop_rate = ((totals.timeout + totals.error) / totals.sent * 100) if totals.sent else 0.0
        rps = totals.sent / elapsed if elapsed > 0 else 0.0

        if not first_print:
            sys.stdout.write(MOVE_UP.format(LINES_PER_UPDATE))
            sys.stdout.flush()
        first_print = False

        lines = []
        lines.append(
            f"  {BOLD}Elapsed: {_format_duration(elapsed)}   Remaining: {remaining}"
            f"   RPS: {rps:.1f}   "
            f"Success: {GREEN if rate >= 95 else RED}{rate:.1f}%{RESET}   "
            f"Dropped: {RED if drop_rate > 5 else GREEN}{drop_rate:.1f}%{RESET}   "
            f"Notifs: {CYAN}{totals.notifications}{RESET}{BOLD}{RESET}"
        )
        lines.append(HDR)
        lines.append(SEP)

        for s in STATS:
            col = _status_color(s.last_status)
            sz = f"{s.last_response_size:,}" if s.last_response_size else "-"
            lines.append(
                f"  {s.thread_id:>3}  {s.sent:>8,}  {s.success:>8,}  "
                f"{s.timeout:>6,}  {s.error:>6,}  {s.notifications:>6,}  "
                f"{s.last_mode:<10}  {col}{s.last_status:<24}{RESET}  "
                f"{s.last_latency_ms:>8.1f}ms  {sz:>10}"
            )

        lines.append(SEP)
        lines.append(
            f"  {'TOT':>3}  {totals.sent:>8,}  {totals.success:>8,}  "
            f"{totals.timeout:>6,}  {totals.error:>6,}  {totals.notifications:>6,}  "
            f"{'':10}  {'':24}  {'':>9}  {'':>10}"
        )
        lines.append("")

        print("\n".join(lines), end="", flush=True)

        await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    start_time = time.monotonic()

    worker_tasks = [
        asyncio.create_task(worker(tid))
        for tid in range(args.threads)
    ]
    dash_task = asyncio.create_task(dashboard(start_time))

    if RUN_SECONDS is not None:
        await asyncio.sleep(RUN_SECONDS)
        _stop_event.set()
    else:
        try:
            await asyncio.gather(*worker_tasks)
        except asyncio.CancelledError:
            _stop_event.set()

    _stop_event.set()

    await asyncio.sleep(1.5)
    dash_task.cancel()
    for t in worker_tasks:
        t.cancel()

    await asyncio.gather(*worker_tasks, dash_task, return_exceptions=True)

    elapsed = time.monotonic() - start_time
    totals = ThreadStats(thread_id=-1)
    for s in STATS:
        totals.sent += s.sent
        totals.success += s.success
        totals.timeout += s.timeout
        totals.error += s.error
        totals.notifications += s.notifications

    print()
    print(f"{BOLD}{CYAN}{'='*78}{RESET}")
    print(f"{BOLD}  FINAL SUMMARY{RESET}")
    print(f"{CYAN}{'='*78}{RESET}")
    print(f"  Mode            : {_MODE_DESC.get(args.mode, args.mode)}")
    print(f"  Total runtime   : {_format_duration(elapsed)}")
    print(f"  Total requests  : {totals.sent:,}")
    if totals.sent:
        print(f"  Successful      : {totals.success:,}  ({totals.success/totals.sent*100:.1f}%)")
    print(f"  Timeouts        : {totals.timeout:,}")
    print(f"  Errors          : {totals.error:,}")
    print(f"  Notifications   : {totals.notifications:,}  (tools/list_changed received)")
    rps = totals.sent / elapsed if elapsed > 0 else 0
    print(f"  Avg RPS         : {rps:.2f}")
    print(f"{CYAN}{'='*78}{RESET}")
    print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{YELLOW}[client] Interrupted by user. Stopping...{RESET}")
