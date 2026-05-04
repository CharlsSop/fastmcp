# Firewall Longevity Test — MCP Server & Client

This directory contains a self-contained MCP server and client pair built for
**firewall throughput and longevity testing**.

---

## Files

| File | Purpose |
|---|---|
| `server.py` | MCP server with small or large (~100 KB) tool-list responses |
| `client.py` | Longevity traffic generator with live status dashboard |

---

## Quick Start

### 1. Install dependencies (from the repo root)

```bash
pip install -e ".[dev]"
# or with uv:
uv pip install -e ".[dev]"
```

### 2. Start the server

```bash
# Normal mode (small tool list)
python examples/firewall_longevity/server.py

# Large mode — tools/list response ≈ 100 KB
python examples/firewall_longevity/server.py --large

# Custom host/port
python examples/firewall_longevity/server.py --large --host 0.0.0.0 --port 9000
```

### 3. Run the client

```bash
# Basic: 4 threads, 1 hour, small responses
python examples/firewall_longevity/client.py

# Large responses, 8 threads, 2 hours
python examples/firewall_longevity/client.py --large --threads 8 --duration 2

# Infinite run with 10 threads (Ctrl+C to stop)
python examples/firewall_longevity/client.py --threads 10 --duration -1

# Against a remote server through a firewall
python examples/firewall_longevity/client.py \
    --server-url http://192.168.100.1:8000/mcp \
    --large \
    --threads 8 \
    --duration 2
```

---

## Client Parameters

| Flag | Default | Description |
|---|---|---|
| `--server-url` | `http://127.0.0.1:8000/mcp` | MCP server URL |
| `--duration` | `1` | Hours to run. `-1` = infinite, `0` = 2-min smoke test |
| `--threads` | `4` | Concurrent worker tasks |
| `--large` | off | Request large (~100 KB) responses via `tools/list` |
| `--timeout` | `30` | Per-request timeout in seconds (counted, not fatal) |
| `--request-delay` | `0.05` | Sleep between requests per thread (seconds) |

## Server Parameters

| Flag | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8000` | Bind port |
| `--large` | off | Register ~260 padded tools so `tools/list` ≈ 100 KB |

---

## Live Dashboard

While the client runs you'll see a live table like:

```
========================================================================
  MCP Firewall Longevity Test
========================================================================
  Server URL  : http://192.168.1.1:8000/mcp
  Mode        : LARGE (tools/list ~100KB)
  Threads     : 8
  Duration    : 2h (02:00:00)
  Timeout/req : 30s
  Started     : 2026-05-04 12:00:00
========================================================================

  Elapsed: 00:01:23   Remaining: 01:58:37   RPS: 142.3   Success: 99.7%   Dropped: 0.3%

   TH      SENT        OK   TIMEOUT     ERROR  LAST_STATUS             LATENCY   RESP_SIZE
  ---  --------  --------  --------  --------  ----------------------  -------  ----------
    0     1,054     1,052         1         1  OK                      12.4ms     102,341
    1     1,049     1,048         0         1  OK                      11.8ms     102,341
  ...
```

- **Success %** turns red when below 95 %
- **Dropped %** turns red when above 5 %
- `TIMEOUT` / `CONN-ERR` / `HTTP-xxx` indicate firewall drops or connectivity issues
