"""
Firewall Longevity Test - MCP Client
======================================
A longevity traffic generator that hammers an MCP server with repeated requests
while a firewall sits in the middle.

Features:
  - Configurable number of concurrent threads (asyncio tasks)
  - Configurable run duration: hours (1, 2, ...) or -1 for infinite
  - Two traffic modes:
      --large   : calls tools/list  → expects a large (~100 KB) JSON response
      (default) : calls ping tool   → expects a small JSON response
  - Live status dashboard: shows per-thread stats, totals, drop detection
  - Ignores individual timeouts/errors and keeps running

Usage:
    # Run for 1 hour, 4 threads, large responses
    python client.py --server-url http://192.168.1.1:8000/mcp --duration 1 --threads 4 --large

    # Run forever, 10 threads, lightweight ping
    python client.py --server-url http://10.0.0.1:8000/mcp --duration -1 --threads 10

    # Quick local test (2 minutes = 0.033 h, use --duration 0.033)
    python client.py --duration 0 --threads 2 --large
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser(
    description="Longevity MCP traffic generator for firewall testing",
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
    "--large",
    action="store_true",
    default=False,
    help=(
        "Request large responses: fetch tools/list (expect ~100 KB response). "
        "Default: call ping tool (small response)."
    ),
)
parser.add_argument(
    "--timeout",
    type=float,
    default=30.0,
    help="Per-request HTTP timeout in seconds (default: 30). Timeouts are counted but ignored.",
)
parser.add_argument(
    "--request-delay",
    type=float,
    default=0.05,
    help="Seconds to sleep between requests per thread (default: 0.05).",
)
args = parser.parse_args()

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
# Shared stats (thread-safe via asyncio single-threaded event loop)
# ---------------------------------------------------------------------------

@dataclass
class ThreadStats:
    thread_id: int
    sent: int = 0
    success: int = 0
    timeout: int = 0
    error: int = 0
    last_status: str = "starting"
    last_response_size: int = 0
    last_latency_ms: float = 0.0


STATS: list[ThreadStats] = [ThreadStats(thread_id=i) for i in range(args.threads)]
_stop_event = asyncio.Event()

# ---------------------------------------------------------------------------
# MCP HTTP helpers (raw httpx - avoids per-request connection overhead)
# ---------------------------------------------------------------------------

# We communicate with the MCP server via the streamable-HTTP / MCP JSON-RPC
# protocol over plain HTTP POST requests.


def _build_jsonrpc(method: str, params: dict, req_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": method,
        "params": params,
    }


def _tools_list_payload(req_id: int) -> dict:
    return _build_jsonrpc("tools/list", {}, req_id)


def _tools_call_ping_payload(req_id: int) -> dict:
    return _build_jsonrpc(
        "tools/call",
        {"name": "ping", "arguments": {}},
        req_id,
    )


# ---------------------------------------------------------------------------
# Worker coroutine
# ---------------------------------------------------------------------------

async def worker(thread_id: int, client: httpx.AsyncClient) -> None:
    stats = STATS[thread_id]
    req_id = thread_id * 1_000_000  # unique IDs per thread

    while not _stop_event.is_set():
        req_id += 1
        payload = _tools_list_payload(req_id) if args.large else _tools_call_ping_payload(req_id)

        t0 = time.monotonic()
        try:
            resp = await client.post(
                args.server_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                timeout=args.timeout,
            )
            latency_ms = (time.monotonic() - t0) * 1000
            stats.last_latency_ms = latency_ms

            if resp.status_code == 200:
                body = resp.text
                stats.last_response_size = len(body)

                # Parse the response - handle SSE format (data: {...})
                json_body = None
                if body.startswith("data:"):
                    # SSE format
                    for line in body.splitlines():
                        line = line.strip()
                        if line.startswith("data:"):
                            data_str = line[5:].strip()
                            if data_str and data_str != "[DONE]":
                                try:
                                    json_body = json.loads(data_str)
                                except json.JSONDecodeError:
                                    pass
                else:
                    try:
                        json_body = json.loads(body)
                    except json.JSONDecodeError:
                        pass

                if json_body is not None and ("result" in json_body or "tools" in str(json_body)):
                    stats.success += 1
                    stats.last_status = "OK"
                elif json_body and "error" in json_body:
                    stats.error += 1
                    err_msg = json_body["error"].get("message", "rpc-error") if isinstance(json_body.get("error"), dict) else "rpc-error"
                    stats.last_status = f"RPC-ERR:{err_msg[:30]}"
                else:
                    # Got a 200 with data but couldn't parse a clean result
                    stats.success += 1
                    stats.last_status = "OK(raw)"
            else:
                stats.error += 1
                stats.last_status = f"HTTP-{resp.status_code}"
        except httpx.TimeoutException:
            stats.timeout += 1
            stats.last_status = "TIMEOUT"
            stats.last_latency_ms = args.timeout * 1000
        except httpx.ConnectError as exc:
            stats.error += 1
            stats.last_status = f"CONN-ERR"
        except Exception as exc:
            stats.error += 1
            stats.last_status = f"ERR:{type(exc).__name__}"

        stats.sent += 1

        # Brief pause to avoid busy-looping
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


async def dashboard(start_time: float) -> None:
    """Periodically print a live status table."""
    # Print header block (static, printed once)
    print()
    print(f"{BOLD}{CYAN}{'='*72}{RESET}")
    print(f"{BOLD}{CYAN}  MCP Firewall Longevity Test{RESET}")
    print(f"{BOLD}{CYAN}{'='*72}{RESET}")
    print(f"  Server URL  : {args.server_url}")
    print(f"  Mode        : {'LARGE (tools/list ~100KB)' if args.large else 'SMALL (ping tool)'}")
    print(f"  Threads     : {args.threads}")
    duration_str = (
        "INFINITE"
        if RUN_SECONDS is None
        else ("2min smoke-test" if args.duration == 0 else f"{args.duration}h ({_format_duration(RUN_SECONDS)})")
    )
    print(f"  Duration    : {duration_str}")
    print(f"  Timeout/req : {args.timeout}s")
    print(f"  Started     : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{BOLD}{CYAN}{'='*72}{RESET}")
    print()

    # Column headers for the thread table
    HDR = f"  {'TH':>3}  {'SENT':>8}  {'OK':>8}  {'TIMEOUT':>8}  {'ERROR':>8}  {'LAST_STATUS':<22}  {'LATENCY':>9}  {'RESP_SIZE':>10}"
    SEP = f"  {'-'*3}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*22}  {'-'*9}  {'-'*10}"

    # Number of lines in the repeating block
    LINES_PER_UPDATE = 2 + args.threads + 4  # header + sep + rows + totals + blank

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

        # Success rate
        rate = (totals.success / totals.sent * 100) if totals.sent else 0.0
        drop_rate = ((totals.timeout + totals.error) / totals.sent * 100) if totals.sent else 0.0
        rps = totals.sent / elapsed if elapsed > 0 else 0.0

        if not first_print:
            # Move cursor up to overwrite the previous block
            sys.stdout.write(MOVE_UP.format(LINES_PER_UPDATE))
            sys.stdout.flush()

        first_print = False

        lines = []
        lines.append(
            f"  {BOLD}Elapsed: {_format_duration(elapsed)}   Remaining: {remaining}"
            f"   RPS: {rps:.1f}   "
            f"Success: {GREEN if rate >= 95 else RED}{rate:.1f}%{RESET}   "
            f"Dropped: {RED if drop_rate > 5 else GREEN}{drop_rate:.1f}%{RESET}{BOLD}{RESET}"
        )
        lines.append(HDR)
        lines.append(SEP)

        for s in STATS:
            col = _status_color(s.last_status)
            sz = f"{s.last_response_size:,}" if s.last_response_size else "-"
            lines.append(
                f"  {s.thread_id:>3}  {s.sent:>8,}  {s.success:>8,}  "
                f"{s.timeout:>8,}  {s.error:>8,}  "
                f"{col}{s.last_status:<22}{RESET}  "
                f"{s.last_latency_ms:>8.1f}ms  {sz:>10}"
            )

        lines.append(SEP)
        lines.append(
            f"  {'TOT':>3}  {totals.sent:>8,}  {totals.success:>8,}  "
            f"{totals.timeout:>8,}  {totals.error:>8,}  "
            f"{'':22}  {'':>9}  {'':>10}"
        )
        lines.append("")

        print("\n".join(lines), end="", flush=True)

        await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    start_time = time.monotonic()

    # Shared httpx client (connection-pooled)
    limits = httpx.Limits(
        max_connections=args.threads + 4,
        max_keepalive_connections=args.threads,
    )
    async with httpx.AsyncClient(limits=limits) as client:
        # Launch all workers
        worker_tasks = [
            asyncio.create_task(worker(tid, client))
            for tid in range(args.threads)
        ]
        dash_task = asyncio.create_task(dashboard(start_time))

        # Wait for duration or forever
        if RUN_SECONDS is not None:
            await asyncio.sleep(RUN_SECONDS)
            _stop_event.set()
        else:
            try:
                # Run until Ctrl+C
                await asyncio.gather(*worker_tasks)
            except asyncio.CancelledError:
                _stop_event.set()

        _stop_event.set()

        # Let dashboard do one final update
        await asyncio.sleep(1.5)
        dash_task.cancel()
        for t in worker_tasks:
            t.cancel()

        # Suppress cancellation noise
        await asyncio.gather(*worker_tasks, dash_task, return_exceptions=True)

    # Final summary
    elapsed = time.monotonic() - start_time
    totals = ThreadStats(thread_id=-1)
    for s in STATS:
        totals.sent += s.sent
        totals.success += s.success
        totals.timeout += s.timeout
        totals.error += s.error

    print()
    print(f"{BOLD}{CYAN}{'='*72}{RESET}")
    print(f"{BOLD}  FINAL SUMMARY{RESET}")
    print(f"{CYAN}{'='*72}{RESET}")
    print(f"  Total runtime   : {_format_duration(elapsed)}")
    print(f"  Total requests  : {totals.sent:,}")
    print(f"  Successful      : {totals.success:,}  ({totals.success/totals.sent*100:.1f}%)" if totals.sent else "  Successful      : 0")
    print(f"  Timeouts        : {totals.timeout:,}")
    print(f"  Errors          : {totals.error:,}")
    rps = totals.sent / elapsed if elapsed > 0 else 0
    print(f"  Avg RPS         : {rps:.2f}")
    print(f"{CYAN}{'='*72}{RESET}")
    print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{YELLOW}[client] Interrupted by user. Stopping...{RESET}")
