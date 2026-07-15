# AbletonBridge Architecture

## Overview

AbletonBridge is a 3-layer system connecting AI assistants to Ableton Live through the Model Context Protocol (MCP). The MCP Server layer is modularized into 20+ focused modules. Each stdio MCP process exposes the complete tool surface, while a single process owns the shared Ableton backend resources at a time.

## System Architecture

```text
┌─────────────┐
│   Claude AI  │
│  (MCP Client)│
└──────┬───────┘
       │ MCP (stdio)
┌──────▼───────────────────────────────────────────────┐
│                    MCP Server                         │
│  ┌─────────┐  ┌────────────┐  ┌──────────────┐      │
│  │ server.py│  │  tools/*   │  │  prompts.py  │      │
│  │(orchestr)│  │(16 modules)│  │(4 workflows) │      │
│  └────┬─────┘  └─────┬──────┘  └──────────────┘      │
│       │              │         ┌──────────────┐       │
│       │              │         │instructions. │       │
│       │              │         │py (guidance) │       │
│       │              │         └──────────────┘       │
│       │              │                                │
│  ┌────▼─────┐  ┌─────▼──────┐  ┌──────────────┐      │
│  │ state.py │  │connections/│  │   cache/     │      │
│  │(globals) │  │ ableton.py │  │  browser.py  │      │
│  └──────────┘  │   m4l.py   │  └──────────────┘      │
│                └─────┬──────┘                         │
│  ┌──────────┐        │         ┌──────────────┐      │
│  │constants │        │         │  dashboard/  │      │
│  │validation│        │         │  html + srv  │      │
│  └──────────┘        │         └──────┬───────┘      │
└──────────────────────┼────────────────┼──────────────┘
                       │                │
          ┌────────────┼────────────┐   │ HTTP :9880
          │            │            │   │
   TCP :9877    UDP :9882   UDP/OSC │   │
          │            │    :9878/9 │   │
   ┌──────▼────────────▼──┐  ┌─────▼─┐ │
   │  Ableton Remote Script│  │ M4L   │ │
   │  (Control Surface)    │  │Bridge │ │
   │  handlers/*.py        │  │(opt.) │ │
   └───────────────────────┘  └───────┘ │
                                        │
                              ┌─────────▼─┐
                              │ Web Dashboard│
                              └─────────────┘
```

## Module Structure

### MCP Server (`MCP_Server/`)

