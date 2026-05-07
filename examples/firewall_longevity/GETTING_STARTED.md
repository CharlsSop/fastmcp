# MCP Firewall Longevity Tool — Getting Started Guide

A practical guide for QA engineers and R&D to set up and run the MCP firewall
longevity test tool from scratch.

---

## What Is This Tool?

A purpose-built **MCP traffic generator** for testing firewalls.

It consists of two Python scripts:

- **`server.py`** — an MCP server that exposes every MCP protocol primitive
- **`client.py`** — a multi-threaded traffic generator with a live dashboard

You place the server on one side of the firewall and run the client on the
other side. The client hammers the server through the firewall across all MCP
message types, and the live dashboard shows you in real time whether the
firewall is dropping, corrupting, or passing MCP traffic correctly.

---

## Step 1 — Clone the Repository

```bash
git clone https://github.com/CharlsSop/fastmcp.git
cd fastmcp
git checkout my-fast-mcp
```

---

## Step 2 — Install Dependencies

You need **Python 3.10+**.

```bash
pip install -e ".[dev]"
```

> **With uv (faster):**
> ```bash
> uv pip install -e ".[dev]"
> ```

All test scripts live under:
```
examples/firewall_longevity/
├── server.py
├── client.py
├── README.md          ← detailed parameter reference
└── GETTING_STARTED.md ← this file
```

---

## Step 3 — Run the Server

Run this on the **server-side machine** (behind or in front of the firewall).

### Basic start

```bash
python examples/firewall_longevity/server.py --host 0.0.0.0 --port 8000
```

> Binds on all interfaces so the client can reach it through the firewall.

### With large tool-list responses (~100 KB)

```bash
python examples/firewall_longevity/server.py --host 0.0.0.0 --large
```

### With JSON fuzzing (corrupts responses on the wire)

```bash
# Corrupt 30% of responses (default rate)
python examples/firewall_longevity/server.py --host 0.0.0.0 --fuzzing

# Corrupt every single response
python examples/firewall_longevity/server.py --host 0.0.0.0 --fuzzing --fuzz-rate 1.0
```

When the server is running you will see:
```
[server] Starting Longevity MCP Server on http://0.0.0.0:8000/mcp
  Fuzzing: OFF (use --fuzzing to enable)
```

---

## Step 4 — Run the Client

Run this on the **client-side machine** pointing at the server's IP.

### Quick 2-minute smoke test

```bash
python examples/firewall_longevity/client.py \
    --server-url http://<SERVER_IP>:8000/mcp \
    --mode mixed \
    --threads 4 \
    --duration 0
```

### 1-hour longevity run, all primitives, 8 threads

```bash
python examples/firewall_longevity/client.py \
    --server-url http://<SERVER_IP>:8000/mcp \
    --mode mixed \
    --threads 8 \
    --duration 1
```

### Infinite run (Ctrl+C to stop)

```bash
python examples/firewall_longevity/client.py \
    --server-url http://<SERVER_IP>:8000/mcp \
    --mode mixed \
    --threads 10 \
    --duration -1
```

---

## Traffic Modes

Use `--mode` to control what the client sends. Each mode exercises a specific
MCP protocol primitive.

| Mode | What it tests |
|---|---|
| `ping` | Basic `tools/call` round-trip (small payload) |
| `tools` | `tools/list` — large response, tests payload size handling |
| `resources` | `resources/list` + `resources/read` (static and large content) |
| `prompts` | `prompts/list` + `prompts/get` |
| `errors` | `tools/call error_tool` → server returns `isError: true` (not a crash) |
| `binary` | `tools/call binary_tool` → server returns a base64-encoded image |
| `progress` | `tools/call slow_tool` → server streams progress notifications |
| `notify` | `tools/call notify_tool` → server pushes `tools/list_changed` notification |
| `mixed` | Randomly rotates through all 8 modes each iteration |

> **Recommended for firewall testing:** `--mode mixed` — covers every wire-format
> message type in a single run.

---

## Understanding the Live Dashboard

```
  Elapsed: 00:14:36   Remaining: 00:45:23   RPS: 19.1   Success: 100.0%   Dropped: 0.0%   Notifs: 2082

   TH      SENT        OK      TO     ERR   NOTIF  MODE        LAST_STATUS               LATENCY   RESP_SIZE
    0       657       657       0       0      85  notify      prog 5/5                  948.2ms          75
    1       662       661       0       1      80  progress    prog 1/5                 1612.5ms      68,268
  ...
  TOT    16,754    16,752       0       2   2,082
```

