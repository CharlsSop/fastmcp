# Firewall Longevity Test — MCP Server & Client

A self-contained MCP server and client pair built for **firewall throughput and
longevity testing**. Covers every MCP protocol primitive so traffic through a
firewall exercises all wire-format message types.

---

## Files

| File | Purpose |
|---|---|
| `server.py` | MCP server exposing tools, resources, prompts, error responses, binary data, progress notifications, and server-push notifications |
| `client.py` | Longevity traffic generator with 9 selectable modes and a live status dashboard |

---

## MCP Protocol Coverage

| Primitive | How it is exercised |
|---|---|
| `tools/list` | `tools` mode — requests the full tool list (~100 KB with `--large`) |
| `tools/call` | `ping`, `errors`, `binary`, `progress`, `notify` modes |
| `resources/list` | `resources` mode |
| `resources/read` | `resources` mode — reads `resource://status` and `resource://large-doc` |
| `prompts/list` | `prompts` mode |
| `prompts/get` | `prompts` mode — fetches `system_context` and `debug_analysis` templates |
| Error responses (`isError:true`) | `errors` mode — `error_tool` returns a structured error |
| Binary / image content | `binary` mode — `binary_tool` returns a random-bytes PNG image |
| Progress notifications | `progress` mode — `slow_tool` streams `notifications/progress` per step |
| Server-push notifications | `notify` mode — `notify_tool` sends `notifications/tools/list_changed` |

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
# Default — all primitives available, small tool list
python examples/firewall_longevity/server.py

# Large mode — adds ~260 padded tools so tools/list ≈ 100 KB
python examples/firewall_longevity/server.py --large

# Custom host/port
python examples/firewall_longevity/server.py --host 0.0.0.0 --port 9000
```

### 3. Run the client

```bash
# Ping mode — simplest smoke test
python examples/firewall_longevity/client.py --mode ping

# Tools mode — large tool-list responses, 8 threads, 2 hours
python examples/firewall_longevity/client.py --mode tools --threads 8 --duration 2

# Mixed mode — random rotation across all 8 primitives
python examples/firewall_longevity/client.py --mode mixed --threads 4 --duration 1

# Infinite run (Ctrl+C to stop)
python examples/firewall_longevity/client.py --mode mixed --threads 10 --duration -1

# Against a remote server through a firewall
python examples/firewall_longevity/client.py \
    --server-url http://192.168.100.1:8000/mcp \
    --mode mixed \
    --threads 8 \
    --duration 2
```

---

## Client Parameters

| Flag | Default | Description |
|---|---|---|
| `--server-url` | `http://127.0.0.1:8000/mcp` | MCP server URL |
| `--mode` | `mixed` | Traffic mode — see table below |
| `--duration` | `1` | Hours to run. `-1` = infinite, `0` = 2-min smoke test |
| `--threads` | `4` | Concurrent worker tasks |
| `--large` | off | Alias for `--mode tools` (backwards compat) |
| `--timeout` | `30` | Per-request timeout in seconds (counted, not fatal) |
| `--request-delay` | `0.05` | Sleep between requests per thread (seconds) |

### `--mode` choices

| Mode | What the client does |
|---|---|
| `ping` | Calls `ping` tool, checks response text |
| `tools` | Calls `tools/list` — exercises large response path with `--large` |
| `resources` | `resources/list` + reads `resource://status` and `resource://large-doc` |
| `prompts` | `prompts/list` + gets `system_context` and `debug_analysis` templates |
| `errors` | Calls `error_tool`, asserts `isError:true` in the response |
| `binary` | Calls `binary_tool`, measures size of returned base64 image |
| `progress` | Calls `slow_tool` with a `progress_handler` callback, counts progress events |
| `notify` | Calls `notify_tool`, receives `tools/list_changed` server-push via `MessageHandler` |
| `mixed` | Randomly picks one of the 8 modes above each iteration |

## Server Parameters

| Flag | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8000` | Bind port |
| `--large` | off | Register ~260 padded tools so `tools/list` ≈ 100 KB |

---

## Live Dashboard

```
==============================================================================
  MCP Firewall Longevity Test
==============================================================================
  Server URL  : http://192.168.1.1:8000/mcp
  Mode        : MIXED (random all modes)
  Threads     : 8
  Duration    : 2h (02:00:00)
  Timeout/req : 30s
  Started     : 2026-05-04 12:00:00
==============================================================================

  Elapsed: 00:01:23   Remaining: 01:58:37   RPS: 142.3   Success: 99.7%   Dropped: 0.3%

   TH  SENT    OK  TO  ERR  NOTIF  MODE       LAST_STATUS          LATENCY  RESP_SIZE
    0  1054  1052   1    1      3  binary     OK(image)             12.4ms     68,200
    1  1049  1048   0    1      1  notify     OK(push->)            11.8ms        128
    2   987   987   0    0      0  resources  OK                    14.1ms      1,024
  ...
```

- **NOTIF** counts server-push notifications received (`tools/list_changed`)
- **MODE** shows which primitive each thread last exercised
- **LAST_STATUS** shows tool-specific detail (e.g. `OK(steps=5,notifs=1)` for progress mode)
- **Success %** turns red when below 95 %
- **Dropped %** turns red when above 5 %
- `TIMEOUT` / `CONN-ERR` / `HTTP-xxx` in LAST_STATUS indicate firewall drops