```
MCP_Server/
├── __init__.py              # Package init, __version__, re-exports
├── server.py                # MCP orchestrator and owner-only backend lifecycle
│                            #   - lifespan, backend start/stop, MCP instance
│                            #   - tool/prompt/resource registration
│                            #   - call instrumentation for dashboard
├── ownership.py             # Cross-process Ableton control coordination
│                            #   - atomic owner lock + status responder (:9881)
│                            #   - owner metadata, release, active-operation tracking
├── state.py                 # ALL global mutable state
│                            #   - connections, stores, caches, locks
│                            #   - threading events, config, MCP instance ref
├── constants.py             # Pure constants (no mutations)
│                            #   - TIER_0/1/2_COMMANDS (command delay tiers)
│                            #   - BROWSER_CATEGORIES, CATEGORY_PRIORITY
│                            #   - cache TTL, disk paths
├── validation.py            # Input validation helpers
│                            #   - _validate_index, _validate_range, _validate_notes
│                            #   - _validate_automation_points, _reduce_automation_points
│                            #   - size limits: MAX_NOTES=10K, MAX_AUTOMATION_POINTS=500
├── grid_notation.py         # ASCII grid notation parser/formatter
├── instructions.py          # MCP server instructions (cross-tool guidance)
│                            #   - SERVER_INSTRUCTIONS constant (~650 words)
│                            #   - injected into client context on initialization
├── prompts.py               # MCP prompt templates (4 workflows)
│
├── connections/
│   ├── __init__.py          # Re-exports
│   ├── ableton.py           # AbletonConnection (TCP :9877)
│   │                        #   - tiered send_command() with per-tier delays
│   │                        #   - NON_IDEMPOTENT_COMMANDS (no retry for create/delete)
│   │                        #   - get_ableton_connection() singleton
│   └── m4l.py               # M4LConnection (UDP/OSC :9878/:9879)
│                             #   - OSC message building, chunked response reassembly
│                             #   - send_command_with_retry() (3 attempts, exponential backoff)
│                             #   - bridge version check on ping
│                             #   - threading.Lock on send_command(), ping cache (5s TTL)
│
├── cache/
│   ├── __init__.py
│   └── browser.py           # Browser cache system
│                             #   - populate_browser_cache() (BFS walk, depth 3)
│                             #   - resolve_device_uri() / resolve_sample_uri()
│                             #   - gzip disk persistence (~50ms load)
│
├── dashboard/
│   ├── __init__.py
│   ├── html.py              # DASHBOARD_HTML constant (HTML/CSS/JS)
│   └── server.py            # Starlette HTTP server (:9880)
│                             #   - status JSON endpoint
│                             #   - DashboardLogHandler (pipes logs to buffer)
│
└── tools/
    ├── __init__.py           # register_all_tools(mcp) — calls 16 modules
    ├── _base.py              # Shared infrastructure
    │                         #   - _tool_handler (semaphore + asyncio.to_thread + timeout)
    │                         #   - _m4l_result(), tool_success(), tool_error()
    ├── session.py            # 52 tools: status, release, transport, recording, views
    ├── tracks.py             # 29 tools: track CRUD, routing, monitoring, implicit arm
    ├── clips.py              # 56 tools: clip CRUD, notes, loop, follow actions
    ├── mixer.py              # 13 tools: volume, pan, sends, set_mixer
    ├── devices.py            # 50 tools: device params, racks, sidechain
    ├── browser.py            # 12 tools: search, load, presets
    ├── automation.py         # 12 tools: clip/track automation
    ├── arrangement.py        # 17 tools: arrangement clips, time editing, analysis
    ├── scenes.py             # 10 tools: scene CRUD, fire, follow actions, tempo
    ├── creative.py           # 17 tools: chords, drums, arpeggios, euclidean
    ├── m4l_tools.py          # 40 tools: M4L bridge (hidden params, chains)
    ├── snapshots.py          # 19 tools: snapshot/macro/param_map stores
    ├── audio.py              # 3 tools: audio analysis, input meters
    ├── grid.py               # 2 tools: grid notation I/O
    ├── workflows.py          # 10 tools: compound workflow tools
    └── midi_cc.py            # 5 tools: mapped and raw MIDI CC control
```

### Remote Script (`AbletonBridge_Remote_Script/`)

```
AbletonBridge_Remote_Script/
├── __init__.py              # Control Surface class + dispatch tables
│                            #   - _MODIFYING_HANDLERS dict (O(1) dispatch)
│                            #   - _READONLY_HANDLERS dict (O(1) dispatch)
│                            #   - TCP server (port 9877)
│                            #   - UDP server (port 9882, real-time params)
│                            #   - Main-thread scheduling via queue
└── handlers/
    ├── __init__.py
    ├── _helpers.py           # Shared: get_track(), get_clip(), get_scene()
    ├── session.py            # Transport, tempo, recording, views, song settings
    ├── tracks.py             # Track CRUD, routing, monitoring, groups
    ├── clips.py              # Clip CRUD, notes, warp markers, follow actions
    ├── mixer.py              # Volume, pan, sends, crossfader, scenes
    ├── devices.py            # Device params, racks, sidechain, presets
    ├── browser.py            # Browser navigation, load instruments, presets
    ├── scenes.py             # Scene management, follow actions
    ├── arrangement.py        # Arrangement clips, time editing
    ├── audio.py              # Warp mode, freeze/unfreeze, audio analysis
    ├── midi.py               # MIDI notes, quantize, transpose, capture
    └── automation.py         # Clip/track automation, envelopes
```