| Column | Meaning |
|---|---|
| `TH` | Thread ID |
| `SENT` | Total requests sent by this thread |
| `OK` | Successful responses |
| `TO` | Timeouts |
| `ERR` | Errors (connection failures, parse errors, etc.) |
| `NOTIF` | Server-push notifications received |
| `MODE` | Last mode this thread executed |
| `LAST_STATUS` | Last result detail — `OK`, `TIMEOUT`, `CONN-ERR`, etc. |
| `LATENCY` | Last request round-trip time |
| `RESP_SIZE` | Last response size in bytes |
| `TOT` | Aggregate totals across all threads |

**What to watch for:**

| Dashboard indicator | What it means |
|---|---|
| `Success: 100%` in green | All traffic passing through the firewall |
| `Dropped: >5%` in red | Firewall is actively dropping connections |
| `TIMEOUT` in LAST_STATUS | Request timed out — possible silent drop by firewall |
| `CONN-ERR` in LAST_STATUS | TCP connection rejected or reset |
| RPS drops sharply | Firewall may be rate-limiting or deep-inspecting |

---

## JSON Fuzzing Mode (for Parser Testing)

Fuzzing tests how a firewall's MCP parser handles **malformed JSON on the
wire**. The server injects corruptions into responses before they leave the NIC.

**Start the server with fuzzing:**
```bash
python examples/firewall_longevity/server.py --host 0.0.0.0 --fuzzing --fuzz-rate 0.5
```

**Run the client normally** — it will receive corrupt responses and count them
as errors. The interesting part is what the firewall does:

| Firewall behavior | What it indicates |
|---|---|
| FW process crashes / restarts | Parser bug — buffer overflow or unhandled exception |
| Connection silently dropped, no RST | Parser hung waiting for more data |
| HTTP 200 returned, but session is broken | Parser accepted garbage |
| Clean TCP RST or HTTP 400 | Parser handled malformed input correctly ✅ |

**Server log output during fuzzing:**
```
[FUZZ/sse]   /mcp  strategy=sse:missing_close  original=3327B  fuzzed=3325B
[FUZZ/json]  /mcp  strategy=swap_quote         original=512B   fuzzed=512B
```

Correlate these timestamps against your firewall's traffic/kernel logs.

---

## Typical Test Scenarios

### Scenario 1 — Baseline (is the firewall passing MCP at all?)

```bash
# Server
python examples/firewall_longevity/server.py --host 0.0.0.0

# Client
python examples/firewall_longevity/client.py \
    --server-url http://<SERVER_IP>:8000/mcp \
    --mode mixed --threads 4 --duration 0
```
Expected: 100% success, no drops.

---

### Scenario 2 — Sustained longevity (does the firewall hold up over time?)

```bash
# Client — 8 hours, 8 threads
python examples/firewall_longevity/client.py \
    --server-url http://<SERVER_IP>:8000/mcp \
    --mode mixed --threads 8 --duration 8
```
Watch for: gradual increase in `TO` or `ERR`, RPS degradation, memory growth on the firewall.

---

### Scenario 3 — Large payload stress (does the firewall parse big MCP messages?)

```bash
# Server with large tool list
python examples/firewall_longevity/server.py --host 0.0.0.0 --large

# Client in tools mode
python examples/firewall_longevity/client.py \
    --server-url http://<SERVER_IP>:8000/mcp \
    --mode tools --threads 8 --duration 1
```
Each `tools/list` response is ~100 KB of JSON. Watch for drops on large payloads.

---

### Scenario 4 — Fuzzing (does the firewall parser crash on bad JSON?)

```bash
# Server with 50% fuzz rate
python examples/firewall_longevity/server.py --host 0.0.0.0 --fuzzing --fuzz-rate 0.5

# Client — mixed mode
python examples/firewall_longevity/client.py \
    --server-url http://<SERVER_IP>:8000/mcp \
    --mode mixed --threads 4 --duration 0
```
Monitor firewall process health during this test. Any crash = critical finding.

---

## Troubleshooting

**Client shows 100% ERR immediately**
→ Server is not reachable. Check IP, port, and that the server is running.

**TIMEOUT on every request**
→ Firewall is dropping the connection. Check the rulebase — there may be a DROP rule matching MCP traffic.

**ERR count climbs during fuzzing run**
→ Expected. The client receives corrupt responses and counts them as errors. That is the point.

**Server terminal is noisy / full of tracebacks**
→ You may have an older version. Pull the latest: `git pull origin my-fast-mcp`