## Import DAG (Dependency Graph)

The module import graph is strictly acyclic:

```text
Level 0 (no internal imports):
  state.py, constants.py, validation.py, grid_notation.py, instructions.py

Level 1 (imports Level 0 only):
  ownership.py            →  state
  connections/ableton.py  →  state, constants
  connections/m4l.py      →  state

Level 2 (imports Levels 0-1):
  cache/browser.py        →  state, constants, connections.ableton
  dashboard/server.py     →  state, connections.ableton
  tools/_base.py          →  ownership

Level 3 (imports Levels 0-2):
  tools/*.py              →  _base, connections, validation, state, cache
  prompts.py              →  (standalone: just receives mcp instance)

Level 4 (imports everything):
  server.py               →  state, connections, cache, dashboard, tools, prompts, instructions
```

**Rule:** No module at Level N imports from Level N or higher. This prevents circular imports.

## Communication Protocols

### TCP (port 9877) — Command/Response

```
Client → Server: {"type": "command_name", "params": {...}}\n
Server → Client: {"status": "success", "result": {...}}\n
```

- Newline-delimited JSON
- Commands dispatched on Ableton's main thread via `schedule_message`
- 10-second timeout per command
- Tiered post-command delays (0ms / 10ms / 20ms)
- Serialized via `threading.Lock` (`_send_lock`) — one command at a time per socket

### UDP (port 9882) — Real-Time Parameters

```
Client → Server: {"type": "set_device_parameter", "params": {...}}
```

- Fire-and-forget (no response)
- No delay, no timeout
- Ideal for knob sweeps at 50+ Hz

### UDP/OSC (ports 9878/9879) — M4L Bridge

```
Send (9878): OSC message with command + args
Recv (9879): OSC response with result (possibly chunked)
```

- URL-safe base64 for large payloads
- Chunked response protocol for >2KB responses
- 10-second default timeout, dynamic scaling for large operations

### HTTP (port 9880) — Web Dashboard

- Starlette server with status JSON endpoint
- Real-time tool call metrics and server logs
- Auto-refreshes every 3 seconds

## Control Ownership Lifecycle

MCP stdio connections are process-private: clients such as Codex may launch one AbletonBridge process per task, but the Live TCP connection, M4L receive socket, dashboard port, and related runtime state must have one owner. Tool availability is therefore separated from backend ownership.

| Event | Behaviour |
|------|-----------|
| MCP process starts | Registers all tools immediately and begins in `standby` without connecting to Live. |
| First normal tool call | Atomically binds loopback port `9881`, starts the backend resources, and becomes `owner`. |
| Another process already owns `9881` | Remains healthy in `standby`; tools return a structured ownership error instead of terminating MCP initialization. |
| Status call | `get_server_capabilities` reports `control_role`, `control_availability`, active operations, and best-effort owner process/task metadata without claiming control. |
| Explicit release | `release_ableton_control` stops owner resources, then closes `9881`; a standby process can claim on its next normal tool call. |
| MCP shutdown | Performs the same release automatically. |
| Backend startup fails | Cleans up partial resources and releases `9881` so a later call can retry. |

The `9881` owner socket also serves a loopback-only JSON status response. Reusing the lock socket avoids a stale metadata file or another management port. If an unrelated process occupies the port, availability is reported as `occupied_unknown`.

Ownership has no idle timeout and cannot be stolen. Manual release is refused while a tool thread or owner background operation is still active, including work that outlived the MCP tool timeout. These constraints keep handoff explicit and prevent two processes from using backend resources during a transition.

## Command Delay Tiers

| Tier | Pre-Delay | Post-Delay | Example Commands |
|------|-----------|------------|-----------------|
| 0 | 0ms | 0ms | set_tempo, set_track_volume, fire_clip |
| 1 | 0ms | 10ms | add_notes_to_clip, create_clip_automation |
| 2 | 10ms | 10ms | create_midi_track, load_instrument_or_effect |
| Read | 0ms | 0ms | get_session_info, get_clip_notes |

> **v3.4.0 note:** Delays were reduced 5-10x from their original values (Tier 1: 50ms, Tier 2: 200ms total) after adding an `asyncio.Semaphore(1)` that serializes tool dispatch at the async level. The semaphore eliminates the need for large defensive delays.

## MCP Protocol Features

### Server Instructions
Defined in `instructions.py` and passed to `FastMCP(instructions=...)`. Automatically injected into the AI client's system context during MCP initialization. Covers cross-tool sequencing, compound tool preferences, M4L fallback logic, input constraints, and browser/loading patterns. ~650 words, model-agnostic.

### Resources (3)
- `ableton://session` — current session state
- `ableton://tracks` — all track information
- `ableton://capabilities` — server version, connections, cache

### Prompts (4)
- `create_beat` — guided drum pattern creation
- `mix_track` — structured mixing workflow
- `sound_design` — parameter exploration guide
- `arrange_section` — arrangement section builder

### Tools (347 core + 19 optional ElevenLabs)
All tools use the `@_tool_handler` decorator which:
1. Gates execution via `asyncio.Semaphore(1)` — only one tool runs at a time, preventing thread pool exhaustion and TCP socket corruption
2. Automatically claims Ableton control for normal tools; status and release are explicitly exempt
3. Wraps sync functions in `asyncio.to_thread()` for non-blocking execution
4. Tracks the real worker lifetime even after an async timeout, preventing unsafe release
5. Enforces a 120-second timeout via `asyncio.wait_for()`
6. Returns consistent structured success, validation, connection, ownership, timeout, and generic error responses

## Testing

```
tests/
├── conftest.py              # Fixtures: mock_ableton, mock_m4l, patch_*, reset_state
├── test_validation.py       # 37 tests: all _validate_* helpers
├── test_grid_notation.py    # 7 tests: parse/format round-trips
├── test_constants.py        # 4 tests: tier disjointness, completeness
├── test_state.py            # 5 tests: thread-safety, events, stores
├── test_tool_handler.py     # async decorator, errors, ownership guard, timeout safety
└── test_ownership.py        # claims, release, metadata, collisions, two stdio clients
```

Run tests:
```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## Key Design Decisions

1. **`register_tools(mcp)` pattern** — each tool module defines tools as closures inside a registration function that receives the MCP instance. This avoids circular imports and keeps tool definitions co-located.

2. **Centralized state** — all mutable globals in `state.py` accessed via `import MCP_Server.state as state`. Thread-safe via explicit locks (`store_lock`, `tool_call_lock`, `browser_cache_lock`, etc.).

3. **Tiered delays** — commands categorized by stability requirements. Property setters need no delay; structural changes (create/delete) need time for Ableton to update its internal state.

4. **Idempotency guards** — create/delete/duplicate commands disable retry (`max_attempts=1`) to prevent accidental double-creation.

5. **Effect chain persistence** — templates saved to `~/.ableton-bridge/chain_templates.json` on every `save_effect_chain` call; loaded on server startup. Survives server restarts.

6. **Compound tools** — high-level workflow tools that batch multiple Remote Script commands, reducing MCP round-trip overhead by 3-5x for common operations.

7. **Layered concurrency control** — three layers prevent command flooding and socket corruption:
   - `asyncio.Semaphore(1)` in `_tool_handler` — serializes tool dispatch at the async level
   - `threading.Lock` on `AbletonConnection` and `M4LConnection` — serializes socket access at the thread level
   - 5ms inter-command delay in Remote Script — defense-in-depth against scheduler flooding

   This layered approach replaced the original large delays (50-200ms) with proper synchronization primitives, achieving both faster throughput and better stability.

8. **Available MCP, single-owner backend** — every stdio process completes MCP initialization and exposes all tools. Port `9881` coordinates one owner for Live, M4L, dashboard, and background services. This preserves the existing stdio deployment model while removing the failure mode where a singleton collision caused later clients to receive no Ableton tools.
